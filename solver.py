from copy import deepcopy
from typing import Optional
import warnings
from packaging.version import Version

from cpmpy.solvers.solver_interface import SolverInterface, SolverStatus, ExitStatus
from cpmpy.expressions.core import Expression, Comparison, Operator
from cpmpy.expressions.variables import _BoolVarImpl, NegBoolView, _IntVarImpl, _NumVarImpl, BoolVar
from cpmpy.expressions.utils import is_num, is_any_list, is_boolexpr
from cpmpy.transformations.get_variables import get_variables
from cpmpy.transformations.normalize import toplevel_list
from cpmpy.transformations.decompose_global import decompose_in_tree
from cpmpy.transformations.flatten_model import flatten_constraint
from cpmpy.transformations.comparison import only_numexpr_equality
from cpmpy.transformations.reification import reify_rewrite, only_bv_reifies
from cpmpy.solvers.utils import SolverLookup
from cpmpy.expressions.core import Expression
from cpmpy.expressions.globalconstraints import Cumulative
from cpmpy.transformations.get_variables import get_variables
from cpmpy import Model
from cpmpy.expressions.globalconstraints import Cumulative

import numpy as np
from scipy.optimize import milp, LinearConstraint, Bounds

def extract_cumulative_matrix(constraints):
    """
    Extract interval list, demand matrix A, and capacity vector b
    from all Cumulative constraints.

    Returns:
        intervals : list of (start_expr, end_expr, dur_expr)
        A         : numpy.ndarray (num_constraints x num_intervals)
        b         : numpy.ndarray (num_constraints,)
    """
    cumulatives = [c for c in constraints if isinstance(c, Cumulative)]

    if not cumulatives:
        return [], np.zeros((0, 0), dtype=int), np.zeros((0,), dtype=int)

    # --- Step 1: collect unique intervals ---
    interval_index = {}
    intervals = []

    def register_interval(start, end, dur):
        key = (start, end, dur)
        if key not in interval_index:
            interval_index[key] = len(intervals)
            intervals.append(key)
        return interval_index[key]

    # First pass: discover all intervals
    for c in cumulatives:
        starts, durations, ends, demands, cap = c.args

        for s, e, d in zip(starts, ends, durations):
            register_interval(s, e, d)

    # --- Step 2: build A and b ---
    num_constraints = len(cumulatives)
    num_intervals = len(intervals)

    A = np.zeros((num_constraints, num_intervals), dtype=int)
    b = np.zeros(num_constraints, dtype=int)

    for i, c in enumerate(cumulatives):
        starts, durations, ends, demands, cap = c.args
        b[i] = int(cap)

        for s, e, dur, d in zip(starts, ends, durations, demands):
            j = interval_index[(s, e, dur)]
            A[i, j] = int(d)

    return intervals, A, b


class CPM_SATUNSAT_LS(SolverInterface):
    """
    SAT-UNSAT linear search meta-solver for CPMpy.

    Solves an optimization problem by repeatedly:
    - removing the objective
    - solving a SAT version
    - tightening the objective bound
    until UNSAT is reached.
    """

    @staticmethod
    def supported():
        # This solver depends only on CPMpy itself
        return True

    def __init__(self, cpm_model=None, subsolver=None, max_cons=5):
        if subsolver is None:
            raise ValueError("CPM_SATUNSAT_LS requires a subsolver name")

        self.subsolver = subsolver
        self.model = cpm_model
        self.max_cons = max_cons
        super().__init__(name="SATUNSAT_LS", cpm_model=None)

    @property
    def native_model(self):
        # No native backend
        return None

    def is_visited_cover(self, cover):
        for vis_cover, min_card in self.visited_covers:
            if min_card <= len(cover) and cover <= vis_cover:
                return True
        return False

    def propose_cover(self):
        for x in range(self.A.shape[1]):
            for y in range(x + 1, self.A.shape[1]):
                if self.is_visited_cover({x, y}):
                    continue
                if np.any(self.A[:, x] + self.A[:, y] > self.b):
                    return {x, y}
        triples = list()
        for x in range(self.A.shape[1]):
            for y in range(x + 1, self.A.shape[1]):
                if self.is_visited_cover({x, y}):
                    continue
                zs = [z for z in range(y + 1, self.A.shape[1]) if np.any(self.A[:, x] + self.A[:, y] + self.A[:, z] > self.b)]
                zs.sort(key=lambda z: -self.intervals[z][2])
                if len(zs) > 0:
                    z = zs[0]
                    triples.append({x, y, z})
        triples.sort(key=lambda ixs: -sum(self.intervals[i][2] for i in ixs))
        triples = triples[:10]
        for triple in triples:
            if self.is_visited_cover(triple):
                continue
            return triple
        return None

    def lifting_subproblem(self, lhs, rhs, next_ix, used_indices):
        # Maximize lhs.T * x w.r.t. A[:, used_indices] <= b - A[:, next_ix] and x binary
        res = milp(
            # SciPy minimizes, so negate objective
            c=-lhs[used_indices],
            constraints=LinearConstraint(self.A[:, used_indices], -np.inf, self.b - self.A[:, next_ix]),
            bounds=Bounds(0, 1),
            integrality=np.ones(len(used_indices), dtype=int), # 1 = integer variable
        )

        if not res.success:
            print(res.message)
            return rhs
            # raise RuntimeError(res.message)

        return int(np.round(lhs[used_indices] @ res.x))

    def process_cumulative_constraints(self):
        while True:
            cover = self.propose_cover()
            if cover is None:
                break
            lhs = np.zeros((self.A.shape[1],), dtype=int)
            rhs = len(cover) - 1
            used_indices = set()
            for ix in cover:
                lhs[ix] = 1
                used_indices.add(ix)
            while len(used_indices) < self.A.shape[1]:
                next_ix = max(
                    (i for i in range(self.A.shape[1]) if not i in used_indices),
                    key=lambda i: self.intervals[i][2]
                )
                lhs[next_ix] = rhs - self.lifting_subproblem(lhs, rhs, next_ix, list(used_indices))
                used_indices.add(next_ix)
            yield (lhs, rhs)
            self.visited_covers.add((frozenset({i for i in range(self.A.shape[1]) if lhs[i] == 1}), len(cover)))

    def solve(self, time_limit=None, **kwargs):
        # --- Extract objective ---
        if not self.has_objective():
            raise ValueError("SATUNSAT_LS requires an objective")

        obj_expr, minimize = self.model.objective_, self.model.objective_is_min

        self.intervals, self.A, self.b = extract_cumulative_matrix(self.model.constraints)
        self.visited_covers = set()
        cons = list(self.process_cumulative_constraints())
        cons.sort(key=lambda x: -sum(w * self.intervals[i][2] for i, w in enumerate(x[0])) / x[1])
        cons = cons[:self.max_cons]
        print(f'[solve] Added {len(cons)} new cumulative constraints')
        for lhs, rhs in cons:
            lhs_str = " + ".join(f"{w} * I_{ix}" for ix, w in enumerate(lhs) if w != 0)
            print(f'[solve] {lhs_str} ≤ {rhs}')
            lb = sum(w * self.intervals[i][2] for i, w in enumerate(lhs)) / rhs
            print(f'Elastic LB: {lb:.3f}')

        best_value = None
        best_solution = None

        m = self.model.copy()
        # m.objective(None, m.minimize)
        for lhs, rhs in cons:
            m += Cumulative(
                start=[x for w, (x, _, _) in zip(lhs, self.intervals) if w != 0],
                end=[y for w, (_, y, _) in zip(lhs, self.intervals) if w != 0],
                duration=[d for w, (_, _, d) in zip(lhs, self.intervals) if w != 0],
                demand=[w for w in lhs if w != 0],
                capacity=rhs,
            )
        solver = SolverLookup.get(name=self.subsolver, model=m)
        with open(f'model-{len(cons)}.fzn', 'w') as f:
            f.write(solver.flatzinc_string())
        raise NotImplementedError


        iteration = 0

        while True:
            if best_value is not None:
                print('[solve] Current objective is', best_value)
            iteration += 1
            # Fresh model every iteration
            # m = self.model.copy()
            # m.objective(None, m.minimize)
            # for lhs, rhs in cons:
            #    m += Cumulative(
            #        start=[x for w, (x, _, _) in zip(lhs, self.intervals) if w != 0],
            #        end=[y for w, (_, y, _) in zip(lhs, self.intervals) if w != 0],
            #        duration=[d for w, (_, _, d) in zip(lhs, self.intervals) if w != 0],
            #        demand=[w for w in lhs if w != 0],
            #        capacity=rhs,
            #    )
 

            # Add bounding constraint if we already have a solution
            assumptions = []
            if best_value is not None:
                b = BoolVar(name=f"bound_{best_value}")

                if minimize:
                    m += b.implies(obj_expr < best_value)
                else:
                    m += b.implies(obj_expr > best_value)
                assumptions = [b]

                # if minimize:
                    # m += (obj_expr < best_value)
                # else:
                    # m += (obj_expr > best_value)

            # Solve with subsolver
            solver = SolverLookup.get(name=self.subsolver, model=m)
            # sat = solver.solve(time_limit=time_limit, **kwargs)
            sat = solver.solve(time_limit=time_limit, assumptions=assumptions, **kwargs)

            if not sat:
                # UNSAT => last solution is optimal
                print("[solve] Optimal")
                self.cpm_status = SolverStatus(self.name)
                if best_solution is None:
                    self.cpm_status.exitstatus = ExitStatus.UNSATISFIABLE
                    return self._solve_return(self.cpm_status)

                # restore best solution
                for var, val in best_solution.items():
                    var._value = val

                self.objective_value_ = best_value
                self.cpm_status.exitstatus = ExitStatus.OPTIMAL
                return self._solve_return(self.cpm_status)

            # SAT: extract solution
            current_value = obj_expr.value()
            current_solution = {
                v: v.value() for v in set(get_variables(obj_expr)) | self.user_vars
            }

            best_value = current_value
            best_solution = current_solution

    def has_objective(self):
        return self.objective is not None


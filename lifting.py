from loguru import logger

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


def collect_cover_sets(durations, A, b, cover_card, pool_size):
    assert cover_card >= 2
    covers = list()
    # Add all binary covers
    for x in range(A.shape[1]):
        for y in range(x + 1, A.shape[1]):
            if np.any(A[:, x] + A[:, y] > b):
                covers.append({x, y})
    binary_covers = len(covers)
    logger.info(f"Added {binary_covers} binary covers")
    # For any pair of tasks, find another tasks to form a ternary cover,
    # and choose the longest one
    if cover_card >= 3:
        for x in range(A.shape[1]):
            for y in range(x + 1, A.shape[1]):
                rem = b - A[:, x] - A[:, y]
                if np.any(rem < 0):
                    continue
                zs = [z for z in range(y + 1, A.shape[1]) if np.any(A[:, z] > rem)]
                zs.sort(key=lambda z: -durations[z])
                if len(zs) > 0:
                    covers.append({x, y, zs[0]})
        ternary_covers = len(covers) - binary_covers
        logger.info(f"Added {ternary_covers} ternary covers")
    # Choose the required number of covers, discarding the ones with
    # the worst elastic lower bound values
    covers.sort(key=lambda cover: -sum(durations[i] for i in cover) / (len(cover) - 1))
    covers = covers[:pool_size]
    return covers


def is_visited_cover(cover, visited_covers):
    for vis_cover, min_card in visited_covers:
        if min_card <= len(cover) and cover <= vis_cover:
            return True
    return False


def lifting_subproblem(A, b, lhs, next_ix, used_indices):
    # Maximize lhs.T * x w.r.t. A[:, used_indices] <= b - A[:, next_ix] and x binary
    res = milp(
        # SciPy minimizes, so negate objective
        c=-lhs[used_indices],
        constraints=LinearConstraint(A[:, used_indices], -np.inf, b - A[:, next_ix]),
        bounds=Bounds(0, 1),
        integrality=np.ones(len(used_indices), dtype=int), # 1 = integer variable
    )

    if not res.success:
        logger.error(f"Failed to solve the lifting subproblem for index {next_ix}, used indices {used_indices}, and LHS {lhs}\n")
        logger.error(f"HiGHS error message: {res.message}")
        raise RuntimeError(res.message)

    return int(np.round(lhs[used_indices] @ res.x))


def lift_cover(durations, A, b, cover):
    lhs = np.zeros((A.shape[1],), dtype=int)
    rhs = len(cover) - 1
    used_indices = set()
    for ix in cover:
        lhs[ix] = 1
        used_indices.add(ix)
    while len(used_indices) < A.shape[1]:
        next_ix = max(
            (i for i in range(A.shape[1]) if not i in used_indices),
            key=lambda i: durations[i]
        )
        lhs[next_ix] = rhs - lifting_subproblem(A, b, lhs, next_ix, list(used_indices))
        used_indices.add(next_ix)
    return (lhs, rhs)


def process_cumulative_constraints(durations, A, b, cover_card, pool_size):
    cover_sets = collect_cover_sets(durations, A, b, cover_card, pool_size)
    logger.info(f"Received {len(cover_sets)} cover sets")
    visited_covers = list()
    cons = list()
    for cover in cover_sets:
        if is_visited_cover(cover, visited_covers):
            continue
        lhs, rhs = lift_cover(durations, A, b, cover)
        visited_covers.append((frozenset({i for i in range(A.shape[1]) if lhs[i] == 1}), len(cover)))
        cons.append((lhs, rhs))
    return cons


def run_lifting(model, max_cons, cover_card, pool_size):
    if max_cons == 0:
        return model.copy()
    intervals, A, b = extract_cumulative_matrix(model.constraints)
    durations = [d for _, _, d in intervals]
    cons = process_cumulative_constraints(durations, A, b, cover_card, pool_size)
    logger.info(f'Received {len(cons)} candidate cumulative constraints')
    for ub in range(1, cover_card):
        n_cons = sum(1 if rhs == ub else 0 for _, rhs in cons)
        if n_cons > 0:
            logger.info(f'* {n_cons} with RHS = {ub}')
    cons.sort(key=lambda x: -sum(w * d for w, d in zip(x[0], durations)) / x[1])
    cons = cons[:max_cons]
    full_model = model.copy()
    for lhs, rhs in cons:
        lhs_str = " + ".join(f"{w}*[{ix}]" for ix, w in enumerate(lhs) if w != 0)
        logger.info(f'{lhs_str} ≤ {rhs}')
        lb = sum(w * d for w, d in zip(lhs, durations)) / rhs
        logger.info(f'Elastic lower bound: {lb:.3f}')
        full_model += Cumulative(
            start=[x for w, (x, _, _) in zip(lhs, intervals) if w != 0],
            end=[y for w, (_, y, _) in zip(lhs, intervals) if w != 0],
            duration=[d for w, (_, _, d) in zip(lhs, intervals) if w != 0],
            demand=[w for w in lhs if w != 0],
            capacity=rhs,
        )
    logger.info(f'Added {len(cons)} new cumulative constraints')
    return full_model

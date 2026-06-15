"""Optimality-preserving (OP) disjunctions for RCPSP.

Builds a *disjointness/conflict graph* over tasks (as in Unite-and-Lead), seeded
with the HARD disjoint pairs implied by the resource capacities, optionally
extended with OP edges (pairs that may be forced non-overlapping without changing
the optimal makespan), then clique-covers the graph and posts each clique as a
unit-capacity ``Cumulative`` (i.e. a ``disjunctive``) to strengthen propagation.

Only normal RCPSP (``flavor == 'std'``) is handled.
"""

import itertools
import subprocess
import time
from pathlib import Path

import numpy as np
from loguru import logger
from cpmpy import Model, intvar
from cpmpy.expressions.globalconstraints import Cumulative
from cpmpy.expressions.python_builtins import max as cpm_max


# --------------------------------------------------------------------------- #
#  Instance view + cheap structural quantities
# --------------------------------------------------------------------------- #
def _instance_view(data):
    n = data["n_tasks"]
    p = np.asarray(data["d"], dtype=int)                 # durations
    cap = np.asarray(data["rc"], dtype=int)              # resource capacities
    rr = np.asarray(data["rr"], dtype=int)               # n_res x n_tasks
    succ = [set(s) for s in data["suc"]]                 # 1-indexed successors
    # 0-indexed successor / predecessor lists
    succ0 = [[j - 1 for j in succ[i]] for i in range(n)]
    pred0 = [[] for _ in range(n)]
    for i in range(n):
        for j in succ0[i]:
            pred0[j].append(i)
    return n, p, cap, rr, succ0, pred0


def _heads_tails(n, p, succ0, pred0):
    """Longest-path heads h_i and tails q_i over the precedence DAG."""
    head = [None] * n
    tail = [None] * n

    def h(i):
        if head[i] is None:
            head[i] = max((h(k) + int(p[k]) for k in pred0[i]), default=0)
        return head[i]

    def t(i):
        if tail[i] is None:
            tail[i] = max((int(p[j]) + t(j) for j in succ0[i]), default=0)
        return tail[i]

    return [h(i) for i in range(n)], [t(i) for i in range(n)]


def _reachable(n, succ0):
    """All-pairs precedence reachability (transitive closure), 0-indexed."""
    reach = [set() for _ in range(n)]
    order = []  # reverse-topological via DFS
    seen = [False] * n

    def dfs(u):
        seen[u] = True
        for v in succ0[u]:
            if not seen[v]:
                dfs(v)
        order.append(u)

    for u in range(n):
        if not seen[u]:
            dfs(u)
    for u in order:  # children finished before parents
        for v in succ0[u]:
            reach[u].add(v)
            reach[u] |= reach[v]
    return reach


# --------------------------------------------------------------------------- #
#  Conflict graph construction
# --------------------------------------------------------------------------- #
def hard_pairs(n, rr, cap):
    """Pairs (i,j) that physically cannot overlap: r_ik + r_jk > C_k for some k."""
    edges = set()
    n_res = rr.shape[0]
    for i in range(n):
        for j in range(i + 1, n):
            if any(rr[r, i] + rr[r, j] > cap[r] for r in range(n_res)):
                edges.add((i, j))
    return edges


def candidate_pairs(n, rr, cap, reach):
    """Pairs that CAN overlap (no resource conflict) and are precedence-incomparable."""
    n_res = rr.shape[0]
    out = []
    for i in range(n):
        for j in range(i + 1, n):
            if j in reach[i] or i in reach[j]:
                continue  # precedence-ordered => already disjoint, not a candidate
            if any(rr[r, i] + rr[r, j] > cap[r] for r in range(n_res)):
                continue  # hard pair
            out.append((i, j))
    return out


def cheap_op_test(i, j, p, rr, cap, head, tail, L, U):
    """Sufficient/necessary cheap tests (pairwise-spreadability note).

    Returns 'reject' | 'accept' | 'unknown'.
    """
    lam = min(head[i], head[j]) + int(p[i]) + int(p[j]) + min(tail[i], tail[j])
    if U is not None and lam > U:
        return "reject"  # serial bound exceeds an upper bound on T*  => not OP
    # energetic accept (conservative; see note): Lambda + sum_k E_k/(C_k-rho_k) <= L
    n_res = rr.shape[0]
    congestion = 0.0
    ok = True
    for r in range(n_res):
        rho = max(int(rr[r, i]), int(rr[r, j]))
        if rho == 0:
            continue
        denom = int(cap[r]) - rho
        if denom <= 0:
            ok = False
            break
        e_rest = int(rr[r, :].sum()) - int(rr[r, i]) - int(rr[r, j])
        e_rest = sum(int(p[t]) * int(rr[r, t]) for t in range(len(p))
                     if t != i and t != j)
        congestion += e_rest / denom
    if ok and lam + congestion <= L:
        return "accept"
    return "unknown"


# --------------------------------------------------------------------------- #
#  Ground-truth oracle (sound) via OR-Tools through CPMpy
# --------------------------------------------------------------------------- #
def _build_cpmpy(n, p, cap, rr, succ0, extra_disjoint=()):
    model = Model()
    s = intvar(0, int(p.sum()), shape=n)
    for i in range(n):
        for j in succ0[i]:
            model += s[j] >= s[i] + int(p[i])
    for r in range(rr.shape[0]):
        model += Cumulative(start=s, duration=p, end=s + p,
                            demand=rr[r, :], capacity=int(cap[r]))
    for (a, b) in extra_disjoint:
        model += (s[a] + int(p[a]) <= s[b]) | (s[b] + int(p[b]) <= s[a])
    makespan = cpm_max(s + p)
    model.minimize(makespan)
    return model, s, makespan


def optimal_makespan(n, p, cap, rr, succ0, extra_disjoint=(), time_limit=None):
    model, s, makespan = _build_cpmpy(n, p, cap, rr, succ0, extra_disjoint)
    ok = model.solve(solver="ortools", time_limit=time_limit) if time_limit \
        else model.solve(solver="ortools")
    if not ok:
        return None, None
    return int(makespan.value()), [int(v) for v in s.value()]


def oracle_op_edges(n, p, cap, rr, succ0, cands, t_star,
                    per_solve_limit=10, budget_s=180):
    """Sound OP-edge detection via incremental closure: a candidate {a,b} is
    accepted iff the model with all previously-accepted edges PLUS disjoint(a,b)
    still admits a makespan-`t_star` schedule.  Testing against the *augmented*
    model (not the original) is what makes the accepted set JOINTLY sound — OP
    composes only in that order (see pairwise-spreadability-conflict-graph.md)."""
    op = []
    t0 = time.perf_counter()
    n_tested = 0
    for (a, b) in cands:
        if time.perf_counter() - t0 > budget_s:
            logger.warning(f"OP oracle budget exhausted after {n_tested} tests, "
                           f"{len(op)} edges accepted ({len(cands)} candidates total)")
            break
        n_tested += 1
        t_ab, _ = optimal_makespan(n, p, cap, rr, succ0,
                                  extra_disjoint=op + [(a, b)],
                                  time_limit=per_solve_limit)
        # finding a t_star-schedule under the extra disjunctions proves the
        # augmented optimum equals t_star (it can never be smaller) => sound.
        if t_ab is not None and t_ab == t_star:
            op.append((a, b))
    return op


# --------------------------------------------------------------------------- #
#  Cheap detector: SPREAD-FAIL dominance check (separable.mzn), scope {a,b}
# --------------------------------------------------------------------------- #
def spreadfail_screen(instance_path, cands, ub, solver="chuffed",
                      per_pair_limit_ms=10000, budget_s=300):
    """Screen candidate pairs with the SPREAD-FAIL dominance CSP.  For each
    {a,b}, `separable.mzn` is UNSAT iff the pair is separable (optimality-
    preserving) under the scope-{a,b} re-timing witness -- a *sufficient*,
    cheaper-than-solving certificate.  Returns the individually-OP pairs."""
    mzn = str(Path(__file__).resolve().parent / "separable.mzn")
    screened = []
    t0 = time.perf_counter()
    n_tested = 0
    for (i, j) in cands:
        if time.perf_counter() - t0 > budget_s:
            logger.warning(f"SPREAD-FAIL budget exhausted after {n_tested} tests, "
                           f"{len(screened)} accepted ({len(cands)} candidates)")
            break
        n_tested += 1
        a, b = i + 1, j + 1  # separable.mzn is 1-indexed
        cmd = ["minizinc", "--solver", solver, "-t", str(per_pair_limit_ms),
               mzn, str(instance_path), "-D", f"a={a};b={b};ub={ub}"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=per_pair_limit_ms / 1000 + 20)
            if "UNSATISFIABLE" in res.stdout:
                screened.append((i, j))
        except subprocess.TimeoutExpired:
            continue  # inconclusive => conservatively not OP
    logger.info(f"SPREAD-FAIL: {len(screened)} individually-OP pairs from "
                f"{n_tested} tested ({len(cands)} candidates)")
    return screened


def _try_disjoint(n, p, cap, rr, succ0, disjoints, fix_to, scope, t_star,
                  solver, per_pair_limit):
    """Is there a makespan-<=t_star schedule satisfying ALL `disjoints`, with the
    tasks outside `scope` pinned to `fix_to`?  Returns such a schedule (the
    witness) or None.  A returned witness is optimal and respects every pair in
    `disjoints`, so it can serve as the next reference schedule."""
    model, s, _mk = _build_cpmpy(n, p, cap, rr, succ0, extra_disjoint=disjoints)
    for i in range(n):
        if i not in scope:
            model += (s[i] == int(fix_to[i]))
        model += (s[i] + int(p[i]) <= int(t_star))
    if model.solve(solver=solver, time_limit=per_pair_limit):
        return [int(v) for v in s.value()]
    return None


def spreadfail_closure(n, p, cap, rr, succ0, cands, t_star, sched0,
                       solver="ortools", per_pair_limit=10, use_neighbors=False,
                       budget_s=None):
    """Composition-aware OP-edge accumulation (the incremental loop).

    Maintain a committed set of disjunctions and a reference optimal schedule.
    For each candidate {a,b}, check -- against the model that ALREADY contains
    the committed edges -- whether re-timing only its scope can make a,b disjoint
    while keeping makespan == t_star.  If so, COMMIT the edge and adopt the
    witness as the new reference (it respects all committed edges plus the new
    one).  By construction every committed edge is OP w.r.t. the augmented model,
    so the whole set is JOINTLY optimality-preserving."""
    accepted = []
    S = list(sched0)
    t0 = time.perf_counter()
    n_tested = 0
    for (a, b) in cands:
        if budget_s is not None and time.perf_counter() - t0 > budget_s:
            logger.warning(f"closure budget exhausted after {n_tested} tests, "
                           f"{len(accepted)} committed ({len(cands)} candidates)")
            break
        n_tested += 1
        scope = {a, b} | (_resource_neighbors(n, rr, a, b) if use_neighbors else set())
        witness = _try_disjoint(n, p, cap, rr, succ0, accepted + [(a, b)], S,
                                scope, t_star, solver, per_pair_limit)
        if witness is not None:
            accepted.append((a, b))
            S = witness  # respects all committed edges + the new one
    logger.info(f"composition closure: {len(accepted)} jointly-OP edges from "
                f"{n_tested} candidates (scope-neighbours={use_neighbors})")
    return accepted


def _spreadfail_model(n, p, cap, rr, succ0, pred0, a, b, ub, committed):
    """Native CPMpy encoding of the all-schedules SPREAD-FAIL check for {a,b}
    w.r.t. model + `committed` disjunctions.  Searches for an UNREPAIRABLE
    counterexample (scope-{a,b} re-timing, relative makespan, horizon `ub` only;
    no optimum used).  UNSAT (proven) => {a,b} is optimality-preserving."""
    from cpmpy import Model, intvar, boolvar
    from cpmpy.expressions.python_builtins import all as cpm_all, max as cpm_max, min as cpm_min
    n_res = rr.shape[0]
    pi = [int(x) for x in p]
    s = intvar(0, ub, shape=n)
    obj = intvar(0, ub)
    C = [obj <= ub] + [s[i] + pi[i] <= obj for i in range(n)]
    for i in range(n):
        for j in succ0[i]:
            C.append(s[j] >= s[i] + pi[i])
    for r in range(n_res):
        C.append(Cumulative(start=s, duration=pi, end=[s[i] + pi[i] for i in range(n)],
                            demand=[int(rr[r, i]) for i in range(n)], capacity=int(cap[r])))
    for (c, e) in committed:
        C.append((s[c] + pi[c] <= s[e]) | (s[e] + pi[e] <= s[c]))
    C += [s[a] < s[b] + pi[b], s[b] < s[a] + pi[a]]      # a,b overlap

    others = [j for j in range(n) if j != a and j != b]
    free = [[int(cap[r]) - sum(int(rr[r, j]) * ((s[j] <= t) & (t < s[j] + pi[j]))
                               for j in others if rr[r, j] > 0)
             for t in range(ub)] for r in range(n_res)]

    rela = cpm_max([s[i] + pi[i] for i in pred0[a]]) if pred0[a] else 0
    relb = cpm_max([s[i] + pi[i] for i in pred0[b]]) if pred0[b] else 0
    uba = cpm_min([obj] + [s[j] for j in succ0[a]]) - pi[a]
    ubb = cpm_min([obj] + [s[j] for j in succ0[b]]) - pi[b]
    CmtA = {e for (c, e) in committed if c == a} | {c for (c, e) in committed if e == a}
    CmtB = {e for (c, e) in committed if c == b} | {c for (c, e) in committed if e == b}

    def es_ls(task, rel, ubt, Cmt):
        es, ls = [], []
        for t in range(0, ub - pi[task] + 1):
            conds = [rel <= t, t <= ubt]
            for r in range(n_res):
                if rr[r, task] > 0:
                    conds += [free[r][tau] >= int(rr[r, task]) for tau in range(t, t + pi[task])]
            for x in Cmt:
                conds.append((t + pi[task] <= s[x]) | (s[x] + pi[x] <= t))
            ft = boolvar()
            C.append(ft == cpm_all(conds))
            es.append(ub - (ub - t) * ft)
            ls.append(-1 + (t + 1) * ft)
        return cpm_min(es), cpm_max(ls)

    ESa, LSa = es_ls(a, rela, uba, CmtA)
    ESb, LSb = es_ls(b, relb, ubb, CmtB)
    C += [ESa + pi[a] > LSb, ESb + pi[b] > LSa]          # non-repairable
    return Model(C)


def spreadfree_closure(n, p, cap, rr, succ0, pred0, cands, ub, solver="ortools",
                       per_pair_limit=15, budget_s=None):
    """OPTIMUM-FREE composition closure (the ceiling experiment), native CPMpy.

    Incremental loop: for each candidate, run the all-schedules SPREAD-FAIL check
    (extended with the committed disjunctions) via `_spreadfail_model`.  A PROVEN
    UNSAT means the pair is OP w.r.t. model+committed -> commit it and re-check
    the rest against the larger model.  No optimum is ever computed -- only the
    horizon `ub`.  The committed set is jointly optimality-preserving."""
    from cpmpy.solvers.solver_interface import ExitStatus
    committed = []
    t0 = time.perf_counter()
    n_tested = n_sat = n_unknown = 0
    for (i, j) in cands:
        if budget_s is not None and time.perf_counter() - t0 > budget_s:
            logger.warning(f"spreadfree budget exhausted after {n_tested} tests, "
                           f"{len(committed)} committed ({len(cands)} candidates)")
            break
        n_tested += 1
        m = _spreadfail_model(n, p, cap, rr, succ0, pred0, i, j, ub, committed)
        m.solve(solver=solver, time_limit=per_pair_limit)
        st = m.status().exitstatus
        if st == ExitStatus.UNSATISFIABLE:           # proven => OP
            committed.append((i, j))
        elif st == ExitStatus.OPTIMAL or st == ExitStatus.FEASIBLE:
            n_sat += 1                               # counterexample => not OP
        else:
            n_unknown += 1                           # timeout => conservatively not OP
    logger.info(f"spreadfree closure (native, ub={ub}): {len(committed)} jointly-OP "
                f"edges; {n_sat} refuted, {n_unknown} inconclusive of {n_tested} tested")
    return committed


def _resource_neighbors(n, rr, a, b):
    """Tasks that share a resource with a or b (candidates for being pushed
    around when separating a and b)."""
    nbr = set()
    for t in range(n):
        if t == a or t == b:
            continue
        for r in range(rr.shape[0]):
            if rr[r, t] > 0 and (rr[r, a] > 0 or rr[r, b] > 0):
                nbr.add(t)
                break
    return nbr


def spreadfail_native(n, p, cap, rr, succ0, cands, t_star, sched_star,
                      solver="minizinc:pumpkin", per_pair_limit=10,
                      use_neighbors=True, budget_s=None):
    """Native (CPMpy) SPREAD-FAIL with neighbour pushing.

    For each candidate {a,b}, freeze every task OUTSIDE the scope
    S = {a,b} (optionally ∪ resource-neighbours) at its value in the reference
    optimal schedule `sched_star`, free the scope, and ask for a schedule with
    `disjoint(a,b)` whose makespan is still `t_star`.  SAT means an optimal
    schedule with a,b disjoint exists (reachable by re-timing only the scope),
    which certifies {a,b} is optimality-preserving.  Wider scope ⇒ more edges;
    scope = all tasks recovers the full oracle.  Sound, incomplete."""
    op = []
    t0 = time.perf_counter()
    n_tested = 0
    for (a, b) in cands:
        if budget_s is not None and time.perf_counter() - t0 > budget_s:
            logger.warning(f"native SPREAD-FAIL budget exhausted after {n_tested} "
                           f"tests, {len(op)} accepted ({len(cands)} candidates)")
            break
        n_tested += 1
        S = {a, b} | (_resource_neighbors(n, rr, a, b) if use_neighbors else set())
        model, s, _mk = _build_cpmpy(n, p, cap, rr, succ0, extra_disjoint=[(a, b)])
        for i in range(n):
            if i not in S:
                model += (s[i] == int(sched_star[i]))
        for i in range(n):
            model += (s[i] + int(p[i]) <= int(t_star))   # makespan <= T*
        if model.solve(solver=solver, time_limit=per_pair_limit):
            op.append((a, b))
    logger.info(f"native SPREAD-FAIL (neighbours={use_neighbors}): {len(op)} "
                f"individually-OP pairs from {n_tested} tested ({len(cands)} candidates)")
    return op


# --------------------------------------------------------------------------- #
#  Greedy edge clique cover
# --------------------------------------------------------------------------- #
def clique_cover(n, edges):
    """Greedy edge clique cover: repeatedly grow a maximal clique around an
    uncovered edge until all edges are covered.  Returns a list of cliques
    (each a sorted list of >=2 vertices)."""
    adj = {v: set() for v in range(n)}
    for (a, b) in edges:
        adj[a].add(b)
        adj[b].add(a)
    uncovered = set(frozenset(e) for e in edges)
    cliques = []
    while uncovered:
        a, b = tuple(next(iter(uncovered)))
        clique = {a, b}
        # candidates: common neighbours of the whole clique
        cand = adj[a] & adj[b]
        while cand:
            # pick the candidate sharing the most edges with current clique
            v = max(cand, key=lambda x: len(adj[x] & clique))
            clique.add(v)
            cand &= adj[v]
        cl = sorted(clique)
        cliques.append(cl)
        for x, y in itertools.combinations(cl, 2):
            uncovered.discard(frozenset((x, y)))
    return cliques


# --------------------------------------------------------------------------- #
#  Entry point
# --------------------------------------------------------------------------- #
def add_op_edges(model, start, data, method="none", instance_path=None,
                 budget_s=300, spreadfail_solver="ortools", cache_path=None,
                 use_neighbors=False, per_pair_limit=10, prefix=None,
                 detect_only=False):
    """Augment `model` (a CPMpy Model with task start vars `start`) with
    disjunctive cliques from the conflict graph.  `method` selects the OP-edge
    detector: 'none' (hard edges only), 'oracle' (sound ground-truth closure,
    a cp-sat solve per candidate -- a ceiling/debugging tool), or 'spreadfail'
    (cheap SPREAD-FAIL screening + a small soundness closure on the screened
    set).  If `cache_path` is given, OP edges are cached as JSON."""
    import json
    import os
    if prefix is not None and method != "none" \
            and not (cache_path and os.path.exists(cache_path)):
        raise ValueError("--op-prefix requires a pre-built --op-cache "
                         "(run with --op-detect-only first)")
    n, p, cap, rr, succ0, pred0 = _instance_view(data)
    head, tail = _heads_tails(n, p, succ0, pred0)
    reach = _reachable(n, succ0)

    # bounds on T*
    L = max(max(head[i] + int(p[i]) + tail[i] for i in range(n)),
            max(int(np.ceil(sum(int(p[t]) * int(rr[r, t]) for t in range(n)) / int(cap[r])))
                for r in range(rr.shape[0])))
    U = int(p.sum())  # trivial valid upper bound

    hard = hard_pairs(n, rr, cap)
    cands = candidate_pairs(n, rr, cap, reach)
    logger.info(f"Conflict graph: {len(hard)} hard pairs, {len(cands)} overlap-candidates")

    # cheap test diagnostics
    n_rej = n_acc = 0
    cheap_op = []
    for (i, j) in cands:
        v = cheap_op_test(i, j, p, rr, cap, head, tail, L, U)
        if v == "reject":
            n_rej += 1
        elif v == "accept":
            n_acc += 1
            cheap_op.append((i, j))
    logger.info(f"Cheap test on candidates: {n_acc} accept, {n_rej} reject, "
                f"{len(cands) - n_acc - n_rej} unknown (L={L}, U={U})")

    op_edges = []
    if method in ("oracle", "spreadfail", "spreadfree"):
        if cache_path and os.path.exists(cache_path):
            with open(cache_path) as fh:
                op_edges = [tuple(e) for e in json.load(fh)]
            logger.info(f"Loaded {len(op_edges)} OP edges from cache {cache_path}")
        elif method == "spreadfree":
            # optimum-free: only an upper bound on the horizon (a feasible
            # incumbent), never the optimum.  ub bounds the counterexample search.
            ub_val, _ = optimal_makespan(n, p, cap, rr, succ0, time_limit=5)
            ub = ub_val if ub_val is not None else int(p.sum())
            logger.info(f"spreadfree detector (optimum-free), horizon ub={ub}")
            op_edges = spreadfree_closure(n, p, cap, rr, succ0, pred0, cands, ub,
                                          solver=("ortools" if spreadfail_solver.startswith("minizinc")
                                                  else spreadfail_solver),
                                          per_pair_limit=per_pair_limit,
                                          budget_s=budget_s)
            if cache_path:
                with open(cache_path, "w") as fh:
                    json.dump([list(e) for e in op_edges], fh)
                logger.info(f"Saved OP edges to cache {cache_path}")
        else:
            t_star, sched_star = optimal_makespan(n, p, cap, rr, succ0)
            logger.info(f"T* = {t_star} (detector = {method})")
            if method == "oracle":
                op_edges = oracle_op_edges(n, p, cap, rr, succ0, cands, t_star,
                                           budget_s=budget_s)
            else:  # spreadfail: composition-aware incremental closure
                op_edges = spreadfail_closure(n, p, cap, rr, succ0, cands,
                                              t_star, sched_star,
                                              solver=spreadfail_solver,
                                              per_pair_limit=per_pair_limit,
                                              use_neighbors=use_neighbors,
                                              budget_s=budget_s)
            logger.info(f"Detector '{method}' found {len(op_edges)} OP edges")
            if cache_path:
                with open(cache_path, "w") as fh:
                    json.dump([list(e) for e in op_edges], fh)
                logger.info(f"Saved OP edges to cache {cache_path}")

    full_seqlen = len(op_edges)
    if detect_only:
        return model, {
            "hard": len(hard), "candidates": len(cands),
            "cheap_accept": n_acc, "cheap_reject": n_rej,
            "op_edges": full_seqlen, "seqlen": full_seqlen,
            "cliques": 0, "L": L, "U": U,
        }
    if prefix is not None:
        k = max(0, min(prefix, full_seqlen))
        if k != prefix:
            logger.info(f"--op-prefix {prefix} clamped to {k} "
                        f"(OP sequence length {full_seqlen})")
        op_edges = op_edges[:k]
        logger.info(f"OP-sequence prefix: using {len(op_edges)} of "
                    f"{full_seqlen} edges")

    all_edges = set(hard) | set(op_edges)
    cliques = clique_cover(n, all_edges)
    big = [c for c in cliques if len(c) >= 2]
    sizes = {}
    for c in big:
        sizes[len(c)] = sizes.get(len(c), 0) + 1
    logger.info(f"Clique cover: {len(big)} cliques, size histogram {dict(sorted(sizes.items()))}")

    for c in big:
        model += Cumulative(
            start=[start[i] for i in c],
            duration=[int(p[i]) for i in c],
            end=[start[i] + int(p[i]) for i in c],
            demand=[1] * len(c),
            capacity=1,
        )
    logger.info(f"Posted {len(big)} disjunctive cliques")
    return model, {
        "hard": len(hard), "candidates": len(cands),
        "cheap_accept": n_acc, "cheap_reject": n_rej,
        "op_edges": len(op_edges), "cliques": len(big), "L": L, "U": U,
        "seqlen": full_seqlen,
    }

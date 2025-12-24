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


def collect_row_cover_sets(durations, A, b, cover_card, pool_size):
    covers = list()
    # For each value in A, precompute the indices where this value is encountered,
    # together with the longest segment in it
    inv_A = dict()
    inv_A_longest = dict()
    for ix, a in enumerate(A):
        if a not in inv_A:
            inv_A[a] = {ix}
            inv_A_longest[a] = ix
        else:
            inv_A[a].add(ix)
            if durations[ix] > inv_A_longest[a]:
                inv_A_longest[a] = ix
    # Add all binary covers
    for x, a in enumerate(A):
        lowest_val = b - a + 1
        for k, ys in inv_A.items():
            if k < lowest_val:
                continue
            for y in ys:
                if y > x:
                    covers.append(frozenset({x, y}))
    binary_covers = len(covers)
    logger.info(f"Added {binary_covers} binary covers")
    # For any pair of tasks, find another tasks to form a ternary cover,
    # and choose the longest one
    if cover_card >= 3:
        for x, ax in enumerate(A):
            for y, ay in enumerate(A[x + 1:], x + 1):
                rem = b - (ax + ay) + 1
                if rem <= 0:
                    continue
                for k, z in inv_A_longest.items():
                    if k < rem or z == x or z == y:
                        continue
                    covers.append(frozenset({x, y, z}))
        ternary_covers = len(covers) - binary_covers
        logger.info(f"Added {ternary_covers} ternary covers")
    # Choose the required number of covers, discarding the ones with
    # the worst elastic lower bound values
    covers.sort(key=lambda cover: -sum(durations[i] for i in cover) / (len(cover) - 1))
    covers = covers[:pool_size]
    return covers


def collect_cover_sets(durations, A, b, cover_card, pool_size):
    assert cover_card >= 2
    covers = set()
    for row_ix in range(len(A)):
        row_covers = collect_row_cover_sets(
            durations, A[row_ix, :], b[row_ix], cover_card, pool_size
        )
        logger.info(
            f"Received {len(row_covers)} covers for row #{row_ix+1}"
        )
        covers.update(row_covers)
    # Choose the required number of covers, discarding the ones with
    # the worst elastic lower bound values
    covers = list(covers)
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
        options={'presolve': False}
    )

    if not res.success:
        logger.error(f"Failed to solve the lifting subproblem for index {next_ix}, used indices {used_indices}, and LHS {lhs[used_indices]}\n")
        logger.trace(f"LHS matrix: {A[:, used_indices]}")
        logger.trace(f"RHS vector: {b - A[:, next_ix]}")
        logger.error(f"HiGHS error message: {res.message}")
        raise RuntimeError(res.message)
    opt = np.round(res.x)

    return int(lhs[used_indices] @ opt), opt


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
        max_lhs, opt = lifting_subproblem(A, b, lhs, next_ix, list(used_indices))
        if max_lhs > rhs:
            logger.trace(f"LHS matrix {A[:, list(used_indices)]}")
            logger.trace(f"RHS vector {b - A[:, next_ix]}")
            logger.trace(f"Objective weights {lhs[list(used_indices)]}")
            logger.trace(f"Reported optimal objective: {max_lhs}")
            logger.trace(f"Known upper bound: {rhs}")
            logger.trace(f"Reported optimizer: {opt}")
        lhs[next_ix] = rhs - max_lhs
        used_indices.add(next_ix)
    if np.any(lhs < 0):
        logger.warning(f"The inequality from the cover {cover} has "
                       f"{sum(lhs < 0)} negative coefficients")
        logger.trace(lhs)
    logger.debug(f"Lifted the cover set {cover} to an inequality with {np.count_nonzero(lhs)} nonzeros " +
                 f"and elastic lower bound of {sum(w * d for w, d in zip(lhs, durations)) / rhs}")
    return (lhs, rhs)


def process_cumulative_constraints(durations, A, b, cover_card, pool_size):
    cover_sets = collect_cover_sets(durations, A, b, cover_card, pool_size)
    logger.info(f"Received {len(cover_sets)} cover sets")
    visited_covers = list()
    cons = list()
    n_dom = 0
    n_skip = 0
    for cover in cover_sets:
        # assert np.any(np.sum(A[:, list(cover)], axis=1) > b), (A[:, list(cover)], b)
        if is_visited_cover(cover, visited_covers):
            n_skip += 1
            continue
        lhs, rhs = lift_cover(durations, A, b, cover)
        visited_covers.append((frozenset({i for i in range(A.shape[1]) if lhs[i] == 1}), len(cover)))
        is_dominated = False
        for ix in range(len(b)):
            if np.all(lhs <= A[ix, :]) and b[ix] <= rhs:
                n_dom += 1
                logger.debug(f"Lifted inequality from {cover} is dominated by row #{ix + 1}")
                is_dominated = True
                break
        if not is_dominated:
            cons.append((lhs, rhs))
    logger.info(f"Skipped {n_skip} cover sets by lifted constraints")
    logger.info(f"Skipped {n_dom} cover sets by dominance")
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

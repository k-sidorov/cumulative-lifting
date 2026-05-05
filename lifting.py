import heapq

from loguru import logger

from cpmpy.expressions.globalconstraints import Cumulative

import numpy as np

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


def collect_large_row_cover_sets(durations, A, b, cover_card, pool_size):
    covers = list()
    # Group tasks into their resource consumptions
    groups = dict()
    for ix, a in enumerate(A):
        if a == 0:
            continue
        if a not in groups:
            groups[a] = list()
        groups[a].append(ix)
    # Collect the longest-duration and the shortest-duration covers from each bucket
    for usage, group in groups.items():
        cover_size = int(np.ceil((b + 1) / usage))
        if cover_size <= 2 or cover_size > cover_card or cover_size > len(group):
            continue
        tasks = list(group)
        tasks.sort(key=lambda x: -durations[x])
        cover_long = set(tasks[:cover_size])
        assert sum(A[x] for x in cover_long) > b
        covers.append(cover_long)
        cover_short = set(tasks[-cover_size:])
        if cover_short != cover_long:
            assert sum(A[x] for x in cover_short) > b
            covers.append(cover_short)
    return covers


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
    if pool_size is not None:
        covers = covers[:pool_size]
    # Switch to the full cover set generation
    if cover_card >= 4:
        long_covers = collect_large_row_cover_sets(durations, A, b, cover_card, pool_size)
        covers += [frozenset(x) for x in long_covers]
        logger.info(f"Added {len(long_covers)} large covers")
        for long_cover in long_covers:
            long_cover_lb = sum(durations[i] for i in long_cover) / (len(long_cover) - 1)
            logger.debug(f"Long cover {long_cover} with lower bound {long_cover_lb:.3f}")
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
        # TODO check minimality
        covers.update(row_covers)
    # Choose the required number of covers, discarding the ones with
    # the worst elastic lower bound values
    covers = list(covers)
    covers.sort(key=lambda cover: -sum(durations[i] for i in cover) / (len(cover) - 1))
    covers = [c for c in covers if len(c) > 3] + covers[:pool_size]
    return covers


def is_visited_cover(cover, visited_covers):
    for vis_cover, min_card in visited_covers:
        if min_card <= len(cover) and cover <= vis_cover:
            return True
    return False


def lifting_subproblem(A, b, lhs, next_ix, used_indices, milp):
    # Maximize lhs.T * x w.r.t. A[:, used_indices] <= b - A[:, next_ix] and x binary
    A_sub = A[:, used_indices]
    b_sub = b - A[:, next_ix]
    c_sub = lhs[used_indices]
    # Return the subproblem objective unless the solver has failed
    try:
        opt = milp(A_sub, b_sub, c_sub)
        return int(lhs[used_indices] @ opt), opt
    except RuntimeError as e:
        logger.error(f"Failed to solve the lifting subproblem for index {next_ix}, used indices {used_indices}, and LHS {lhs[used_indices]}\n")
        raise e


def lift_cover(durations, A, b, cover, milp, worst_pool_bound: float = float('-inf'), remaining_calls=None):
    lhs = np.zeros((A.shape[1],), dtype=int)
    rhs = len(cover) - 1
    used_indices = set()
    n_calls = 0
    for ix in cover:
        lhs[ix] = 1
        used_indices.add(ix)
    remaining = sorted(
        (i for i in range(A.shape[1]) if i not in used_indices),
        key=lambda i: durations[i],
        reverse=True,
    )
    current_energy = np.dot(lhs, durations) / rhs
    remaining_energy = sum(durations[i] for i in remaining) / rhs
    for step, next_ix in enumerate(remaining):
        if remaining_calls is not None and n_calls >= remaining_calls:
            logger.info("Exhausted the MILP budget")
            break
        upper_bound_elb = current_energy + remaining_energy
        if upper_bound_elb <= worst_pool_bound:
            logger.debug(
                f"Early stop for cover {cover}: upper bound ELB {upper_bound_elb:.4f} "
                f"<= worst pool bound {worst_pool_bound:.4f}, "
                f"skipping {len(remaining) - step} MIP calls"
            )
            break
        max_lhs, opt = lifting_subproblem(A, b, lhs, next_ix, list(used_indices), milp)
        n_calls += 1
        if max_lhs > rhs:
            logger.trace(f"LHS matrix {A[:, list(used_indices)]}")
            logger.trace(f"RHS vector {b - A[:, next_ix]}")
            logger.trace(f"Objective weights {lhs[list(used_indices)]}")
            logger.trace(f"Reported optimal objective: {max_lhs}")
            logger.trace(f"Known upper bound: {rhs}")
            logger.trace(f"Reported optimizer: {opt}")
        lhs[next_ix] = rhs - max_lhs
        used_indices.add(next_ix)
        current_energy += lhs[next_ix] * durations[next_ix] / rhs
        remaining_energy -= durations[next_ix] / rhs
    if np.any(lhs < 0):
        logger.warning(f"The inequality from the cover {cover} has "
                       f"{sum(lhs < 0)} negative coefficients")
        logger.trace(lhs)
    if len(cover) <= 3:
        logger.debug(f"Lifted the cover set {cover} to an inequality with {np.count_nonzero(lhs)} nonzeros " +
                     f"and elastic lower bound of {sum(w * d for w, d in zip(lhs, durations)) / rhs}")
    else:
        logger.info(f"Lifted the cover set {cover} to an inequality with {np.count_nonzero(lhs)} nonzeros " +
                     f"and elastic lower bound of {sum(w * d for w, d in zip(lhs, durations)) / rhs}")
    return ((lhs, rhs), n_calls)


def process_cumulative_constraints(durations, A, b, cover_card, pool_size, max_lifting_calls, milp, max_cons: int):
    cover_sets = collect_cover_sets(durations, A, b, cover_card, pool_size)
    logger.info(f"Received {len(cover_sets)} cover sets")
    visited_covers = list()
    pool = []  # min-heap of (elb, uid, lhs, rhs); tracks best max_cons constraints
    n_dom = 0
    n_skip = 0
    n_calls = 0
    remaining_calls = None if max_lifting_calls is None else max_lifting_calls
    for cover in cover_sets:
        # assert np.any(np.sum(A[:, list(cover)], axis=1) > b), (A[:, list(cover)], b)
        if is_visited_cover(cover, visited_covers):
            n_skip += 1
            continue
        worst_pool_bound = pool[0][0] if len(pool) >= max_cons else float('-inf')
        (lhs, rhs), n_calls_cover = lift_cover(durations, A, b, cover, milp,
                                               worst_pool_bound=worst_pool_bound,
                                               remaining_calls=remaining_calls)
        n_calls += n_calls_cover
        if remaining_calls is not None:
            remaining_calls -= n_calls_cover
            if remaining_calls <= 0:
                break
        visited_covers.append((frozenset({i for i in range(A.shape[1]) if lhs[i] == 1}), len(cover)))
        is_dominated = False
        for ix in range(len(b)):
            if np.all(lhs <= A[ix, :]) and b[ix] <= rhs:
                n_dom += 1
                logger.debug(f"Lifted inequality from {cover} is dominated by row #{ix + 1}")
                is_dominated = True
                break
        if not is_dominated:
            elb = sum(w * d for w, d in zip(lhs, durations)) / rhs
            entry = (elb, id(lhs), lhs, rhs)
            if len(pool) < max_cons:
                heapq.heappush(pool, entry)
            elif elb > pool[0][0]:
                heapq.heapreplace(pool, entry)
    logger.info(f"Skipped {n_skip} cover sets by lifted constraints")
    logger.info(f"Skipped {n_dom} cover sets by dominance")
    logger.info(f"Solved {n_calls} lifting subproblems")
    return [(lhs, rhs) for _, _, lhs, rhs in sorted(pool, key=lambda x: -x[0])]


def run_lifting(model, max_cons, cover_card, pool_size, max_lifting_calls, milp):
    if max_cons == 0:
        return model.copy()
    intervals, A, b = extract_cumulative_matrix(model.constraints)
    durations = [d for _, _, d in intervals]
    cons = process_cumulative_constraints(durations, A, b, cover_card, pool_size, max_lifting_calls, milp, max_cons)
    logger.info(f'Received {len(cons)} candidate cumulative constraints')
    for ub in range(1, cover_card):
        n_cons = sum(1 if rhs == ub else 0 for _, rhs in cons)
        if n_cons > 0:
            logger.info(f'* {n_cons} with RHS = {ub}')
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

#!/usr/bin/env -S uv run --script --frozen --offline --project /home/ksidorov/cumulative-lifting

import argparse
from pathlib import Path
import time
import shlex
import subprocess
import sys

from cpmpy import *
from cpmpy.expressions.globalconstraints import Cumulative
from cpmpy.solvers.utils import SolverLookup

from loguru import logger

from parse_dzn import parse_dzn
from lifting import run_lifting
from milp import configure_milp_solver
from op_edges import add_op_edges

def solve_rcpsp_dzn(filename, flavor):
    if flavor not in {'std', 'max'}:
        logger.error(f"Cannot recognize flavor {flavor}`")
        raise ValueError(f"Cannot recognize flavor {flavor}`")
    data = parse_dzn(filename)

    # --- Extract data ---
    n_tasks = data["n_tasks"]
    n_res = data["n_res"]

    durations = cpm_array(data["d" if flavor == 'std' else "dur"])
    resource_caps = cpm_array(data["rc" if flavor == 'std' else 'rcap'])
    resource_needs = cpm_array(data["rr"])
    # 1-indexed!
    successors = data["suc" if flavor == 'std' else 'dcons']

    max_time = sum(durations)
    if flavor == 'max':
        for i in range(1, n_tasks + 1):
            max_lag = 0
            for p, lag, _ in successors:
                if p == i:
                    max_lag = max(max_lag, lag)
            max_time += max_lag

    # --- Construct base model ---
    model = Model()

    start = intvar(0, max_time, shape=n_tasks)

    # Precedence constraints
    if flavor == 'std':
        for i in range(n_tasks):
            for j in successors[i]:
                # MiniZinc is 1-indexed
                model += start[j - 1] >= start[i] + durations[i]
    else:
        for p, lag, q in successors:
            # 1-indexing of tasks
            model += start[q - 1] >= start[p - 1] + lag

    # Resource constraints
    for r in range(n_res):
        model += Cumulative(
            start=start,
            duration=durations,
            end=start + durations,
            demand=resource_needs[r, :],
            capacity=resource_caps[r],
        )

    # Objective
    makespan = max(start + durations)
    model.minimize(makespan)

    return model, start, data


def invoke_minizinc(fzn, time_limit, solver):
    if time_limit is not None and time_limit < 0:
        logger.warning("Timeout")
        sys.exit(1)
    mzn_args = ["minizinc", "-a", "-s"]
    if time_limit is not None:
        mzn_args += ["-t", str(time_limit)]
    mzn_args += ["--input-from-stdin", "--input-is-flatzinc"]
    mzn_args += ["--solver", solver]
    if args.solver_flags is not None:
        mzn_args += shlex.split(args.solver_flags)
    p = subprocess.Popen(mzn_args, stdin=subprocess.PIPE, text=True)
    p.communicate(input=fzn)


def main(args):
    start_time = time.perf_counter_ns()
    logger.info(f"Starting with command-line arguments {args}")
    base_model, start, data = solve_rcpsp_dzn(args.filename, args.flavor)
    logger.info("Generated the model for the input instance")
    solve_milp = configure_milp_solver(args.lifting_solver)
    model = run_lifting(base_model,
                        max_cons=args.max_cons,
                        cover_card=args.max_upper_bound + 1,
                        pool_size=args.cover_pool_size,
                        max_lifting_calls=args.max_lifting_calls,
                        milp=solve_milp)
    if args.op_edges:
        if args.flavor != 'std':
            logger.error("OP-disjoint edges are only implemented for the 'std' flavor")
            raise ValueError("OP-disjoint edges require flavor 'std'")
        cache_path = args.op_cache
        if cache_path == "auto":
            cache_path = str(Path(args.filename).with_suffix(".opseq.json"))
            logger.info(f"--op-cache auto resolved to {cache_path}")
        model, op_stats = add_op_edges(model, start, data,
                                       method=args.op_method,
                                       instance_path=args.filename,
                                       budget_s=args.op_budget,
                                       spreadfail_solver=args.op_spreadfail_solver,
                                       cache_path=cache_path,
                                       use_neighbors=args.op_neighbors,
                                       per_pair_limit=args.op_per_pair_limit,
                                       prefix=args.op_prefix,
                                       detect_only=args.op_detect_only)
        logger.info(f"OP-edge preprocessing stats: {op_stats}")
        seqlen = op_stats["seqlen"]
        cand = op_stats["candidates"]
        ratio = (seqlen / cand) if cand else 0.0
        logger.info(
            f"OP-SEQUENCE | n={data['n_tasks']} hard={op_stats['hard']} "
            f"candidates={cand} seqlen={seqlen} used={op_stats['op_edges']} "
            f"ratio={ratio:.4f}"
        )
        if args.op_detect_only:
            logger.info("Detect-only mode: sequence cached, skipping solve")
            return
    solver = SolverLookup.get(name=f'minizinc:{args.solver}', model=model)
    logger.info("Finished preprocessing, FlatZinc output pending")
    elapsed_time = time.perf_counter_ns() - start_time
    elapsed_time_ms = elapsed_time // (10 ** 6)
    logger.info(f"Preprocessing phase completed in {elapsed_time_ms} ms")
    remaining_time = None if args.time_limit is None else args.time_limit - elapsed_time_ms
    invoke_minizinc(solver.flatzinc_string(), remaining_time, args.solver)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog='cumulative-lifting',
        description='Runs the lifting preprocessing pass on RCPSP instances',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('flavor', choices=['std', 'max'],
                        help='Problem flavor')
    parser.add_argument('filename', type=Path, help='MiniZinc data file with the instance')
    parser.add_argument('--solver', type=str, default='cp-sat', help="MiniZinc target solver ID")
    parser.add_argument('--solver-flags', type=str, help="MiniZinc solver flags")
    parser.add_argument('--lifting-solver', type=str,
                        default='highs', choices=['scip', 'highs', 'gurobi'],
                        help="Integer programming solver for lifting subproblems")
    parser.add_argument('-t', '--time-limit', type=int,
                        help="Stop after the given time in milliseconds")
    parser.add_argument('-n', '--max-cons', type=int, default=5,
                        help="Maximum number of added cumulative constraints")
    parser.add_argument('-b', '--max-upper-bound', type=int, default=1,
                        help="Maximum capacity of the added constraints")
    parser.add_argument('-p', '--cover-pool-size', type=int, default=None,
                        help="Number of cover sets to be considered")
    parser.add_argument('-l', '--max-lifting-calls', type=int, default=None,
                        help="Maximum number of lifting subproblems to be solved")
    parser.add_argument('--op-edges', action='store_true',
                        help="Add disjunctive cliques from the conflict graph")
    parser.add_argument('--op-method',
                        choices=['none', 'oracle', 'spreadfail', 'spreadfree'],
                        default='none',
                        help="OP-edge detector: none (hard edges only); oracle "
                             "(ground-truth closure, needs T*); spreadfail "
                             "(reference-schedule closure, needs T*); spreadfree "
                             "(optimum-free all-schedules closure, native CPMpy)")
    parser.add_argument('--op-budget', type=int, default=300,
                        help="Wall-clock budget (s) for OP-edge detection")
    parser.add_argument('--op-spreadfail-solver', type=str, default='ortools',
                        help="CPMpy solver for the native SPREAD-FAIL per-pair checks "
                             "(ortools = in-process, fast; minizinc:pumpkin = subprocess)")
    parser.add_argument('--op-neighbors', action='store_true',
                        help="SPREAD-FAIL: also free resource-neighbours of the pair")
    parser.add_argument('--op-per-pair-limit', type=int, default=10,
                        help="Per-pair time limit (s) for SPREAD-FAIL checks")
    parser.add_argument('--op-cache', type=str, default=None,
                        help="JSON path to cache/load detected OP edges; "
                             "'auto' = <instance>.opseq.json next to the data file")
    parser.add_argument('--op-prefix', type=int, default=None,
                        help="Use only the first K edges of the (ordered) cached OP "
                             "sequence; clamped to its length. Requires a pre-built "
                             "--op-cache. Default: use the whole sequence.")
    parser.add_argument('--op-detect-only', action='store_true',
                        help="Detect the OP sequence, write the cache, print the "
                             "OP-SEQUENCE metric line, then exit without solving.")
    args = parser.parse_args()
    main(args)

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

    return model


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
    base_model = solve_rcpsp_dzn(args.filename, args.flavor)
    logger.info("Generated the model for the input instance")
    solve_milp = configure_milp_solver(args.lifting_solver)
    model = run_lifting(base_model,
                        max_cons=args.max_cons,
                        cover_card=args.max_upper_bound + 1,
                        pool_size=args.cover_pool_size,
                        milp=solve_milp)
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
    parser.add_argument('-p', '--cover-pool-size', type=int, default=1000,
                        help="Number of cover sets to be considered")
    args = parser.parse_args()
    main(args)

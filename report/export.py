#!/usr/bin/env python3
"""
Produces the CSV report of the lifting runs from a scut experiment tree.

A port of the gourd-era gourd/report/export.py (see commit ecc8a50), which produced the CSVs behind
the CP 2026 paper. The stdout/stderr parsing and both CSV layouts are kept as they were, so that
the corrigendum numbers differ from the paper's only through the runs themselves; the only change
to the parsing is splitting stdout on '\\n' (it was '/n') when reading UNSAT statistics.

What gourd's status CSV used to provide now comes from three places:
  * tasks.json, for the program, the input flavor and file, and the Slurm job id of each run;
  * the run directory written by scut's wrapper: argv.json (what was run, and when it started)
    and the final `meta` event in events/ (exit code, and the signal it was killed by, if any);
  * Slurm accounting (sacct), for the final job state, the node, and the peak memory.

Columns appended to the gourd-era layouts: `experiment` (the config's Slurm experiment name),
`lifting_behavior` (`fixed`, `cp26` or `cp26-appendix-c` for lifted runs, empty otherwise) and
`node`; the bounds CSV also gets `status`, `exit_code` and `preprocessing_completed`, so that runs
which never finished the lifting can be told apart from runs that found no constraint.

A run is reported once it has finished: scut's wrapper writes `outcome.json` when the program
exits, so a run without it is still running, unless Slurm says that its job has ended (then it
failed without the wrapper noticing, e.g. on a node failure). Runs still running are skipped.
"""

import argparse
from copy import deepcopy
import csv
import json
from pathlib import Path
import shlex
import subprocess
import sys


SACCT_FIELDS = ["JobID", "State", "Elapsed", "MaxRSS", "NodeList"]
SACCT_CHUNK = 200
# Slurm states of a job that is not over yet: its accounting is not final
ACTIVE_STATES = {"PENDING", "RUNNING", "REQUEUED", "RESIZING", "SUSPENDED", "CONFIGURING", "COMPLETING"}


def query_sacct(job_ids):
    out = []
    for i in range(0, len(job_ids), SACCT_CHUNK):
        cmd = ["sacct", "-j", ",".join(job_ids[i:i + SACCT_CHUNK]),
               f"--format={','.join(SACCT_FIELDS)}", "--parsable2", "--noheader"]
        r = subprocess.run(cmd, capture_output=True, text=True, check=True)
        out.append(r.stdout)
    return "".join(out)


def parse_elapsed(s):
    """Slurm's [D-]HH:MM:SS, in seconds."""
    days, _, hms = s.rpartition("-")
    h, m, sec = (int(x) for x in hms.split(":"))
    return (int(days) if days else 0) * 86400 + h * 3600 + m * 60 + sec


def parse_rss_kib(s):
    """Slurm's MaxRSS (e.g. 123456K, 1.5G), in KiB, the unit of getrusage's ru_maxrss."""
    if not s:
        return None
    scale = {"K": 1, "M": 1024, "G": 1024 ** 2, "T": 1024 ** 3}
    if s[-1] in scale:
        return int(float(s[:-1]) * scale[s[-1]])
    return int(s) // 1024


def parse_sacct(text):
    """JobID of an array task -> its state, elapsed time, node and peak memory.

    The state and node are on the row of the array task itself; MaxRSS is only on its steps
    (<id>.batch, <id>.extern), so the maximum over the steps is taken.
    """
    acct = dict()
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) < len(SACCT_FIELDS):
            continue
        job_id, state, elapsed, max_rss, node = parts[:5]
        base, _, step = job_id.partition(".")
        entry = acct.setdefault(base, {"state": None, "elapsed": None, "node": None, "max_rss": None})
        if step:
            rss = parse_rss_kib(max_rss)
            if rss is not None and (entry["max_rss"] is None or rss > entry["max_rss"]):
                entry["max_rss"] = rss
        else:
            # "CANCELLED by <uid>" -> "CANCELLED"
            entry["state"] = state.split()[0] if state else None
            entry["elapsed"] = parse_elapsed(elapsed) if elapsed else None
            entry["node"] = node
    return acct


def load_sacct(tasks, sacct_file):
    """Slurm accounting of every task, from the cache in `sacct_file` where it is final.

    The cache only answers for array tasks whose jobs had ended when it was written: a task that
    is missing from it, or was still active then, is queried again, and the cache is rewritten.
    Without sacct, the cache is used as it is.
    """
    cached = sacct_file.read_text() if sacct_file is not None and sacct_file.exists() else ""
    acct = parse_sacct(cached)
    task_ids = {t["slurm_job_id"] for t in tasks if t.get("slurm_job_id")}
    stale = sorted({tid.split("_")[0] for tid in task_ids
                    if acct.get(tid, {}).get("state") in (None, *ACTIVE_STATES)})
    if not stale:
        return acct
    try:
        text = query_sacct(stale)
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        print(f"sacct unavailable ({e}); the accounting of {len(stale)} job(s) is not final "
              f"or missing, so their states, nodes and peak memory may be empty", file=sys.stderr)
        return acct
    # Fresh rows go last, so that they replace the cached rows of the same tasks
    cached_rows = [line for line in cached.splitlines()
                   if line.split("|")[0].split(".")[0].split("_")[0] not in stale]
    merged = "\n".join(cached_rows + text.splitlines()) + "\n"
    if sacct_file is not None:
        sacct_file.write_text(merged)
    return parse_sacct(merged)


def read_outcome(run_dir):
    """scut's `outcome.json`: written atomically once the program has exited; None before."""
    try:
        return json.loads((run_dir / "outcome.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def final_meta(run_dir):
    """The wrapper's last `meta` event of the latest attempt, or None if it never exited cleanly."""
    files = sorted((run_dir / "events").glob("events-*.jsonl"),
                   key=lambda p: int(p.stem.split("-")[1]))
    if not files:
        return None
    meta = None
    with files[-1].open() as f:
        for line in f:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "meta" and "exit_code" in event:
                meta = event
    return meta


def solver_flags(argv):
    """The value of --solver-flags, whether passed as one token or two."""
    for key, value in zip(argv, argv[1:] + [None]):
        if key == "--solver-flags":
            return value or ""
        if key.startswith("--solver-flags="):
            return key.split("=", 1)[1]
    return ""


def option(argv, name):
    for key, value in zip(argv, argv[1:]):
        if key == name:
            return value
    return None


def lifting_behavior(argv):
    """main.py's --cp26-lifting-behavior [main|appendix-c], as the name of the lifting behavior."""
    names = {"main": "cp26", "appendix-c": "cp26-appendix-c"}
    for key, value in zip(argv, argv[1:] + [None]):
        if key == "--cp26-lifting-behavior":
            return names.get(value, "cp26")
        if key.startswith("--cp26-lifting-behavior="):
            return names[key.split("=", 1)[1]]
    return "fixed"


def describe_run(task, argv):
    """The labels gourd/report/export.py derived from gourd's program name and input arguments."""
    if Path(argv[0]).name == "rcpsp":
        solver = "unl"
        search_direction = "dual"
    else:
        solver = option(argv, "--solver")
        flags = shlex.split(solver_flags(argv))
        dual = ("linear-unsat-sat" in flags) if solver == "pumpkin" else ("-v" in flags)
        search_direction = "dual" if dual else "primal"
    n = option(argv, "-n")
    lift = solver != "unl" and int(n if n is not None else 5) > 0  # 5 is the default of main.py -n
    *_, collection, instance = task["file"].split("/")
    return {
        "solver": solver,
        "search_direction": search_direction,
        "lift": lift,
        "lifting_behavior": lifting_behavior(argv) if lift else None,
        "problem": {"max": "rcpsp-max", "std": "rcpsp"}[task["kind"]],
        "collection": collection,
        "instance": instance,
    }


def run_status(outcome, meta, acct):
    """Gourd's run status: ok, timeout, memout or failed; None while the run is pending or running.

    A run that exited by itself is `ok` whatever its exit code, as with gourd: main.py exits with 1
    when the preprocessing used up the time limit, and Slurm records that as FAILED. Slurm's
    TIMEOUT and OUT_OF_MEMORY take precedence, since the wrapper also sees the kill as an exit.

    Whether the run is over comes from scut's `outcome.json` (or, for runs from before it, the
    wrapper's final `meta` event) together with Slurm: with neither, the run is still going, even
    when Slurm has no record of it yet. A job that Slurm reports as ended without an outcome failed.
    """
    state = acct.get("state")
    if state in ACTIVE_STATES:
        return None
    if state == "OUT_OF_MEMORY":
        return "memout"
    if state == "TIMEOUT":
        return "timeout"
    if outcome is not None:
        return "ok" if outcome.get("signal") is None else ("timeout" if state is None else "failed")
    if meta is not None:
        if "killed_by" in meta:
            return "timeout" if state is None else "failed"
        return "ok"
    return None if state is None else "failed"


def base_row_for(task, experiment, acct_by_job):
    run_dir = Path(task["run_dir"])
    if not (run_dir / "argv.json").exists():
        return None  # never started
    argv = json.loads((run_dir / "argv.json").read_text())
    outcome = read_outcome(run_dir)
    meta = final_meta(run_dir)
    acct = acct_by_job.get(task.get("slurm_job_id") or "", {})
    status = run_status(outcome, meta, acct)
    if status is None:
        return None
    row = {"experiment": experiment, **describe_run(task, argv), "status": status, "node": acct.get("node")}
    row["exit_code"] = (outcome["exit_code"] if outcome is not None
                        else meta["exit_code"] if meta is not None else None)
    if status == "ok":
        # argv.json is written right before the program starts; outcome.json (or, for runs from
        # before it, the final meta event) right after it exits
        started = (run_dir / "argv.json").stat().st_mtime
        if outcome is not None and "finished_at" in outcome:
            row["full_time"] = outcome["finished_at"] - started
        elif meta is not None and "_ts" in meta:
            row["full_time"] = meta["_ts"] - started
        else:
            row["full_time"] = acct.get("elapsed")
        row["peak_memory"] = acct.get("max_rss")
    return row


def parse_mzn(base_row, stdout):
    mzn_segments = list()
    rows = list()
    status = base_row['status']
    if status == 'unknown':
        # Status unknown, do not write any solver-specific statistics
        rows = [base_row]
    elif status == 'unsat':
        # UNSAT, report the stats (time, conflicts, propagations) at UNSAT verdict
        unsat_row = deepcopy(base_row)
        reading = False
        for line in stdout.split('\n'):
            if "UNSAT" in line:
                reading = True
                continue
            if reading and "%%%mzn-stat: " in line:
                k, v = line.split(": ")[1].split("=")
                if k == 'solveTime':
                    unsat_row['time'] = float(v) + unsat_row['preprocessing_time']
                elif k == 'failures':
                    unsat_row['conflicts'] = int(v)
                elif k == 'propagations':
                    unsat_row['propagations'] = int(v)
        rows = [unsat_row]
    else:
        # SAT, report the stats (time, conflicts, propagations) at every bound change
        if "use_node_packing: true" in stdout:
            stdout = stdout.replace("%%%mzn-stat-end", "")
        blocks = [
            block
            for raw_block in stdout.split("----------")
            for block in raw_block.split("%%%mzn-stat-end")
        ]
        for block in blocks:
            row = deepcopy(base_row)
            for line in block.split('\n'):
                if "%%%mzn-stat: " in line:
                    k, v = line.split(": ")[1].split("=")
                    if k == 'solveTime':
                        row['time'] = float(v) + row['preprocessing_time']
                    elif k == 'engineStatisticsTimeSpentInSolver':
                        row['time'] = 1e-3 * int(v)
                    elif k in {'failures', 'engineStatisticsNumConflicts'}:
                        row['conflicts'] = int(v)
                    elif k in {'propagations', 'engineStatisticsNumPropagations'}:
                        row['propagations'] = int(v)
                    elif k == 'objective':
                        row['objective'] = int(v)
                    elif k == 'objectiveBound':
                        row['bound'] = int(v)
                elif "Attempting to find solution with assumption" in line:
                    row['bound'] = int(line.split('<=')[1].replace(']', '').strip())
            if "objective" in row or "bound" in row:
                last_obj = None if len(rows) == 0 else rows[-1].get('objective')
                last_bound = None if len(rows) == 0 else rows[-1].get('bound')
                if last_obj != row.get('objective') or last_bound != row.get('bound'):
                    if last_bound is not None and 'bound' not in row:
                        row['bound'] = last_bound
                    rows.append(row)
    return rows


def parse_cpsat_verbose(base_row, stdout):
    if "=====UNSATISFIABLE=====" in stdout:
        _, unsat_block = stdout.split("=====UNSATISFIABLE=====")
        assert base_row['instance_status'] == 'unsat', base_row
        row = deepcopy(base_row)
        for line in stdout.split('\n'):
            if "%%%mzn-stat: " in line:
                k, v = line.split(": ")[1].split("=")
                if k == 'solveTime':
                    row['time'] = float(v) + row['preprocessing_time']
                elif k == 'failures':
                    row['conflicts'] = int(v)
                elif k == 'propagations':
                    row['propagations'] = int(v)
        return [row]
    rows = list()
    for line in stdout.split('\n'):
        row = None
        if "%% #Bound" in line:
            row = deepcopy(base_row)
            for token in line.split():
                if "next:" in token:
                    lb, _ = token.replace("next:", "").strip()[1:-1].split(",")
                    lb = int(lb)
                    row["bound"] = lb
                elif token.endswith("s"):
                    row["time"] = float(token[:-1]) + row['preprocessing_time']
        elif "%% #" in line and "best:" in line:
            assert "%% #1" in line, (line, base_row)
            row = deepcopy(base_row)
            for token in line.split():
                if "best:" in token:
                    opt = int(token.replace("best:", ""))
                    row["objective"] = opt
                    row["bound"] = opt
                elif token.endswith("s"):
                    row["time"] = float(token[:-1]) + row['preprocessing_time']
        if row is not None:
            rows.append(row)
    return rows


def main(args):
    meta = json.loads((args.experiment_dir / "tasks.json").read_text())
    experiment = meta["slurm"]["experiment_name"]
    tasks = meta["tasks"]
    acct_by_job = load_sacct(tasks, args.sacct_file)
    with args.output_file.open("w") as csvfile, args.bounds_output_file.open("w") as boundfile:
        writer = csv.DictWriter(
            csvfile,
            fieldnames=[
                'solver',
                'search_direction',
                'lift',
                'problem',
                'collection',
                'instance',
                'status',
                'exit_code',
                'full_time',
                'peak_memory',
                'instance_status',
                'objective',
                'bound',
                'time',
                'preprocessing_time',
                'conflicts',
                'propagations',
                'experiment',
                'lifting_behavior',
                'node',
            ],
            extrasaction='ignore',
        )
        writer.writeheader()
        bound_writer = csv.DictWriter(
            boundfile,
            fieldnames=[
                'solver',
                'search_direction',
                'problem',
                'collection',
                'instance',
                'preprocessing_time',
                'n_candidate_constraints',
                'n_lifting_subproblems',
                'n_added_cumulative',
                'n_added_disjunctive',
                'elastic_lower_bound',
                'best_lhs',
                'best_rhs',
                'experiment',
                'lifting_behavior',
                'node',
                'status',
                'exit_code',
                'preprocessing_completed',
            ],
            extrasaction='ignore',
        )
        bound_writer.writeheader()
        for index, task in enumerate(tasks):
            base_row = base_row_for(task, experiment, acct_by_job)
            if base_row is None:
                continue
            run_dir = Path(task["run_dir"])
            base_row['n_added_cumulative'] = 0
            base_row['n_added_disjunctive'] = 0
            base_row['elastic_lower_bound'] = 0.0
            base_row['best_lhs'] = None
            base_row['best_rhs'] = None
            base_row['preprocessing_completed'] = False
            # Read preprocessing time from stderr
            with (run_dir / "stderr").open() as f:
                current_lhs = None
                current_rhs = None
                for line in f:
                    if 'Preprocessing phase completed in' in line:
                        base_row['preprocessing_time'] = 1e-3 * int(line.split()[-2])
                        base_row['preprocessing_completed'] = True
                    elif "lifting subproblems" in line:
                        base_row['n_lifting_subproblems'] = int(line.split()[-3])
                    elif "candidate cumulative constraints" in line:
                        base_row['n_candidate_constraints'] = int(line.split()[-4])
                    elif '≤' in line:
                        base_row['n_added_cumulative'] += 1
                        if line.split('≤')[1].strip() == '1':
                            base_row['n_added_disjunctive'] += 1
                        current_lhs, current_rhs = line.split('≤')
                        current_rhs = int(current_rhs)
                        *_, current_lhs = current_lhs.split(' | ')
                        _, current_lhs = current_lhs.split(' - ')
                        current_lhs = [
                            [
                                int(term.replace('[', '').replace(']', ''))
                                for term in token.split('*')
                            ]
                            for token in current_lhs.split(' + ')
                        ]
                    elif 'Elastic lower bound' in line:
                        lb = float(line.split(': ')[-1].strip())
                        if lb > base_row['elastic_lower_bound']:
                            base_row['elastic_lower_bound'] = lb
                            base_row['best_lhs'] = current_lhs
                            base_row['best_rhs'] = json.dumps(current_rhs)
            # Write all objective claims in stdout
            rows = list()
            # --- Determine if the problem is UNSAT and write the last row accordingly
            status = 'sat'
            with (run_dir / "stdout").open() as f:
                for line in f:
                    if "=UNSAT" in line:
                        status = 'unsat'
                        break
                    elif "=UNKNOWN" in line:
                        status = 'unknown'
                        break
            if status == 'unknown':
                # CP-SAT dual search reports UNKNOWN when it fails to discover a solution;
                # process the output
                if base_row['solver'] == 'cp-sat' and base_row['search_direction'] == 'dual':
                    status = 'sat'
            base_row['instance_status'] = status

            if base_row['solver'] == 'cp-sat' and base_row['search_direction'] == 'dual':
                rows = parse_cpsat_verbose(
                    deepcopy(base_row),
                    (run_dir / "stdout").read_text()
                )
            else:
                rows = parse_mzn(
                    deepcopy(base_row),
                    (run_dir / "stdout").read_text()
                )

            print(f'Parsed {index} out of {len(tasks)}', end='\r')
            for row in rows:
                writer.writerow(row)
            if base_row['lift']:
                bound_writer.writerow(base_row)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Produces the CSV report of the lifting runs")
    parser.add_argument(
        "experiment_dir",
        type=Path,
        help="scut experiment directory holding tasks.json, e.g. <workspace>/experiments/latest",
    )
    parser.add_argument(
        "--output-file",
        "-o",
        type=Path,
        required=True,
        help="CSV file with the report table",
    )
    parser.add_argument(
        "--bounds-output-file",
        "-b",
        type=Path,
        required=True,
        help="CSV file with the best lifted cuts",
    )
    parser.add_argument(
        "--sacct-file",
        type=Path,
        default=None,
        help="Cache of the raw sacct output: read if it exists, otherwise written after querying sacct",
    )
    args = parser.parse_args()
    main(args)

#!/usr/bin/env python3

import argparse
from pathlib import Path
from copy import deepcopy
import csv
import os
import subprocess


def parse_duration(gourd_dur_string: str) -> float:
    if gourd_dur_string.endswith("s"):
        return float(gourd_dur_string.replace("s", ""))
    elif "N/A" in gourd_dur_string:
        return None
    assert False, gourd_dur_string


def parse_gourd_report(path):
    rows = dict()
    with path.open() as csvfile:
        reader = csv.DictReader(csvfile)
        for csv_row in reader:
            args = (csv_row["input args"]
                    .replace('"', '')
                    .replace("[", "")
                    .replace("]", "")
                    .split(', '))
            assert csv_row['program'].endswith('-lb') or csv_row['program'].endswith('-ub'), csv_row
            solver = csv_row['program'].replace('-lb', '').replace('-ub', '')
            search_direction = 'dual' if csv_row['program'].endswith('-lb') else 'primal'
            lift = any(p == '-n' and int(n) > 0 for p, n in zip(args[:-1], args[1:]))
            problem = 'rcpsp-max' if 'max' in args else ('rcpsp' if 'std' in args else None)
            *_, path = args
            *_, collection, instance = path.split('/')
            row = None
            if "job finished" in csv_row["slurm"]:
                row = {
                    'solver': solver,
                    'search_direction': search_direction,
                    'lift': lift,
                    'problem': problem,
                    'collection': collection,
                    'instance': instance,
                    'status': 'ok',
                    'exit_code': int(csv_row['exit']) if csv_row['exit'] != 'N/A' else None,
                    'full_time': parse_duration(csv_row['wall time']),
                    'peak_memory': int(csv_row['max rss']) if csv_row['max rss'] != 'N/A' else None,
                }
            elif "running" in csv_row['slurm'] or "pending" in csv_row['slurm']:
                continue
            else:
                row = {
                    'solver': solver,
                    'search_direction': search_direction,
                    'lift': lift,
                    'problem': problem,
                    'collection': collection,
                    'instance': instance,
                    'status': (
                        'timeout' if "timed out" in csv_row['slurm'] else
                        'memout' if "out of memory" in csv_row['slurm'] else None
                    ),
                    'exit_code': int(csv_row['exit']) if csv_row['exit'] != 'N/A' else None,
                }
            assert row['status'] is not None, (row, csv_row)
            rows[csv_row['run id']] = row
    return rows

def main(args):
    in_rows = parse_gourd_report(args.input_file)
    with open(args.output_file, "w") as csvfile:
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
                'preprocessing_time',
                'n_added_cumulative',
                'n_added_disjunctive',
                'elastic_lower_bound',
                'instance_status',
                'objective',
                'bound',
                'time',
                'conflicts',
                'propagations'
            ]
        )
        writer.writeheader()
        runs = [
            run_dir
            for solver_dir in args.logdir.iterdir()
            for run_dir in solver_dir.iterdir()
        ]
        for index, run_dir in enumerate(runs):
            # TODO remove this continue
            if run_dir.name not in in_rows:
                continue
            base_row = in_rows[run_dir.name]
            base_row['n_added_cumulative'] = 0
            base_row['n_added_disjunctive'] = 0
            base_row['elastic_lower_bound'] = 0.0
            # Read preprocessing time from stderr
            with (run_dir / "stderr").open() as f:
                for line in f:
                    if 'Preprocessing phase completed in' in line:
                        base_row['preprocessing_time'] = 1e-3 * int(line.split()[-2])
                    elif '≤' in line:
                        base_row['n_added_cumulative'] += 1
                        if line.split('≤')[1].strip() == '1':
                            base_row['n_added_disjunctive'] += 1
                    elif 'Elastic lower bound' in line:
                        base_row['elastic_lower_bound'] = max(
                            base_row['elastic_lower_bound'],
                            float(line.split(': ')[-1].strip())
                        )
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
            base_row['instance_status'] = status
            mzn_segments = list()
            if status == 'unknown':
                # Status unknown, do not write any solver-specific statistics
                rows = [deepcopy(base_row)]
            elif status == 'unsat':
                # UNSAT, report the stats (time, conflicts, propagations) at UNSAT verdict
                unsat_row = deepcopy(base_row)
                reading = False
                with (run_dir / "stdout").open() as f:
                    for line in f:
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
                blocks = [
                    block
                    for raw_block in (run_dir / "stdout").read_text().split("----------")
                    for block in raw_block.split("%%%mzn-stat-end")
                ]
                for block in blocks:
                    row = deepcopy(base_row)
                    for line in block.split('\n'):
                        if "%%%mzn-stat: " in line:
                            k, v = line.split(": ")[1].split("=")
                            if k == 'solveTime':
                                row['time'] = float(v) + row['preprocessing_time']
                            elif k == 'failures':
                                row['conflicts'] = int(v)
                            elif k == 'propagations':
                                row['propagations'] = int(v)
                            elif k == 'objective':
                                row['objective'] = int(v)
                            elif k == 'objectiveBound':
                                row['bound'] = int(v)
                    if "objective" in row or "bound" in row:
                        last_obj = None if len(rows) == 0 else rows[-1].get('objective')
                        last_bound = None if len(rows) == 0 else rows[-1].get('bound')
                        if last_obj != row.get('objective') or last_bound != row.get('bound'):
                            rows.append(row)
            print(f'Parsed {index} out of {len(runs)}', end='\r')
            for row in rows:
                writer.writerow(row)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Produces the CSV report of the lifting runs")
    parser.add_argument(
        "logdir",
        type=Path,
        help="Directory with lifting script outputs",
    )
    parser.add_argument(
        "--input-file",
        "-i",
        type=Path,
        required=True,
        help="CSV file with the Gourd report file"
    )
    parser.add_argument(
        "--output-file",
        "-o",
        type=Path,
        required=True,
        help="CSV file with the report table",
    )
    args = parser.parse_args()
    main(args)

#!/usr/bin/env python3

import json
import csv
from pathlib import Path


def load_events(events_dir: Path):
    files = list(events_dir.glob("events-*.jsonl"))
    if not files:
        return

    latest = max(files, key=lambda p: p.stat().st_mtime)

    with open(latest) as fh:
        for line in fh:
            try:
                yield json.loads(line)
            except json.decoder.JSONDecodeError:
                print(f"{latest} has a non-JSON line {line.strip()}, skipping")


def process_run(run_dir: Path):
    argv_path = run_dir / "argv.json"
    events_dir = run_dir / "events"

    if not argv_path.exists() or not events_dir.exists():
        return None

    argv = json.loads(argv_path.read_text())
    if "--op-prefix" not in argv:
        return
    prefix = int(argv[argv.index("--op-prefix") + 1])
    seed = int(argv[argv.index("--solver-seed") + 1]) if "--solver-seed" in argv else None
    # The instance path is the trailing positional (flavor + filename).
    path = argv[-1]
    *_, instance = path.split("/")

    opt_event = {"instance": instance, "prefix": prefix, "seed": seed}

    for ev in load_events(events_dir):
        if "objective" not in ev or "bound" not in ev:
            continue
        if ev["objective"] != ev["bound"]:
            continue
        opt_event = {**opt_event, **ev}

    return opt_event


def main(root: Path, out_csv: Path):
    with open(out_csv, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "instance", "prefix", "seed", "time", "n_failures", "n_propagations"
            ],
            extrasaction='ignore'
        )
        writer.writeheader()

        for program_dir in sorted(root.iterdir()):
            for run_dir in sorted(program_dir.iterdir()):
                if not run_dir.is_dir():
                    continue

                row = process_run(run_dir)
                if row:
                    writer.writerow(row)
                    f.flush()



if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("root")
    p.add_argument("--out", default="results.solve.csv")

    args = p.parse_args()

    main(Path(args.root), Path(args.out))

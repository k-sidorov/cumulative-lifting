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
    *_, path = argv
    *_, instance = path.split("/")

    start_time, finish_time, n_used, n_possible, ratio = None, None, None, None, None

    for ev in load_events(events_dir):
        t = ev.get("_ts")

        if ev["type"] == "start":
            start_time = t

        elif ev["type"] == "end":
            finish_time = t
            n_used = ev["used"]
            n_possible = ev["candidates"]
            ratio = ev["ratio"]

    duration = None if (start_time is None or finish_time is None) else finish_time - start_time

    return {
        "instance": instance,
        "duration": duration,
        "n_used": n_used,
        "n_possible": n_possible,
        "possible_ratio": ratio
    }


def main(root: Path, out_csv: Path):
    rows = []

    for program_dir in sorted(root.iterdir()):
        for run_dir in sorted(program_dir.iterdir()):
            if not run_dir.is_dir():
                continue

            row = process_run(run_dir)
            if row:
                rows.append(row)

    with open(out_csv, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "instance", "duration", "n_used", "n_possible", "possible_ratio"
            ]
        )
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("root")
    p.add_argument("--out", default="results.detect.csv")

    args = p.parse_args()

    main(Path(args.root), Path(args.out))

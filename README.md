# Cumulative lifting preprocessor

This repository implements the lifting-based preprocessing described in the “On inferring cumulative constraints” paper. It automatically discovers and injects valid global `cumulative` constraints into RCPSP models to strengthen lower bounds and improve solver performance.

## Prerequisites

* Python 3.11+.
* [`uv`](https://docs.astral.sh/uv/) for Python package and environment management.
* MiniZinc (installed and available in your `PATH`).

## Installation

`uv` will automatically handle all Python dependencies upon the first run. To ensure your environment is ready:

```bash
uv sync
```

⚠️ By default, the project uses the HiGHS solver for the lifting subproblems. To use Gurobi, ensure it is installed and licensed on your system, and prepare the environment with `uv sync --all-extras`. ⚠️

## Usage

The entry point is `main.py`. It takes a problem flavor (`std` for RCPSP or `max` for RCPSP/max) and a data file path.

### Command-line arguments

* `-n`: Maximum number of inferred constraints to add (default: 5).
* `-b`: Maximum capacity of the added constraints.
* `-p`: Size of the pool of short covers to be considered for lifting.
* `--lifting-solver`: The IP solver used for the subproblems (`highs`, `scip`, or `gurobi`).
* `--solver`: The MiniZinc ID of the target CP solver (`cp-sat`, `pumpkin`, `chuffed`, etc.).

## Reproducing experimental results

Below are the configurations used for the benchmarks in the paper. These commands use various solvers and search strategies (primal vs. dual) to evaluate the impact of the lifted constraints.

### CP-SAT

Primal search with strong propagation enabled:

```bash
uv run main.py --solver cp-sat \
  --solver-flags "-f --params \"use_strong_propagation_in_disjunctive:true use_overload_checker_in_cumulative:true\"" \
  -b 1000 -p 100 -n 5 \
 <flavor> <data_file>
```

To shift focus to lower-bound improvement using MaxSAT-style core optimization, run the dual search as follows:

```bash
uv run main.py --solver cp-sat \
  --solver-flags "-f -v --params \"optimize_with_core:true use_strong_propagation_in_disjunctive:true use_overload_checker_in_cumulative:true\"" \
  -b 1000 -p 100 -n 5 \
 <flavor> <data_file>
```

### Pumpkin

Primal search:

```bash
uv run main.py --solver pumpkin \
  --solver-flags "-f" \
  -b 1000 -p 100 -n 5 \
   <flavor> <data_file>
```

Dual search is achieved by passing the `--optimisation-strategy` argument to Pumpkin:

```bash
uv run main.py --solver pumpkin \
  --solver-flags "-f --optimisation-strategy linear-unsat-sat" \
  -b 1000 -p 100 -n 5 \
   <flavor> <data_file>
```

## How this works

The pipeline follows three main stages:

1. **Discovery** identifies promising subsets of tasks (covers) that conflict for one of the resource constraints.
2. **Lifting** formulates and solves a series of integer programs (using the solved requested by `--lifting-solver`) to maximize the coefficients of the newly constructed occupancy-vector inequality.
3. **Injection** translates the valid inequality back into a `cumulative` global constraint and appends it to the model before handing it off to the target CP solver.

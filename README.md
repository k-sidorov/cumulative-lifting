# Cumulative lifting preprocessor

This repository implements the lifting-based preprocessing described in the “[On inferring cumulative constraints](https://arxiv.org/abs/2602.15635)” paper. It automatically discovers and injects valid global `cumulative` constraints into RCPSP models to strengthen lower bounds and improve solver performance.

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
* `-l`: Maximum number of lifting subproblems to be solved.
* `--lifting-solver`: The IP solver used for the subproblems (`highs`, `scip`, or `gurobi`).
* `--solver`: The MiniZinc ID of the target CP solver (`cp-sat`, `pumpkin`, `chuffed`, etc.).
* `--cp26-lifting-behavior [main|appendix-c]`: Recover the lifting behavior of the CP 2026 paper: `main` (the default when the flag is given) for the main-text results, `appendix-c` for Appendix C (see [Corrigendum](#corrigendum-to-the-cp-2026-paper)).

## Corrigendum to the CP 2026 paper

Version 0.3.0 corrects three issues behind the results reported in the paper:

1. **Max-duration representative.** When collecting covers, the longest task among those with equal demand was chosen by comparing a duration to a task *index* instead of to that task's duration. This affects all lifting results of the paper.
2. **ELB early stop.** While lifting a cover, the remaining energy in the upper bound on the elastic lower bound was divided by the right-hand side. This underestimates the bound unless the right-hand side is one, so lifting could stop before the best constraint was found. The early stop is not described in the paper, and it only ran in the experiments of Appendix C.
3. **Lifting solver in the UNL comparison.** The comparison with Pumpkin's disjointness extension used HiGHS for the lifting subproblems instead of Gurobi, which was used in all other experiments.

The paper text has one further error: Algorithm 2 (step L3) lifts the task with the shortest duration first, whereas the implementation, as intended, lifts the longest first.

The paper's results come from two versions of the code:

| Results | Commit | Lifting | Flag |
|---|---|---|---|
| Main text (the UNL comparison: presumably, as its configuration dates from this version) | `4125e99` | legacy representative, no early stop | `--cp26-lifting-behavior` (or `=main`) |
| Appendix C (call budget `-l`) | `ecc8a50` (v0.2.0) | legacy representative, early stop with the underestimated bound | `--cp26-lifting-behavior=appendix-c` |

Without the flag, the lifting uses both fixes. Rerunning the lifting of `4125e99` reproduces the constraints behind the paper's main-text results exactly; the flag reproduces those of each version, except that ties between constraints with equal elastic lower bounds are now broken by discovery order (earlier first), where `ecc8a50` broke them by memory address and so was not deterministic. The [gourd](https://github.com/ConSol-Lab/gourd) configurations that produced the paper's results are at commit `ecc8a50`; the data is on Zenodo ([10.5281/zenodo.18663551](https://doi.org/10.5281/zenodo.18663551)).

## Reproducing experimental results

The experiments run on Slurm with scut, which orders tasks instance by instance so that all configurations for an instance tend to run on the same node. Install it from its repository with `uv tool install`, and prepare the environment with `uv sync --all-extras` (the experiments use Gurobi for the lifting subproblems). The shebang of `main.py` points `uv` at the checkout location on the cluster; adjust it if yours differs.

There is one configuration per experiment of the paper in `slurm/configs/`:

| Configuration | Experiment | Placeholders |
|---|---|---|
| `scheduling.toml` | Lifting on RCPSP and RCPSP/max | `rcpsp`, `rcpsp_max` |
| `scheduling-budgeted.toml` | Lifting with at most 20000 subproblems (`-l`) on large RCPSP/max instances | `rcpsp_max_large` |
| `unl.toml` | Comparison with Pumpkin's disjointness extension | `rcpsp`, `rcpsp_max`, `unl_binary` |

Every configuration runs each instance without lifting (`base-*`), with the corrected lifting (`lift-*`) and with the paper's lifting (`lift-cp26-*`: the main-text lifting, or in `scheduling-budgeted.toml` that of Appendix C), for each solver configuration; the header of each file lists the exact arms. All of them also take `pumpkin_repo` and `pumpkin_commit` (the Pumpkin checkout that MiniZinc runs, and the commit it must be at; `unl.toml` additionally takes `unl_repo` and `unl_commit`, the checkout `unl_binary` is built from and its commit), `cpsat_binary` (except `unl.toml`), `workspace` (where the results go; use a different one per configuration), `project_dir` (this checkout) and `glob` (the instances, relative to each data directory):

```bash
scut build slurm/configs/scheduling.toml \
  -Dworkspace=<workspace> -Dproject_dir=<checkout> \
  -Drcpsp=<data>/rcpsp -Drcpsp_max=<data>/rcpsp-max \
  -Dpumpkin_repo=<pumpkin checkout> -Dpumpkin_commit=<commit> \
  -Dcpsat_binary=<or-tools>/bin/fzn-cp-sat \
  -Dglob='**/*.dzn'
scut plan     # prints one fully resolved command line
scut submit   # re-run until the backlog is drained, as it respects the submission quota
```

Each configuration declares the solvers, MiniZinc and this checkout as dependencies. `scut build` fingerprints them (commit, binary hash, `minizinc --solvers-json`) and `scut submit` refuses to run once any of them has changed, so all runs of a campaign use the same builds. `scut plan` prints what was pinned. The checkout must be clean: commit or stash changes before building.

To run a pilot first, build the same configuration with a narrower pattern such as `-Dglob='**/*1.dzn'`. Keep a leading wildcard, so that the runs are named the same way as in the full campaign.

Once the runs are done, `report/export.py` collects them into the same CSV layouts as those used for the paper, plus the columns `experiment`, `lifting_behavior` and `node`:

```bash
uv run report/export.py <workspace>/experiments/latest \
  -o runs.csv -b bounds.csv --sacct-file sacct.txt
```

It has to run where `sacct` is available the first time; `--sacct-file` keeps the Slurm accounting for later runs of the script.

The solver configurations used in the experiments are:

* CP-SAT, primal: `--solver cp-sat --solver-flags "-f --params \"use_strong_propagation_in_disjunctive:true use_overload_checker_in_cumulative:true\""`
* CP-SAT, dual: `--solver cp-sat --solver-flags "-v --params \"optimize_with_core:true use_strong_propagation_in_disjunctive:true use_overload_checker_in_cumulative:true\""`
* Pumpkin, primal: `--solver pumpkin --solver-flags=-f`
* Pumpkin, dual: `--solver pumpkin --solver-flags "-f --optimisation-strategy linear-unsat-sat"`

with `--lifting-solver gurobi -b 1000 -p 100 -n 5 -t 3600000` for lifting, and `-n 0` without it.

## How this works

The pipeline follows three main stages:

1. **Discovery** identifies promising subsets of tasks (covers) that conflict for one of the resource constraints.
2. **Lifting** formulates and solves a series of integer programs (using the solved requested by `--lifting-solver`) to maximize the coefficients of the newly constructed occupancy-vector inequality.
3. **Injection** translates the valid inequality back into a `cumulative` global constraint and appends it to the model before handing it off to the target CP solver.

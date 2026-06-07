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

## Optimality-preserving disjunctive edges (`--op-edges`)

This branch adds a second, independent preprocessing pass: discovery of **optimality-preserving
(OP) disjunctions**. The idea, complementary to the valid-`cumulative` lifting above:

* Build a **conflict (disjointness) graph** over tasks, seeded with the **hard** pairs — every
  pair `(i, j)` whose demands exceed some resource capacity (`r[i,k] + r[j,k] > C_k`), so they
  physically cannot overlap. This is the graph used by the Unite-and-Lead approach.
* Optionally extend it with **OP edges**: pairs that *can* overlap (no resource conflict, no
  precedence between them) but whose `disjunctive` constraint nonetheless preserves the optimal
  makespan — i.e. some optimal schedule already runs them disjointly. These are *not* implied
  constraints; they cut feasible-but-suboptimal schedules while provably keeping an optimum.
* **Clique-cover** the resulting graph and post each clique as a unit-capacity `Cumulative`
  (equivalently a `disjunctive`), which gives the target solver much stronger
  energetic/disjunctive propagation than the per-resource `cumulative`s alone.

Every posted clique is optimality-preserving by construction, so **the optimal objective is
unchanged** — always sanity-check this (the reported `objective` should match the instance's
known optimum).

### Quick start

```bash
# hard-edge cliques only (no OP detection; needs no optimum; cheap)
uv run main.py -n 0 --op-edges --op-method none --solver pumpkin std <data_file>

# + OP edges via the reference-schedule closure (needs the optimum; fast)
uv run main.py -n 0 --op-edges --op-method spreadfail --solver pumpkin std <data_file>
```

`-n 0` disables the `cumulative` lifting so the effect of the OP cliques is isolated (it also
makes the base solve slower, which is convenient for seeing a difference).

### Detection methods (`--op-method`)

| value        | needs optimum? | cost            | what it does |
|--------------|----------------|-----------------|--------------|
| `none`       | no             | negligible      | hard edges only (resource-conflict pairs). The UnL-style baseline. |
| `spreadfail` | **yes** (T\*)  | low (~s)        | Reference-schedule closure: compute one optimal schedule `S*`, then for each candidate check (natively, in CPMpy + OR-Tools) whether re-timing its scope can keep makespan `= T*` with the pair disjoint; commit it and adopt the witness as the new reference. Composition-aware ⇒ the committed set is *jointly* OP. |
| `oracle`     | **yes** (T\*)  | high (a solve/pair) | Ground-truth incremental closure: for each candidate, solve `model + committed + disjoint(a,b)` to optimality and commit iff the optimum is still `T*`. Complete relative to the candidate order; mainly a **debugging / ceiling** tool. |
| `spreadfree` | **no**         | very high (~min/pair) | Optimum-free closure: for each candidate run the all-schedules SPREAD-FAIL *dominance* check (native CPMpy port of `separable_ext.mzn`, with committed edges folded in); `UNSAT ⇒ OP`. Uses only a feasible incumbent as a search horizon, never the optimum. This is the "how much is knowable without solving" experiment — sound but slow. |

`spreadfail` and `oracle` both call OR-Tools to obtain `T*` (and, for `spreadfail`, the
reference schedule), so they are "optimum-conditioned": useful for *certified / fast-proof*
acceleration (find `T*` with any solver, then let the LCG solver prove optimality quickly on the
augmented model), not as a standalone primal speedup. `spreadfree` is the only optimum-free
variant; it is the right tool for measuring the detection ceiling, not for production.

### All OP-related flags

| flag | default | meaning |
|------|---------|---------|
| `--op-edges` | off | enable the conflict-graph pass (hard cliques + whatever `--op-method` finds) |
| `--op-method {none,oracle,spreadfail,spreadfree}` | `none` | OP-edge detector (see table above) |
| `--op-budget <seconds>` | 300 | wall-clock budget for OP-edge **detection** (the closure stops early when exceeded; partial results are still sound) |
| `--op-per-pair-limit <seconds>` | 10 | per-candidate solve/limit for `spreadfail`/`spreadfree` |
| `--op-spreadfail-solver <id>` | `ortools` | CPMpy solver for the per-pair checks. **Keep `ortools`** — it is in-process and ~100× faster per call than `minizinc:pumpkin`, which subprocess-flattens every check. |
| `--op-neighbors` | off | `spreadfail`/`spreadfree`: also free the resource-neighbours of the pair (wider repair scope). Helps on *sparse* instances; on dense ones it makes each per-pair model nearly a full re-solve and usually finds *fewer* edges within the per-pair limit — leave off unless the instance is sparse. |
| `--op-cache <path.json>` | none | load OP edges from this JSON if it exists, otherwise compute and save them. Detection is then paid **once** per instance. |

### Caching and the time budget (important for batch runs)

Two practical gotchas when scripting validation runs:

1. **`-t` is the *total* budget** (preprocessing **+** solve). If detection overruns `-t`, the
   solve gets a negative time limit and is skipped. For the expensive detectors either pass a
   large `-t`, or (better) precompute the cache.
2. **Always cache.** Build the OP-edge set once, then re-run solves cheaply (the cache loads
   instantly) under different target solvers / flags:

```bash
# 1) build the cache once (large -t so detection completes; budget caps detection time)
uv run main.py -n 0 -t 700000 --op-edges --op-method spreadfail \
  --op-budget 600 --op-cache /tmp/<inst>_op.json --solver pumpkin std <data_file>

# 2) reuse it for fast, fair comparisons (cache load is instant; -t is now the solve budget)
uv run main.py -n 0 -t 60000 --op-edges --op-method spreadfail \
  --op-cache /tmp/<inst>_op.json --solver pumpkin std <data_file>     # + OP cliques
uv run main.py -n 0 -t 60000 --op-edges --op-method none \
  --solver pumpkin std <data_file>                                    # hard cliques only
uv run main.py -n 0 -t 60000 --solver pumpkin std <data_file>         # baseline
```

The optimum-free ceiling run looks the same with `--op-method spreadfree` (give it a generous
`--op-budget`, e.g. `3600`, and expect tens of seconds per pair):

```bash
uv run main.py -n 0 -t 5000000 --op-edges --op-method spreadfree \
  --op-budget 3600 --op-per-pair-limit 60 --op-cache /tmp/<inst>_free.json \
  --solver pumpkin std <data_file>
```

### Reading the output

Detection logs (at `LOGURU_LEVEL=INFO`) report, in order:

* `Conflict graph: <H> hard pairs, <K> overlap-candidates`
* the detector's tally, e.g. `composition closure: <N> jointly-OP edges …` /
  `spreadfree closure (native, ub=…): <N> jointly-OP edges; <r> refuted, <u> inconclusive …`
* `Clique cover: <C> cliques, size histogram {…}`
* `OP-edge preprocessing stats: {…}`

Then the usual MiniZinc statistics (`objective`, `objectiveBound`, `nodes`, `failures`,
`solveTime`). Optimality is **proved** when `objective == objectiveBound`; confirm `objective`
equals the instance's known optimum to be sure the OP edges were sound.

### Relevant files

* `op_edges.py` — conflict-graph construction, the four detectors, clique cover, and posting.
* `separable.mzn` — the scope-`{a,b}` all-schedules SPREAD-FAIL dominance check (`UNSAT ⇒` the
  pair is OP). Kept for reference / standalone use; the `spreadfree` detector uses a native
  CPMpy port of its extended version.
* `separable_ext.mzn` — the same check extended with already-committed disjunctions (the
  "extended formulation" that makes the optimum-free closure composition-aware).

### Notes for the J30 validation pass

* Per instance: build a `spreadfail` cache (fast, optimum-conditioned) for the "what do OP
  cliques buy" question, and optionally a `spreadfree` cache (slow, optimum-free) for the
  "what is knowable without solving" ceiling. Then compare baseline / `none` / `spreadfail`
  under a fixed solve budget using the cached edges.
* Keep `--op-spreadfail-solver ortools`; reserve `pumpkin` for the *target* solve (`--solver`).
* Leave `--op-neighbors` off for J30 (dense); revisit it only on sparse/structured instances.
* `oracle` and `spreadfail` should agree on whether an instance is "cracked"; if they diverge,
  `oracle` is the ground truth (it does full per-pair solves), so use it to debug `spreadfail`.

## How this works

The pipeline follows three main stages:

1. **Discovery** identifies promising subsets of tasks (covers) that conflict for one of the resource constraints.
2. **Lifting** formulates and solves a series of integer programs (using the solved requested by `--lifting-solver`) to maximize the coefficients of the newly constructed occupancy-vector inequality.
3. **Injection** translates the valid inequality back into a `cumulative` global constraint and appends it to the model before handing it off to the target CP solver.

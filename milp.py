from loguru import logger
import numpy as np

def configure_milp_solver(solver_name):
    """
    Returns a function solving 0-1 integer programs.

    The inputs of the function:
        - Left-hand side matrix A: np.array
        - Right-hand side vector b: np.array

        - Maximization vector c: np.array

    The only output is the np.array encoding the optimum
    """
    if solver_name == 'highs':
        return solve_with_highs
    elif solver_name == 'scip':
        return solve_with_scip
    elif solver_name == 'gurobi':
        return solve_with_gurobi
    else:
        raise LookupError(f"Unknown solver name {solver_name}")


def solve_with_highs(A, b, c):
    from scipy.optimize import milp, LinearConstraint, Bounds
    res = milp(
        # SciPy minimizes, so negate objective
        c=-c,
        constraints=LinearConstraint(A, -np.inf, b),
        bounds=Bounds(0, 1),
        integrality=np.ones(len(c), dtype=int), # 1 = integer variable
        options={'presolve': False} # https://github.com/scipy/scipy/issues/18907
    )

    if not res.success:
        logger.trace(f"LHS matrix: {A}")
        logger.trace(f"RHS vector: {b}")
        logger.trace(f"Objective: {c}")
        logger.trace(f"HiGHS error message: {res.message}")
        raise RuntimeError(res.message)

    return np.round(res.x)


def solve_with_scip(A, b, c):
    from pyscipopt import Model, quicksum

    n_vars = len(c)
    n_cons = len(b)

    model = Model("binary_milp")

    # SCIP silencing
    model.setIntParam("display/verblevel", 0)
    model.setIntParam("display/freq", -1)
    model.setLogfile(None)

    # Binary variables
    x = [
        model.addVar(lb=0.0, ub=1.0, vtype="B", name=f"x{i}")
        for i in range(n_vars)
    ]

    # Constraints: A.T @ x <= b
    for i in range(n_cons):
        model.addCons(
            quicksum(A[i, j] * x[j] for j in range(n_vars)) <= b[i],
            name=f"c{i}",
        )

    # Objective: maximize c @ x
    model.setObjective(
        quicksum(c[j] * x[j] for j in range(n_vars)),
        sense="maximize",
    )

    model.optimize()

    # Handle the non-optimal case
    status = model.getStatus()
    if status != "optimal":
        logger.trace(f"LHS matrix: {A}")
        logger.trace(f"RHS vector: {b}")
        logger.trace(f"Objective: {c}")
        logger.trace(f"SCIP status: {status}")
        raise RuntimeError(f"SCIP did not find an optimal solution (status={status})")

    # Extract solution
    sol = np.array([model.getVal(x[j]) for j in range(n_vars)])
    return np.round(sol)


def solve_with_gurobi(A, b, c):
    try:
        import gurobipy as gp
        from gurobipy import GRB
    except ImportError as e:
        raise ImportError(
            "Gurobi is not installed. Install optional dependency or set solver_name!='gurobi'."
        ) from e

    n_vars = len(c)

    model = gp.Model("binary_milp")

    # Silence output completely
    model.Params.OutputFlag = 0
    model.Params.LogToConsole = 0

    # Determinism & stability
    model.Params.Threads = 1
    model.Params.Seed = 0

    # Root-node–focused tuning (important for your case)
    model.Params.Presolve = 2          # aggressive presolve
    model.Params.Cuts = 2              # moderate cuts
    model.Params.Heuristics = 0.05     # light heuristics
    model.Params.MIPFocus = 1          # focus on optimality / bounds

    # Numerical robustness (knapsack-friendly)
    model.Params.NumericFocus = 1

    # Variables: x_j ∈ {0,1}
    x = model.addMVar(
        shape=n_vars,
        vtype=GRB.BINARY,
        name="x",
    )

    # Constraints: A @ x <= b
    model.addConstr(A @ x <= b, name="knapsack")

    # Objective: maximize c @ x
    model.setObjective(c @ x, GRB.MAXIMIZE)

    model.optimize()

    status = model.Status
    if status != GRB.OPTIMAL:
        logger.trace(f"LHS matrix: {A}")
        logger.trace(f"RHS vector: {b}")
        logger.trace(f"Objective: {c}")
        logger.trace(f"Gurobi status code: {status}")
        raise RuntimeError(f"Gurobi did not find an optimal solution (status={status})")

    return np.round(x.X)

class ParserState:
    def __init__(self):
        self.time = None
        self.n_failures = None
        self.n_props = None
        self.objective = None
        self.bound = None

    def probe(self):
        for v in [self.time, self.n_failures, self.n_props, self.objective, self.bound]:
            if v is None:
                return None
        row = {
            "time": self.time,
            "n_failures": self.n_failures,
            "n_propagations": self.n_props,
            "objective": self.objective,
            "bound": self.bound
        }
        self.time = None
        self.n_failures = None
        self.n_props = None
        self.objective = None
        self.bound = None
        return row


def on_start(ctx):
    ctx.state = ParserState()
    ctx.emit({
        "type": "start",
        "parser": "detect",
        "parser_version": "1.0"
    })


def on_stderr(ctx, line):
    pass


def on_stdout(ctx, line):
    if line.startswith("%%%mzn-stat: "):
        k, v = line.strip().replace("%%%mzn-stat: ", "").split("=")
        if k == "solveTime":
            v = float(v)
        else:
            try:
                v = int(v)
            except ValueError:
                pass
        if k == "objective":
            ctx.state.objective = v
        elif k == "objectiveBound":
            ctx.state.bound = v
        elif k == "failures":
            ctx.state.n_failures = v
        elif k == "propagations":
            ctx.state.n_props = v
        elif k == "solveTime":
            ctx.state.time = v
        else:
            return
        row = ctx.state.probe()
        if row is not None:
            ctx.emit({**row, **{"type": "bound-change"}})


def on_exit(ctx, code):
    ctx.emit({
        "type": "exit",
        "exit_code": code,
    })

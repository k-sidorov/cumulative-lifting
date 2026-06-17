def on_start(ctx):
    ctx.emit({
        "type": "start",
        "parser": "detect",
        "parser_version": "1.0"
    })


def on_stderr(ctx, line):
    if "OP-SEQUENCE" in line:
        _, tokens = line.split("OP-SEQUENCE | ")
        tokens = [x.split("=") for x in tokens.split()]
        tokens = {k: int(v) if k != "ratio" else float(v) for k, v in tokens}
        tokens["type"] = "end"
        ctx.emit(tokens)


def on_stdout(ctx, line):
    # Not used for this solver, but keep hook for symmetry
    pass


def on_exit(ctx, code):
    ctx.emit({
        "type": "exit",
        "exit_code": code,
    })

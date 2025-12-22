import re
import ast


def _parse_value(val):
    val = val.strip()

    # 2D array: [| row | row |]
    if val.startswith("[|"):
        inner = val[2:-1].strip()   # remove [| and ]
        rows = []

        for row in inner.split("|"):
            row = row.strip()
            if not row:
                continue
            rows.append(ast.literal_eval("[" + row + "]"))

        return rows

    # 1D array or set
    if val.startswith("["):
        return ast.literal_eval(val)

    # scalar
    return ast.literal_eval(val)


def parse_dzn(filename):
    data = {}

    with open(filename, "r") as f:
        text = f.read()

    # Remove comments
    text = re.sub(r"%.*", "", text)

    # Split entries
    entries = [e.strip() for e in text.split(";") if e.strip()]

    for entry in entries:
        name, val = entry.split("=", 1)
        name = name.strip()
        val = val.strip()

        data[name] = _parse_value(val)

    return data


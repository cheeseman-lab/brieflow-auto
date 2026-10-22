#!/usr/bin/env python3
"""Check that a restructured marimo notebook defines what the original did.

    compare_notebooks.py OLD.py NEW.py [--allow-changed NAME ...]

Compares, from each notebook's marimo graph and source:
  * every name the notebook defines (cell defs), so no variable is lost or added by accident;
  * every uppercase operator parameter and its literal default;
  * the config-write cell (the cell that assigns ``config[...]``), source normalized by ast.
Exit 0 when they match (apart from names given with --allow-changed), 1 otherwise.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path


def graph(path: Path):
    from marimo._ast.app import InternalApp
    from marimo._ast.load import load_app

    return InternalApp(load_app(str(path)))


def defs_and_params(path: Path) -> tuple[set[str], dict[str, str], list[str]]:
    """(all defs, {UPPERCASE name: ast-dump of its literal default}, config-cell sources)."""
    app = graph(path)
    defs: set[str] = set()
    params: dict[str, str] = {}
    config_cells: list[str] = []
    for cd in app.cell_manager.cell_data():
        if cd.cell is None:
            continue
        defs |= set(cd.cell.defs)
        tree = ast.parse(cd.code)
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                t = node.targets[0]
                if isinstance(t, ast.Name) and t.id.isupper() and t.id in cd.cell.defs:
                    try:
                        ast.literal_eval(node.value)
                    except ValueError:
                        continue
                    params[t.id] = ast.dump(node.value)
        if any(
            isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Subscript) and getattr(t.value, "id", "") == "config"
                    for t in n.targets)
            for n in ast.walk(tree)
        ):
            config_cells.append(ast.dump(tree))
    return defs, params, config_cells


def compare(old: Path, new: Path, allowed: set[str]) -> list[str]:
    d0, p0, c0 = defs_and_params(old)
    d1, p1, c1 = defs_and_params(new)
    problems = []
    lost, added = sorted(d0 - d1 - allowed), sorted(d1 - d0 - allowed)
    if lost:
        problems.append(f"defs lost: {lost}")
    if added:
        problems.append(f"defs added: {added}")
    for name in sorted((set(p0) | set(p1)) - allowed):
        if p0.get(name) != p1.get(name):
            problems.append(f"parameter {name}: default changed or moved out of a literal assignment")
    if sorted(c0) != sorted(c1):
        problems.append("config-write cell(s) differ")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("old", type=Path)
    ap.add_argument("new", type=Path)
    ap.add_argument("--allow-changed", nargs="*", default=[])
    a = ap.parse_args(argv)
    problems = compare(a.old, a.new, set(a.allow_changed))
    for p in problems:
        print(f"{a.new.name}: {p}")
    if not problems:
        print(f"{a.new.name}: equivalent to {a.old.name}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

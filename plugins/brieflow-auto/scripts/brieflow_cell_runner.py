#!/usr/bin/env python3
"""brieflow_cell_runner.py — headless sweep of one tile that pays the upstream once.

The headless fallback used to run the whole phase notebook per (value, tile): imports,
image loading, alignment and segmentation again for a parameter that only changes the read
calling at the bottom (~14 min per value on one screen). This runs the notebook's cells in
one process instead:

1. Export the marimo notebook to a flat script (`marimo export script`) and split it into
   cells; read each cell's top-level definitions and references (marimo's dataflow rule:
   one definition per name, `_`-prefixed names are cell-private).
2. Run every cell once (the priming pass), capturing each cell's output. Cells whose output
   carries the sweep metrics, and the cells they depend on, are the cells a value needs.
3. Per trial, rewrite the swept marker assignment(s) and rerun only the cells downstream
   of the changed marker that the metric needs; plots and other display cells are skipped.

Writes one TSV row per trial (the parameters, then `run_marimo_sweep`'s columns) and one
`<out>.<n>.log` per trial with the captured output. Run inside the screen's environment:

    python brieflow_cell_runner.py --notebook copy.py --cwd <analysis-dir> \\
        --trials '[{"THRESHOLD_READS": 200}, {"THRESHOLD_READS": 400}]' --out band.tsv
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import re
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CELL_SEP = "\n# %%\n"
METRIC_RE = re.compile(r"SINGLE_MAP_RATE|ANY_MAP_RATE|_residual_max_px|_frac:|_cv:|_solidity:")


def split_cells(flat: str) -> list[str]:
    return [c for c in flat.split(CELL_SEP) if c.strip()]


def cell_names(code: str) -> tuple[set[str], set[str]]:
    """(defs, refs): top-level names a cell binds and the names it reads, without the
    cell-private `_` names and without anything inside function or class bodies."""
    tree = ast.parse(code)
    defs: set[str] = set()
    refs: set[str] = set()

    def visit(node, top: bool):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if top:
                defs.add(node.name)
            for dec in getattr(node, "decorator_list", []):
                visit(dec, False)
            # Free names only: a function's own arguments and locals are not references.
            local = {a.arg for a in ast.walk(node) if isinstance(a, ast.arg)}
            local |= {n.id for n in ast.walk(node)
                      if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
            refs.update(n.id for n in ast.walk(node)
                        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                        and n.id not in local)
            return
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                defs.add((alias.asname or alias.name).split(".")[0])
            return
        if isinstance(node, ast.Name):
            (defs if isinstance(node.ctx, ast.Store) else refs).add(node.id)
        for child in ast.iter_child_nodes(node):
            visit(child, top and isinstance(node, (ast.Module, ast.If, ast.Try, ast.With, ast.For)))

    visit(tree, True)
    public = lambda names: {n for n in names if not n.startswith("_")}  # noqa: E731
    return public(defs), public(refs) - public(defs)


def dependents(cells_names: list[tuple[set, set]], changed: set[int], names: set[str]) -> list[int]:
    """`changed` cells plus every cell downstream of the changed `names`, in notebook order.
    Only the swept names count as changed: the marker cell's other definitions keep their
    values when it reruns."""
    out, dirty = [], set(names)
    for i, (defs, refs) in enumerate(cells_names):
        if i in changed:
            out.append(i)
        elif refs & dirty:
            out.append(i)
            dirty |= defs
    return out


def ancestors(cells_names: list[tuple[set, set]], targets: set[int]) -> set[int]:
    """`targets` plus every cell whose definitions they need, transitively."""
    owner = {name: i for i, (defs, _) in enumerate(cells_names) for name in defs}
    need, todo = set(targets), list(targets)
    while todo:
        for name in cells_names[todo.pop()][1]:
            i = owner.get(name)
            if i is not None and i not in need:
                need.add(i)
                todo.append(i)
    return need


def set_assignment(code: str, name: str, value) -> str:
    pattern = re.compile(rf"^(\s*){re.escape(name)}\s*=.*$", re.M)
    if not pattern.search(code):
        raise KeyError(name)
    return pattern.sub(lambda m: f"{m.group(1)}{name} = {value!r}  # sweep trial", code, count=1)


class CellRunner:
    """Executes notebook cells in one namespace, capturing each cell's output."""

    def __init__(self, cells: list[str]):
        self.cells = list(cells)
        self.names = [cell_names(c) for c in self.cells]
        self.ns: dict = {"__name__": "__main__"}
        self.required: set[int] = set(range(len(self.cells)))

    def run(self, indices) -> tuple[str, dict[int, str]]:
        per_cell: dict[int, str] = {}
        for i in indices:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                try:
                    exec(compile(self.cells[i], f"cell{i}", "exec"), self.ns)
                except Exception as e:  # a value that leaves no reads is data, not a crash
                    print(f"cell {i} error: {type(e).__name__}: {e}")
            per_cell[i] = buf.getvalue()
        return "".join(per_cell.values()), per_cell

    def prime(self) -> str:
        """Run every cell once; keep only metric cells and their ancestors for trials."""
        text, per_cell = self.run(range(len(self.cells)))
        metric = {i for i, out in per_cell.items() if METRIC_RE.search(out)}
        if metric:
            self.required = ancestors(self.names, metric)
        return text

    def trial(self, assignments: dict) -> str:
        changed = set()
        for name, value in assignments.items():
            idx = next(i for i, c in enumerate(self.cells)
                       if re.search(rf"^\s*{re.escape(name)}\s*=", c, re.M))
            self.cells[idx] = set_assignment(self.cells[idx], name, value)
            self.names[idx] = cell_names(self.cells[idx])
            changed.add(idx)
        downstream = dependents(self.names, changed, set(assignments))
        todo = [i for i in downstream if i in self.required or i in changed]
        text, _ = self.run(todo)
        return text


def _flat_cells(notebook: Path) -> list[str]:
    flat = notebook.with_suffix(".flat.py")
    subprocess.run([sys.executable, "-m", "marimo", "export", "script", str(notebook), "-o",
                    str(flat)], check=True, capture_output=True)
    return split_cells(flat.read_text())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notebook", type=Path, required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--trials", required=True, help="JSON list of {param: value}")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(SCRIPT_DIR))
    import os

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from brieflow_sweep import _parse_mapping_rates, parse_qc_block

    plt.show = lambda *a, **k: plt.close("all")
    cells = _flat_cells(args.notebook.resolve())
    os.chdir(args.cwd)
    runner = CellRunner(cells)
    t0 = time.time()
    baseline_text = runner.prime()
    (args.out.parent / f"{args.out.stem}.prime.log").write_text(baseline_text)
    print(f"PRIMED {time.time() - t0:.1f}s cells={len(cells)} needed={len(runner.required)}",
          flush=True)
    trials = json.loads(args.trials)
    params = list(dict.fromkeys(k for t in trials for k in t))
    baseline = parse_qc_block(baseline_text)
    with open(args.out, "w") as out:
        out.write("\t".join(params + ["single_rate", "any_rate", "qc_pass", "elapsed_s"]) + "\n")
        for n, assignments in enumerate(trials):
            t1 = time.time()
            text = runner.trial(assignments)
            elapsed = time.time() - t1
            (args.out.parent / f"{args.out.stem}.{n}.log").write_text(text)
            single, any_ = _parse_mapping_rates(text)
            qc = parse_qc_block(text, baseline=baseline)
            row = [assignments.get(p) for p in params] + [single, any_, qc["qc_pass"],
                                                           round(elapsed, 1)]
            out.write("\t".join("" if v is None else str(v) for v in row) + "\n")
            out.flush()
            print(f"TRIAL {n} {assignments} single={single} {elapsed:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

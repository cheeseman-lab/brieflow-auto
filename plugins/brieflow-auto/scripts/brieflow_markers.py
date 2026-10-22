#!/usr/bin/env python3
"""brieflow_markers.py — operator-parameter block scan + patch.

Operator parameters live in ``params_*`` cells (plain assignments only) in current
notebooks, and in comment-fenced OPERATOR PARAMETERS blocks in older ones; both count
as marker blocks here.

The marimo-run contract layer: scan marker blocks, verify coverage, detect
variable collisions, patch a marker-block value (pre/post-kernel only), and
locate the marimo cell holding a marker. CONTRACT-FROZEN — case-insensitive
matching, suffixed-header support, and the "only valid before
start_marimo_kernel / after stop_marimo_kernel" rule must not change. Pure
stdlib (ast/re) + the bucket set; re-exported from brieflow_wizard.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Iterable


# ---------------------------------------------------------------------------
# Marker-block coverage scan (notebook-template defect detection)
# ---------------------------------------------------------------------------


# Marker matchers — allow optional descriptive suffix on either marker so the
# canonical can write `# === OPERATOR PARAMETERS (POSITIONS APPROACH) ===`
# and the END line stays the canonical short form.
_MARKER_OPEN_RE = re.compile(
    r"^#\s*=+\s*OPERATOR\s+PARAMETERS\b.*?=+\s*$", re.MULTILINE
)
_MARKER_CLOSE_RE = re.compile(
    r"^#\s*=+\s*END\s+OPERATOR\s+PARAMETERS\b.*?=+\s*$", re.MULTILINE
)


PARAMS_CELL_PREFIX = "params"


def params_cell_spans(source: str) -> list[tuple[int, int]]:
    """(first, last) 1-based lines of the assignments in each parameter cell.

    A parameter cell is an ``@app.cell`` function named ``params`` or ``params_<topic>``
    whose body is plain assignments followed by its ``return``; the span covers the
    assignments, so it plays the role a comment-fenced marker block plays in older notebooks."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    spans = []
    for node in tree.body:
        if not (isinstance(node, ast.FunctionDef) and _is_app_cell(node)):
            continue
        if node.name != PARAMS_CELL_PREFIX and not node.name.startswith(PARAMS_CELL_PREFIX + "_"):
            continue
        body = [n for n in node.body if not isinstance(n, ast.Return)]
        if body:
            spans.append((body[0].lineno, max(n.end_lineno or n.lineno for n in body)))
    return spans


def _is_app_cell(node: ast.FunctionDef) -> bool:
    return any(
        (isinstance(d, ast.Attribute) and d.attr == "cell")
        or (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "cell")
        for d in node.decorator_list
    )


def scan_marker_blocks(notebook_path: str | Path) -> list[tuple[int, int]]:
    """Return (first_line, last_line) tuples (1-based) for each block where operator
    parameters live: comment-fenced OPERATOR PARAMETERS blocks (older notebooks) and the
    assignments of ``params_*`` cells (current notebooks), in file order. Raises if a
    fenced block is unterminated. Accepts an optional descriptive suffix on the open
    marker (e.g., `(POSITIONS APPROACH)`)."""
    text = Path(notebook_path).read_text()
    return sorted(_fenced_blocks(notebook_path, text) + params_cell_spans(text))


def _fenced_blocks(notebook_path, text: str) -> list[tuple[int, int]]:
    lines = text.splitlines()
    blocks: list[tuple[int, int]] = []
    open_at: int | None = None
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if _MARKER_CLOSE_RE.match(stripped):
            if open_at is None:
                raise ValueError(
                    f"{notebook_path}: stray END OPERATOR PARAMETERS at line {i} (no matching open)"
                )
            blocks.append((open_at, i))
            open_at = None
        elif _MARKER_OPEN_RE.match(stripped):
            if open_at is not None:
                raise ValueError(
                    f"{notebook_path}: nested OPERATOR PARAMETERS open at line {i} "
                    f"(previous open at {open_at} never closed)"
                )
            open_at = i
    if open_at is not None:
        raise ValueError(
            f"{notebook_path}: OPERATOR PARAMETERS opened at line {open_at} but never closed"
        )
    return blocks


def _assignment_span(notebook_path, var_name, blocks):
    """Return (start_line, end_line, indent, matched_name) 1-based for the single
    in-marker top-level assignment of `var_name`, using AST end_lineno so
    multi-line RHS literals are spanned. None if not found. Case-insensitive."""
    src = Path(notebook_path).read_text()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    lines = src.splitlines()
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Name) and tgt.id.lower() == var_name.lower():
                start, end = node.lineno, (node.end_lineno or node.lineno)
                if any(o <= start <= c for o, c in blocks):
                    indent = lines[start - 1][: len(lines[start - 1]) - len(lines[start - 1].lstrip())]
                    hits.append((start, end, indent, tgt.id))
    if len(hits) != 1:
        return None
    return hits[0]


def _assignment_lines(notebook_path: str | Path, var_name: str) -> list[int]:
    """Return 1-based line numbers where `var_name` is the target of an actual
    Python assignment (e.g., `var_name = ...`). AST-based: kwarg literals in
    function calls (`f(var_name=...)`), attribute writes (`obj.var_name = ...`),
    and string occurrences are excluded by construction.

    Matches case-insensitively so the CSV's lowercase config keys (e.g.
    `threshold_peaks`) align with the notebook's uppercase Python variables
    (`THRESHOLD_PEAKS`). Falls back to a regex scan if the file isn't valid
    Python (defensive — shouldn't happen on canonical marimo notebooks)."""
    src = Path(notebook_path).read_text()
    var_lower = var_name.lower()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        # Fall back to regex anchored at start-of-line.
        pattern = re.compile(rf"^\s*{re.escape(var_name)}\s*=", re.IGNORECASE)
        return [
            i
            for i, line in enumerate(src.splitlines(), start=1)
            if pattern.match(line) and not line.lstrip().startswith("#")
        ]

    out: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id.lower() == var_lower:
                    out.append(node.lineno)
        elif isinstance(node, ast.AnnAssign):  # `var_name: T = ...`
            if (
                isinstance(node.target, ast.Name)
                and node.target.id.lower() == var_lower
            ):
                out.append(node.lineno)
    return sorted(set(out))


def verify_marker_coverage(
    notebook_path: str | Path,
    expected_params: Iterable[str],
) -> dict:
    """Confirm each expected operator-bucket variable is assigned inside a
    marker block. Returns a report dict:

        {
          "notebook": <path>,
          "blocks": [(open_line, close_line), ...],
          "covered":   [param_name, ...],   # at least one assignment inside a marker
          "outside":   [{param, line}, ...], # assigned, but only outside markers
          "missing":   [param, ...],         # no assignment found anywhere
        }

    Use this BEFORE the wizard edits a notebook. `outside` entries signal a
    canonical-notebook defect that must be fixed (expand the markers) before
    the wizard runs — silently editing outside the markers violates scope.
    """
    notebook_path = Path(notebook_path)
    blocks = scan_marker_blocks(notebook_path)

    def in_any_block(line: int) -> bool:
        return any(open_l <= line <= close_l for open_l, close_l in blocks)

    covered: list[str] = []
    outside: list[dict] = []
    missing: list[str] = []
    for param in expected_params:
        # Dot-notation params (e.g. "sbs_channels_metadata.name") reference
        # nested dict keys within a list-of-dicts Python var — the CSV uses
        # them for schema-doc purposes; only the root name maps to a marker
        # assignment. Strip to root for the assignment check.
        root = param.split(".", 1)[0]
        lines = _assignment_lines(notebook_path, root)
        if not lines:
            missing.append(param)
            continue
        inside_lines = [ln for ln in lines if in_any_block(ln)]
        if inside_lines:
            covered.append(param)
        else:
            outside.append({"param": param, "line": lines[0]})

    return {
        "notebook": str(notebook_path),
        "blocks": blocks,
        "covered": covered,
        "outside": outside,
        "missing": missing,
    }


# ---------------------------------------------------------------------------
# Cross-cell variable-collision scan (marimo defines-once rule)
# ---------------------------------------------------------------------------


def scan_variable_collisions(notebook_path: str | Path) -> list[dict]:
    """Detect variables assigned at cell scope in more than one @app.cell.

    Marimo's contract is that each global is defined in exactly one cell. If a
    marker-block param in Cell A shares a name with an auto-bucket constant in
    Cell B's body, marimo raises a duplicate-definition error at export time
    (e.g., PREFIX_MAP set in the operator markers AND in the library-defaults
    cell). Returns a list of:

        {var, cells: [(cell_func_line, assign_line), ...]}

    Underscore-prefixed names are excluded — marimo treats those as cell-local,
    so duplicate definitions don't conflict across cells. Empty list means no
    real collisions. Detected statically — no execution needed.
    """
    src = Path(notebook_path).read_text()
    tree = ast.parse(src)
    assignments: dict[str, list[tuple[int, int]]] = {}

    def _collect_assigns(body: list[ast.stmt], cell_line: int) -> None:
        for node in body:
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and not tgt.id.startswith("_"):
                        assignments.setdefault(tgt.id, []).append(
                            (cell_line, node.lineno)
                        )
            elif isinstance(node, ast.If):
                _collect_assigns(node.body, cell_line)
                _collect_assigns(node.orelse, cell_line)
            elif isinstance(node, (ast.For, ast.While, ast.With, ast.Try)):
                _collect_assigns(node.body, cell_line)

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        is_cell = any(
            (isinstance(d, ast.Attribute) and d.attr == "cell")
            or (
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and d.func.attr == "cell"
            )
            for d in node.decorator_list
        )
        if not is_cell:
            continue
        _collect_assigns(node.body, node.lineno)

    collisions = []
    for var, sites in assignments.items():
        unique_cells = {cell_line for cell_line, _ in sites}
        if len(unique_cells) > 1:
            collisions.append({"var": var, "cells": sites})
    return collisions


def patch_marker_block_value(
    notebook_path: str | Path,
    var_name: str,
    new_value_literal: str,
) -> bool:
    """Rewrite the assignment for `var_name` inside its marker block.

    `new_value_literal` is the right-hand side as a Python source string (e.g.,
    `'12.5'`, `'"sgRNA"'`, `'[1, 2, 3]'` — caller is responsible for repr).
    Returns True if exactly one in-marker assignment was found and replaced.
    Raises if `var_name` is assigned outside any marker block (caller should
    have already run `verify_marker_coverage` and refused to edit).

    This is the write-side companion to verify_marker_coverage — keeps the
    notebook the canonical record of "what ran" (per wizard_workflow.md).
    """
    nb = Path(notebook_path)
    blocks = scan_marker_blocks(nb)
    lines = nb.read_text().splitlines(keepends=True)

    span = _assignment_span(nb, var_name, blocks)
    if span and span[1] > span[0]:  # multi-line literal
        start, end, indent, matched_name = span
        text_lines = nb.read_text().splitlines(keepends=True)
        newline = "\n" if text_lines and text_lines[0].endswith("\n") else ""
        replacement = f"{indent}{matched_name} = {new_value_literal}{newline}"
        text_lines[start - 1:end] = [replacement]
        nb.write_text("".join(text_lines))
        return True

    # Capture: (indent)(matched-name-as-spelled)(rhs)(optional trailing comment).
    # Case-insensitive so the CSV's lowercase config keys (e.g. channel_names)
    # match the notebook's canonical-case Python variables (CHANNEL_NAMES) —
    # mirrors the documented contract of `_assignment_lines`.
    assign_re = re.compile(
        rf"^(\s*)({re.escape(var_name)})\s*=\s*([^#\n]*?)(\s*#.*)?$",
        re.IGNORECASE,
    )

    def _in_marker(line_num: int) -> bool:
        return any(o <= line_num <= c for o, c in blocks)

    matches = []
    for i, line in enumerate(lines, start=1):
        m = assign_re.match(line.rstrip("\n"))
        if m and _in_marker(i):
            matches.append((i, m))

    if not matches:
        any_assign = _assignment_lines(nb, var_name)
        if any_assign:
            raise ValueError(
                f"{nb}: {var_name} is assigned at line(s) {any_assign} but none "
                f"are inside marker blocks (case-insensitive marker scan failed "
                f"too — recheck the canonical template)."
            )
        raise KeyError(f"{nb}: no assignment found for {var_name}")
    if len(matches) > 1:
        raise ValueError(
            f"{nb}: {var_name} has {len(matches)} in-marker assignments at lines {[m[0] for m in matches]}; "
            f"ambiguous — fix the canonical notebook to deduplicate."
        )

    line_num, m = matches[0]
    indent = m.group(1)
    matched_name = m.group(2)  # preserve the file's case (CHANNEL_NAMES, not channel_names)
    trailing = m.group(4) or ""
    # Preserve a two-space gap before a trailing `# comment` if the captured
    # leading-whitespace was lost or collapsed.
    if trailing and not trailing.startswith((" ", "\t")):
        trailing = "  " + trailing
    lines[line_num - 1] = f"{indent}{matched_name} = {new_value_literal}{trailing}\n"
    nb.write_text("".join(lines))
    return True


def find_marker_cell(ctx_or_notebook_path, param_name: str) -> str | None:
    """Return the cell ID of the @app.cell that contains the marker-block
    assignment for `param_name`. Accepts either a live marimo context (an
    object with a `cells` mapping/iterable) or a notebook path; in the
    notebook-path mode the AST is walked to locate the cell function whose
    body contains the marker-anchored assignment for the param.

    Returns `None` if no marker-block assignment is found. Case-insensitive on
    the param name to match the wizard's CSV-vs-marker convention.
    """
    # Live-context mode: iterate ctx.cells (whatever shape they take) and
    # return the first cell whose source contains both the OPERATOR PARAMETERS
    # marker and an assignment for the param.
    if not isinstance(ctx_or_notebook_path, (str, Path)):
        ctx = ctx_or_notebook_path
        cells = getattr(ctx, "cells", None)
        if cells is None:
            return None
        # ctx.cells may be a dict {id: cell} or an iterable of cells.
        iterable = cells.values() if hasattr(cells, "values") else cells
        assign_re = re.compile(
            rf"^\s*{re.escape(param_name)}\s*=", re.IGNORECASE | re.MULTILINE
        )
        for c in iterable:
            code = getattr(c, "code", "") or ""
            name = getattr(c, "name", "") or ""
            is_params = name == PARAMS_CELL_PREFIX or name.startswith(PARAMS_CELL_PREFIX + "_")
            if (is_params or _MARKER_OPEN_RE.search(code)) and assign_re.search(code):
                cell_id = getattr(c, "id", None) or getattr(c, "cell_id", None)
                if cell_id is not None:
                    return cell_id
        return None

    # Notebook-path mode: AST walk over the @app.cell functions. Find the
    # function whose body contains an assignment for `param_name` AND whose
    # source spans a marker block.
    nb_path = Path(ctx_or_notebook_path)
    blocks = scan_marker_blocks(nb_path)
    if not blocks:
        return None
    assign_lines = _assignment_lines(nb_path, param_name)
    in_marker = [ln for ln in assign_lines if any(o <= ln <= c for o, c in blocks)]
    if not in_marker:
        return None
    src = nb_path.read_text()
    tree = ast.parse(src)
    target_line = in_marker[0]
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        is_cell = any(
            (isinstance(d, ast.Attribute) and d.attr == "cell")
            or (
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and d.func.attr == "cell"
            )
            for d in node.decorator_list
        )
        if not is_cell:
            continue
        if (
            node.lineno
            <= target_line
            <= (getattr(node, "end_lineno", node.lineno) or node.lineno)
        ):
            return node.name
    return None


# Canonical notebook filename → phase name. Used to derive phase from nb_path
# in sweep_headless / validate_tiles_headless so sweep_qc overrides can be
# loaded from qc_gates.yaml without the caller knowing the phase explicitly.

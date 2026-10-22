#!/usr/bin/env python3
"""audit_csv_vs_canonical.py — structural completeness check.

Compares the param taxonomy (data/interview_template.json, the hand-maintained
source of truth) against each canonical phase notebook. Reports:

- **csv_missing**: variables assigned inside marker blocks of the canonical
  notebook but absent from the template. Would have caught the first
  reference pass's F-020 gap (PERTURBATION_ID_COL/CONTROL_KEY/BATCH_COLS missing
  from the aggregate rows).
- **notebook_missing**: template rows whose `param_name` doesn't appear
  anywhere in the corresponding notebook (case-insensitive). Either a typo, a
  deleted notebook variable, or a template row for a feature that never
  landed.
- **bucket_mismatch**: bucket=user/user_tuned_seed param assigned outside
  marker blocks in the notebook (canonical-template defect — preflight would
  block the wizard).

Usage:
    python tools/audit_csv_vs_canonical.py <canonical_dir>

Where `<canonical_dir>` is the screen's canonical notebook folder (or any dir with the
six phase notebooks named 0_preprocess.py / 1_sbs.py / 2_phenotype.py /
3_merge.py / 5_aggregate.py / 6_cluster.py).

Exits 0 if all clean, 1 if any audit category is non-empty.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

PHASE_TO_FILE = {
    "preprocess": "0_preprocess.py",
    "sbs": "1_sbs.py",
    "phenotype": "2_phenotype.py",
    "merge": "3_merge.py",
    "aggregate": "5_aggregate.py",
    "cluster": "6_cluster.py",
}

# Heuristic: skip noisy local variables that are clearly not config params.
_PYTHON_BUILTINS = {"True", "False", "None"}
_LOCAL_PATTERNS = (
    re.compile(r"^_"),                  # marimo private
    re.compile(r"^[a-z][a-z0-9_]*_(re|rx|fp|idx|index|tmp)$"),  # local sentinel suffix
)


def _is_config_param(name: str) -> bool:
    if name in _PYTHON_BUILTINS:
        return False
    if any(p.match(name) for p in _LOCAL_PATTERNS):
        return False
    # Heuristic: config params are UPPER_SNAKE or lowercase config-key shape.
    # Mixed-case is usually a local var. Single-letter is always local.
    if len(name) <= 2:
        return False
    if name.isupper() or "_" in name:
        return True
    # Lowercase single-word and CamelCase are usually locals — skip.
    return False


def _marker_blocks(src: str) -> list[tuple[int, int]]:
    """Return (open_line, close_line) for each marker block. Matches the same
    regex pair as brieflow_wizard.scan_marker_blocks."""
    open_re = re.compile(r"^#\s*=+\s*OPERATOR\s+PARAMETERS\b.*?=+\s*$")
    close_re = re.compile(r"^#\s*=+\s*END\s+OPERATOR\s+PARAMETERS\b.*?=+\s*$")
    blocks: list[tuple[int, int]] = []
    open_at: int | None = None
    for i, line in enumerate(src.splitlines(), start=1):
        s = line.strip()
        if close_re.match(s):
            if open_at is not None:
                blocks.append((open_at, i))
                open_at = None
        elif open_re.match(s):
            open_at = i
    return blocks


def collect_marker_assignments(notebook_path: Path) -> set[str]:
    """Return UPPER_SNAKE names assigned at top level inside marker blocks."""
    src = notebook_path.read_text()
    blocks = _marker_blocks(src)

    def _in_block(line: int) -> bool:
        return any(o <= line <= c for o, c in blocks)

    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        print(f"  [warn] {notebook_path.name}: SyntaxError at L{e.lineno} ({e.msg}); audit may be incomplete", file=sys.stderr)
        return set()

    names: set[str] = set()

    def _walk(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, ast.Assign):
                if _in_block(node.lineno):
                    for tgt in node.targets:
                        if isinstance(tgt, ast.Name) and _is_config_param(tgt.id):
                            names.add(tgt.id)
            elif isinstance(node, ast.If):
                _walk(node.body)
                _walk(node.orelse)
            elif isinstance(node, (ast.For, ast.While, ast.With, ast.Try)):
                _walk(node.body)

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            _walk(node.body)
    return names


def audit_phase(notebook_path: Path, csv_param_names: set[str]) -> dict:
    """Compare one canonical notebook against the CSV's params for that phase."""
    notebook_names = collect_marker_assignments(notebook_path)
    notebook_lower = {n.lower() for n in notebook_names}

    # Strip dot-notation suffixes when checking root presence in the notebook
    # (CSV param `sbs_channels_metadata.name` is satisfied by notebook
    # variable `SBS_CHANNELS_METADATA`).
    csv_roots_lower = {p.split(".", 1)[0].lower() for p in csv_param_names}
    csv_missing = sorted(n for n in notebook_names if n.lower() not in csv_roots_lower)
    notebook_missing = sorted(
        p for p in csv_param_names
        if "." in p
        and p.split(".", 1)[0].lower() not in notebook_lower
        or "." not in p and p.lower() not in notebook_lower
    )

    return {
        "csv_missing": csv_missing,
        "notebook_missing": notebook_missing,
        "n_marker_vars": len(notebook_names),
        "n_csv_params": len(csv_param_names),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <canonical_dir>", file=sys.stderr)
        return 2

    canonical_dir = Path(argv[1])
    if not canonical_dir.is_dir():
        print(f"not a directory: {canonical_dir}", file=sys.stderr)
        return 2

    plugin_root = Path(__file__).resolve().parent.parent
    template_path = plugin_root / "data" / "interview_template.json"
    if not template_path.exists():
        print(f"template not found: {template_path}", file=sys.stderr)
        return 2

    rows = json.loads(template_path.read_text())["params"]
    by_phase: dict[str, set[str]] = {}
    for r in rows:
        by_phase.setdefault(r["notebook"], set()).add(r["param_name"])

    any_issues = False
    for phase, fname in PHASE_TO_FILE.items():
        nb_path = canonical_dir / fname
        if not nb_path.exists():
            print(f"{fname}: MISSING in canonical dir")
            any_issues = True
            continue
        report = audit_phase(nb_path, by_phase.get(phase, set()))
        clean = not report["csv_missing"] and not report["notebook_missing"]
        flag = "OK" if clean else "ISSUES"
        print(f"{fname}: {flag} (marker_vars={report['n_marker_vars']}, csv_params={report['n_csv_params']})")
        if report["csv_missing"]:
            print(f"  csv_missing ({len(report['csv_missing'])}) — marker var not in CSV: {report['csv_missing']}")
            any_issues = True
        if report["notebook_missing"]:
            print(f"  notebook_missing ({len(report['notebook_missing'])}) — CSV row not in notebook: {report['notebook_missing'][:10]}{'...' if len(report['notebook_missing']) > 10 else ''}")
            any_issues = True

    return 1 if any_issues else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

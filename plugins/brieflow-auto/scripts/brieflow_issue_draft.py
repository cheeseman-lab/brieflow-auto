#!/usr/bin/env python3
"""brieflow_issue_draft.py — issue_draft.{md,json}: what the runner leaves behind when it
gives up, assembled from the recover action, the diag bundle and run_manifest.json.
/brieflow-issue uses this local evidence to automatically open a complete report with
a backward-compatible fix proposal via brieflow_issue.py. Raw evidence stays local."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
try:
    from brieflow_manifest import read_run_manifest  # noqa: E402
except Exception:  # pragma: no cover

    def read_run_manifest(analysis_dir):  # type: ignore[misc]
        return None


DRAFT_SCHEMA = "brieflow-issue-draft/v1"
FILL = "<operator: fill in>"
UPSTREAM_REPO = "cheeseman-lab/brieflow"
RC_NAMES = {
    1: "QC_GATE_FAIL",
    2: "RUN_FAIL",
    3: "TEMPLATE_GAP",
    4: "DISK_UNAVAIL",
    5: "NOTEBOOK_FAIL",
    6: "PREFLIGHT_FAIL",
    7: "UPSTREAM_RERUN",
    8: "AWAITING_LAUNCH",
}


def write_issue_draft(
    analysis_dir,
    tag: str,
    rc: int,
    phase: str,
    *,
    action: dict | None = None,
    diag_bundle: Path | str | None = None,
) -> Path:
    """Write .brieflow/issue_draft.md + .json; archive the previous JSON to issue_drafts.jsonl."""
    bf = Path(analysis_dir) / ".brieflow"
    js_path, md_path, hist = (
        bf / "issue_draft.json",
        bf / "issue_draft.md",
        bf / "issue_drafts.jsonl",
    )
    manifest = read_run_manifest(analysis_dir)
    action = action or {}
    route, next_action = issue_route(rc, action)
    if route == "investigate" and suggested_repo(rc, action):
        next_action = _UPSTREAM_ACTION
    doc = {
        "schema": DRAFT_SCHEMA,
        "written_at": datetime.now(timezone.utc).isoformat(),
        "tag": tag,
        "phase": phase,
        "rc": rc,
        "trigger": RC_NAMES.get(rc, str(rc)),
        "classification": {
            k: action.get(k)
            for k in (
                "classification",
                "rule",
                "reason",
                "evidence",
                "should_file_issue",
                "slurm_log",
            )
        }
        if action
        else None,
        "diag_bundle": str(diag_bundle) if diag_bundle else None,
        "manifest": manifest,
        "suggested_repo": suggested_repo(rc, action),
        "blocker": _pause_reason(analysis_dir, phase),
        "route": route,
        "agent_next_action": next_action,
        "filed_url": None,
    }
    try:
        bf.mkdir(parents=True, exist_ok=True)
        if js_path.exists():
            with open(hist, "a") as f:
                f.write(js_path.read_text().replace("\n", "") + "\n")
        js_path.write_text(json.dumps(doc, indent=2, default=str))
        md_path.write_text(render_issue_draft(doc))
    except OSError as e:
        print(f"brieflow_issue_draft: could not write {bf}: {e}", file=sys.stderr)
    return md_path


_UPSTREAM_ACTION = (
    "Investigate the pipeline defect and invoke brieflow-issue to open an actual "
    "upstream issue with a backward-compatible fix proposal and regression tests. "
    "Do not edit pipeline source during the run."
)
# Where each blocker goes. Only an identified pipeline defect becomes an upstream report;
# a QC verdict is the operator's call and a harness gap is a finding.
_ROUTES = {
    1: (
        "operator-decision",
        "The phase finished and its QC gate failed. Ask the operator: retune the phase "
        "(/brieflow-notebook) or accept the result with `brieflow-run --accept-qc <phase>` "
        "(no rerun). Not an upstream issue unless the metric itself is computed wrongly.",
    ),
    5: (
        "investigate",
        "The phase notebook failed before writing config. Read its log: a harness defect "
        "(an interview row the notebook ignores, a missing derivation) is a finding "
        "(record_finding); a Brieflow defect goes to brieflow-issue.",
    ),
    6: (
        "config",
        "Preflight found a HIGH-severity issue. Fix the key it names. A rule missing from "
        "the plugin's memory baseline is a harness finding (record_finding); a valid value "
        "the rule code rejects is a Brieflow defect for brieflow-issue.",
    ),
    7: (
        "investigate",
        "A completed upstream phase would rerun. Check the phase gates and upstream outputs "
        "before forcing anything.",
    ),
}
_CONFIG_CLASSES = ("config_mismatch", "submission_failure")


def issue_route(rc: int, action: dict | None) -> tuple[str, str]:
    """(route, next action) for an escalation: upstream-issue, operator-decision, config,
    or investigate."""
    action = action or {}
    if action.get("classification") in _CONFIG_CLASSES:
        return "config", (
            f"Recovery classified this as {action['classification']}: fix the config or "
            "profile it names, then resume. Not an upstream issue."
        )
    if action.get("should_file_issue"):
        return "upstream-issue", _UPSTREAM_ACTION
    return _ROUTES.get(
        rc,
        ("investigate", "Investigate the blocker; open an upstream issue only if it is a "
         "pipeline defect."),
    )


def _pause_reason(analysis_dir, phase: str) -> str | None:
    """Why the runner paused at `phase`, from progress.json."""
    try:
        paused = json.loads((Path(analysis_dir) / ".brieflow" / "progress.json").read_text())
    except (OSError, ValueError):
        return None
    paused = paused.get("paused") or {}
    return paused.get("reason") if paused.get("phase") == phase else None


def suggested_repo(rc: int, action: dict | None) -> str | None:
    if action and action.get("should_file_issue"):
        return UPSTREAM_REPO
    return UPSTREAM_REPO if rc in (6, 7) else None


def screen_name(manifest: dict) -> str:
    """The screen's own name from screen.yaml, else the repo dir minus `-analysis`."""
    analysis_dir = (manifest.get("screen") or {}).get("analysis_dir") or ""
    if not analysis_dir:
        return FILL
    try:
        import yaml

        doc = yaml.safe_load((Path(analysis_dir) / "screen.yaml").read_text())
        name = doc.get("name") if isinstance(doc, dict) else None
    except Exception:
        name = None
    return str(name) if name else Path(analysis_dir).parent.name.removesuffix("-analysis")


def render_issue_draft(doc: dict) -> str:
    m = doc.get("manifest") or {}
    c = doc.get("classification") or {}
    screen = screen_name(m)
    title = (
        f"{screen}: {doc['phase']} {doc['trigger']} — {c.get('rule') or c.get('reason') or FILL}"
    )
    evidence = "\n".join(f"    {e}" for e in (c.get("evidence") or [])) or f"    {FILL}"
    argv = " ".join(m.get("run", {}).get("argv", [])) if m else FILL
    return f"""# {title}

Suggested repo: {doc.get("suggested_repo") or "none (operator-fixable)"}

Route: {doc.get("route") or "investigate"} — {doc.get("agent_next_action") or ""}

## Description
{c.get("reason") or doc.get("blocker") or FILL}

Evidence:
{evidence}

Diag bundle: {doc.get("diag_bundle") or FILL}

## Reproduction Steps
    {argv}
Phase: {doc["phase"]}  Trigger: {doc["trigger"]}  Tag: {doc["tag"]}

## Expected Behavior
{FILL}

## Environment
- Screen: {screen}
- brieflow commit: {m.get("screen", {}).get("brieflow_submodule_sha") if m else FILL}
- Screen repo commit: {m.get("screen", {}).get("repo_sha") if m else FILL}
- Plugin: brieflow-auto {m.get("plugin", {}).get("version") if m else FILL}
- Compute: {m.get("run", {}).get("backend") if m else FILL} on {m.get("env", {}).get("hostname") if m else FILL}
"""

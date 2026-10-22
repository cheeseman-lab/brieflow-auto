#!/usr/bin/env python3
"""brieflow_launch_review.py — the launch gate. After the interview resolves and before
any compute is spent, write .brieflow/launch_review.{html,json}: what will run, operator
gaps, every parameter with its provenance, geometry and disk, planned sweeps, how to
proceed. The runner stops (exit 8) until re-run with --approved against the review a
PREVIOUS invocation left on disk — read the prior hash before writing the fresh one, or
--approved approves a page nobody has read. Writing is best-effort; a review that cannot
be written leaves the run unapproved rather than raising."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shlex
import shutil
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


REVIEW_SCHEMA = "brieflow-launch-review/v1"
REVIEW_JSON = "launch_review.json"
REVIEW_HTML = "launch_review.html"

#: argv tokens the printed re-run command supplies itself (bin/brieflow-run adds
#: --screen; --approved is what the operator is being asked to add).
_ARGV_DROP = ("--approved", "--screen")


# Values the runner itself writes during a run (test tiles, sweep winners, merge anchors).
# Their rows stay in the hash, their values do not: a resumed run would otherwise need a new
# review for its own output. Each such value is journaled when it is written.
RUNNER_WRITTEN_SOURCES = ("auto-probe", "tuned-confirmed")


def review_hash(interview: dict, phases: list[str], backend: str, hooks: str) -> str:
    """Hash what the operator is approving: the active interview rows (operator-owned
    values; runner-written ones by source only), the phase range, the backend and the
    data-movement hooks."""
    rows = sorted(
        (
            r.get("param_name", ""),
            r.get("notebook", ""),
            f"<{r.get('value_source')}>"
            if r.get("value_source") in RUNNER_WRITTEN_SOURCES
            else json.dumps(r.get("value"), sort_keys=True, default=str),
        )
        for r in interview.get("params", [])
        if r.get("active", True)
    )
    payload = json.dumps([rows, phases, backend, hooks], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def write_launch_review(
    analysis_dir,
    args,
    tag,
    *,
    interview,
    phases,
    geometry,
    disk,
    disk_check_path,
    compute,
    lab,
    gaps,
) -> dict:
    """Write .brieflow/launch_review.{json,html} for this launch and return the review."""
    analysis_dir = Path(analysis_dir)
    bf = analysis_dir / ".brieflow"
    backend = getattr(args, "backend", "slurm")
    hooks = getattr(args, "lifecycle_hooks", "noop")
    manifest = read_run_manifest(analysis_dir) or {}
    breakdown = disk.get("phase_breakdown", {}) if disk else {}
    try:
        free_gb = shutil.disk_usage(disk_check_path).free / 1e9
    except OSError:
        free_gb = None
    review = {
        "schema": REVIEW_SCHEMA,
        "written_at": datetime.now(timezone.utc).isoformat(),
        "tag": tag,
        "what_will_run": {
            "phases": list(phases),
            "backend": backend,
            "compute": compute,
            "lifecycle_hooks": hooks,
            "screen_id": getattr(args, "screen_id", None) or analysis_dir.name,
            "plugin_version": _get(manifest, "plugin", "version"),
            "brieflow_sha": _get(manifest, "screen", "brieflow_submodule_sha"),
        },
        "operator_gaps": list(gaps),
        "interview": _interview_rows(interview),
        "geometry_and_disk": {
            "geometry": geometry,
            "required_gb_by_phase": {p: breakdown.get(p, 0) for p in phases},
            "free_gb": free_gb,
        },
        "sweeps_planned": {
            "sweep_tiles": (lab or {}).get("sweep_tiles"),
            "sweep_joint": (lab or {}).get("sweep_joint"),
            "sweeps": {p: (lab or {}).get("sweeps", {}).get(p) for p in phases},
        },
        "how_to_proceed": {
            "command": _proceed_command(analysis_dir),
            "journal": ".brieflow/decisions.jsonl (kind=launch-approved)",
        },
    }
    review["review_sha256"] = review_hash(interview, list(phases), backend, hooks)
    try:
        bf.mkdir(parents=True, exist_ok=True)
        # HTML first: the JSON is the cross-invocation approval token, and a write that
        # dies between the two must not leave a fresh token beside a stale page.
        (bf / REVIEW_HTML).write_text(render_launch_review_html(review, _interview_html(interview)))
        (bf / REVIEW_JSON).write_text(json.dumps(review, indent=2, default=str))
    except OSError as e:
        print(f"brieflow_launch_review: could not write {bf}: {e}", file=sys.stderr)
    return review


def read_prior_review_hash(analysis_dir) -> str | None:
    """The review_sha256 a PREVIOUS invocation left on disk, or None when there is none.

    Read this BEFORE write_launch_review overwrites the file. It is the whole of the
    gate: without it, --approved would be checked against a review this same invocation
    had just written, and a virgin screen would launch unread."""
    try:
        doc = json.loads((Path(analysis_dir) / ".brieflow" / REVIEW_JSON).read_text())
    except (OSError, ValueError):
        return None
    return doc.get("review_sha256") if isinstance(doc, dict) else None


def launch_approved(args, review: dict, prior_hash: str | None) -> bool:
    """True only when --approved was given AND the review already on disk before this
    invocation matches the one just computed.

    A virgin screen (prior_hash None), a changed range/backend/interview, and a review
    that could not be written all read as "not the thing the operator approved"."""
    if not getattr(args, "approved", False):
        return False
    return bool(review.get("review_sha256")) and prior_hash == review.get("review_sha256")


def _proceed_command(analysis_dir: Path) -> str:
    """This launch, re-run through the one entry point with the approval added.

    sys.argv reads `brieflow_run.py --screen ...` even when bin/brieflow-run supplied
    the --screen, so drop the wrapper's own tokens instead of printing a command the
    operator cannot paste back. --analysis-dir is re-rendered from the resolved path
    rather than echoed from argv, and the interpreter is carried whenever `python` on
    PATH does not resolve to this one — bin/brieflow-run honours PYTHON, and the VM
    orchestrator launches by the env's absolute python with no conda activate. The test
    is the resolved path, not the name: a conda env's `python` is also called python."""
    argv = _drop_analysis_dir([a for a in sys.argv[1:] if a not in _ARGV_DROP])
    parts = ["bin/brieflow-run", "--analysis-dir", str(analysis_dir), *argv, "--approved"]
    on_path = shutil.which("python")
    if not on_path or os.path.realpath(on_path) != os.path.realpath(sys.executable):
        parts.insert(0, f"PYTHON={sys.executable}")
    return shlex.join(parts)


def _drop_analysis_dir(argv: list[str]) -> list[str]:
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
        elif a == "--analysis-dir":
            skip = True
        elif not a.startswith("--analysis-dir="):
            out.append(a)
    return out


def _get(doc: dict, section: str, key: str):
    sub = doc.get(section)
    return sub.get(key) if isinstance(sub, dict) else None


def _interview_rows(interview: dict) -> list[dict]:
    return [
        {
            "param_name": r.get("param_name"),
            "notebook": r.get("notebook"),
            "value": r.get("value"),
            "value_source": r.get("value_source"),
            "notes": r.get("notes"),
        }
        for r in interview.get("params", [])
        if r.get("active", True)
    ]


def _interview_html(interview: dict) -> str:
    """The wizard's own provenance table when the brieflow env is importable, and a
    plain param/value/source table when it is not.

    Either way a FRAGMENT: the wizard renders a whole page, and a second
    <!doctype>/<html> nested inside section 3 is not a document a browser parses as
    the author meant. See _fragment for what survives the trip."""
    try:
        from brieflow_wizard import interview_review, render_interview_review_html  # noqa: E402

        return _fragment(render_interview_review_html(interview_review(interview)))
    except Exception:
        rows = "".join(
            f"<tr><td>{html.escape(str(r['param_name']))}</td>"
            f"<td>{html.escape(str(r['value']))}</td>"
            f"<td>{html.escape(str(r['value_source']))}</td></tr>"
            for r in _interview_rows(interview)
        )
        return f"<table><tr><th>param</th><th>value</th><th>source</th></tr>{rows}</table>"


def _fragment(doc: str) -> str:
    """A whole HTML document reduced to its <style> plus the inner <body>; anything
    that is already a fragment passes through untouched."""
    body = re.search(r"<body[^>]*>(.*)</body>", doc, re.S)
    if not body:
        return doc
    style = re.search(r"<style>.*?</style>", doc, re.S)
    return (style.group(0) if style else "") + body.group(1)


def _hoist_style(fragment: str) -> tuple[str, str]:
    """Split a leading <style> block off a fragment: (css, rest).

    The embedded rules belong in the page head. They go in AHEAD of this page's own,
    so where the two collide (body, table, td) the launch review's layout wins."""
    m = re.search(r"<style>(.*?)</style>", fragment, re.S)
    if not m:
        return "", fragment
    return m.group(1), fragment[: m.start()] + fragment[m.end() :]


def render_launch_review_html(review: dict, interview_html: str) -> str:
    w = review["what_will_run"]
    gaps = review["operator_gaps"]
    gap_html = (
        "<p class='gap'>Run cannot start until these are answered:</p><ul>"
        + "".join(f"<li>{html.escape(str(g.get('param_name')))}</li>" for g in gaps)
        + "</ul>"
        if gaps
        else "<p>None.</p>"
    )
    gd = review["geometry_and_disk"]
    disk_rows = "".join(
        f"<tr><td>{html.escape(str(p))}</td><td>{gb:.0f}</td></tr>"
        for p, gb in gd["required_gb_by_phase"].items()
    )
    free = "unknown" if gd["free_gb"] is None else f"{gd['free_gb']:.0f} GB"
    iv_css, interview_html = _hoist_style(interview_html)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Launch review — {html.escape(review["tag"])}</title>
<style>{iv_css}</style>
<style>body{{font:15px/1.5 system-ui,sans-serif;max-width:80ch;margin:2rem auto;padding:0 1rem}}
h2{{margin-top:2rem;border-top:1px solid #ccc;padding-top:.5rem}}table{{border-collapse:collapse}}
td,th{{border-bottom:1px solid #ddd;padding:.25rem .6rem;text-align:left}}.gap{{color:#a33;font-weight:600}}
pre{{background:#f3f3f3;padding:.6rem;overflow-x:auto}}</style></head><body>
<h1>Launch review</h1>
<h2>1. What will run</h2>
<p>Phases: <b>{" → ".join(html.escape(str(p)) for p in w["phases"])}</b><br>
Backend: {html.escape(str(w["backend"]))} {html.escape(json.dumps(w["compute"], default=str))}<br>
Data movement: {html.escape(str(w["lifecycle_hooks"]))} · screen id {html.escape(str(w["screen_id"]))}<br>
Plugin {html.escape(str(w["plugin_version"]))} · brieflow {html.escape(str(w["brieflow_sha"]))}</p>
<h2>2. Operator gaps</h2>{gap_html}
<h2>3. Interview review</h2>{interview_html}
<h2>4. Geometry and disk</h2><pre>{html.escape(json.dumps(gd["geometry"], indent=2, default=str))}</pre>
<table><tr><th>phase</th><th>required GB</th></tr>{disk_rows}</table>
<p>Free on output disk: {free}</p>
<h2>5. Sweeps planned</h2><pre>{html.escape(json.dumps(review["sweeps_planned"], indent=2, default=str))}</pre>
<h2>6. How to proceed</h2><pre>{html.escape(review["how_to_proceed"]["command"])}</pre>
<p>Approval is journalled in <code>{html.escape(review["how_to_proceed"]["journal"])}</code>.</p>
</body></html>
"""

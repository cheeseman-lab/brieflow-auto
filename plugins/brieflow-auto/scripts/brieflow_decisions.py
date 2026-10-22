"""Consequential-decision journal for autonomous runs.

Writes .brieflow/decisions.jsonl (append-only, schema brieflow-decisions/v1) plus a
rendered, human-readable .brieflow/decisions.md. Captures CHOICES among alternatives and
LLM judgment calls — NOT routine param fills (those carry value_source) and NOT run
progress (that lives in the monitor / .tee.log files)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "brieflow-decisions/v1"


def _paths(analysis_dir: str | Path) -> tuple[Path, Path]:
    d = Path(analysis_dir) / ".brieflow"
    return d / "decisions.jsonl", d / "decisions.md"


def record_decision(analysis_dir, *, kind: str, phase: str, choice: str, why: str,
                    inputs: dict | None = None, source: str = "auto") -> dict:
    """Append one consequential-decision entry and re-render the md.

    source: 'auto' (mechanical loop decision) | 'agent' (LLM judgment call) |
    'operator' (a person's explicit approval)."""
    if source not in ("auto", "agent", "operator"):
        raise ValueError(f"source must be 'auto', 'agent' or 'operator', got {source!r}")
    jsonl, md = _paths(analysis_dir)
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "schema": SCHEMA,
        "ts": datetime.now(timezone.utc).isoformat(),
        "phase": phase,
        "kind": kind,
        "choice": choice,
        "why": why,
        "inputs": inputs or {},
        "source": source,
    }
    with open(jsonl, "a") as f:
        f.write(json.dumps(entry) + "\n")
    _render_md(jsonl, md)
    return entry


def _render_md(jsonl: Path, md: Path) -> None:
    rows = [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    out = ["# Runner decision journal", "",
           f"{len(rows)} consequential decision(s) — most recent last.", ""]
    for e in rows:
        tag = {"agent": "🤖", "operator": "🧑"}.get(e.get("source"), "⚙️")
        line = (f"- **{e['ts']}** · `{e['phase']}` · {tag} **{e['kind']}** — {e['choice']}  "
                f"\n  _why:_ {e['why']}")
        if e.get("inputs"):
            line += f"  \n  _inputs:_ `{json.dumps(e['inputs'])}`"
        out.append(line)
    md.write_text("\n".join(out) + "\n")

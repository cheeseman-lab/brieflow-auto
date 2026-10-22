"""Defect and workaround log for a screen.

Writes .brieflow/findings.json (schema wizard-findings/v1). Every skill records a finding
when the agent works around a defect in place (a screen.yaml edit, an input symlink tree,
a resource stopgap, a sticky interview-row rewrite) or finds one it cannot fix. The
improvement loop reads these rows; a workaround that lives only in prose is invisible to it."""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "wizard-findings/v1"


def load_findings(analysis_dir) -> dict:
    """The findings document; a bare list from an older writer is wrapped, not dropped."""
    path = Path(analysis_dir) / ".brieflow" / "findings.json"
    if not path.is_file():
        return {"schema": SCHEMA, "findings": []}
    data = json.loads(path.read_text() or "[]")
    if isinstance(data, list):
        return {"schema": SCHEMA, "findings": data}
    data.setdefault("schema", SCHEMA)
    data.setdefault("findings", [])
    return data


def record_finding(analysis_dir, *, kind: str, phase: str, summary: str,
                   workaround: str | None = None, evidence: list[str] | None = None,
                   source: str = "agent") -> dict:
    """Append one finding.

    workaround: what was changed in place to keep the run going (None if nothing was).
    source: 'auto' | 'agent' | 'operator'."""
    if source not in ("auto", "agent", "operator"):
        raise ValueError(f"source must be 'auto', 'agent' or 'operator', got {source!r}")
    if not kind.strip() or not summary.strip():
        raise ValueError("kind and summary are required")
    path = Path(analysis_dir) / ".brieflow" / "findings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = load_findings(analysis_dir)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "phase": phase,
        "kind": kind,
        "summary": summary,
        "workaround": workaround,
        "evidence": list(evidence or []),
        "source": source,
    }
    doc["findings"].append(entry)
    tmp = path.with_name(f".findings.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    tmp.replace(path)
    return entry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-dir", required=True)
    parser.add_argument("--kind", required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--workaround")
    parser.add_argument("--evidence", action="append", default=[])
    parser.add_argument("--source", default="agent")
    args = parser.parse_args()
    entry = record_finding(args.analysis_dir, kind=args.kind, phase=args.phase,
                           summary=args.summary, workaround=args.workaround,
                           evidence=args.evidence, source=args.source)
    print(json.dumps(entry))


if __name__ == "__main__":
    main()

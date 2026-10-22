import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import brieflow_decisions as bd  # noqa: E402

def test_record_appends_jsonl_and_renders_md(tmp_path):
    e = bd.record_decision(tmp_path, kind="sweep-winner", phase="sbs",
                           choice="THRESHOLD_READS=320", why="max mapped-single-gene rate",
                           inputs={"candidates": [200, 320, 500]}, source="auto")
    assert e["schema"] == "brieflow-decisions/v1" and e["source"] == "auto"
    jsonl = tmp_path / ".brieflow" / "decisions.jsonl"
    md = tmp_path / ".brieflow" / "decisions.md"
    rows = [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    assert len(rows) == 1 and rows[0]["kind"] == "sweep-winner"
    assert "THRESHOLD_READS=320" in md.read_text() and "max mapped-single-gene rate" in md.read_text()

def test_append_is_ordered_and_agent_tagged(tmp_path):
    bd.record_decision(tmp_path, kind="blocker-pause", phase="merge", choice="paused",
                       why="qc gate failed", source="auto")
    bd.record_decision(tmp_path, kind="qc-accept", phase="merge", choice="proceed",
                       why="borderline but recovery-rate within 2% of gate", source="agent")
    rows = [json.loads(line) for line in (tmp_path/".brieflow"/"decisions.jsonl").read_text().splitlines() if line.strip()]
    assert [r["kind"] for r in rows] == ["blocker-pause", "qc-accept"]
    assert "🤖" in (tmp_path/".brieflow"/"decisions.md").read_text()  # agent-tagged entry rendered

def test_bad_source_rejected(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        bd.record_decision(tmp_path, kind="x", phase="sbs", choice="y", why="z", source="bogus")

"""Every escalation told the agent to file an upstream issue, including a QC verdict
(operator's call) and a classify switch the harness ignored; QC drafts also came out empty."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import brieflow_issue_draft as dr  # noqa: E402


def test_routes_by_blocker():
    assert dr.issue_route(1, None)[0] == "operator-decision"
    assert "--accept-qc" in dr.issue_route(1, None)[1]
    assert dr.issue_route(2, {"classification": "submission_failure"})[0] == "config"
    assert dr.issue_route(2, {"classification": "unknown", "should_file_issue": True})[0] == (
        "upstream-issue"
    )
    assert dr.issue_route(5, None)[0] == "investigate"
    assert "record_finding" in dr.issue_route(6, None)[1]


def test_qc_draft_carries_the_blocker_and_route(tmp_path):
    (tmp_path / ".brieflow").mkdir()
    (tmp_path / ".brieflow" / "progress.json").write_text(json.dumps(
        {"paused": {"phase": "phenotype", "reason": "QC gate failed — cell_retention 0.709"}}))
    md = dr.write_issue_draft(tmp_path, "t", 1, "phenotype")
    js = json.loads((tmp_path / ".brieflow" / "issue_draft.json").read_text())
    assert js["route"] == "operator-decision" and "cell_retention 0.709" in js["blocker"]
    assert js["classification"] is None and js["suggested_repo"] is None
    text = md.read_text()
    assert "Route: operator-decision" in text and "cell_retention 0.709" in text

"""issue_draft.{md,json} on every escalating runner exit (spec §4.2)."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_issue_draft.py"


@pytest.fixture(scope="module")
def dr():
    spec = importlib.util.spec_from_file_location("brieflow_issue_draft", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_issue_draft"] = m
    spec.loader.exec_module(m)
    return m


def _manifest(tmp_path):
    d = tmp_path / ".brieflow"
    d.mkdir(exist_ok=True)
    (d / "run_manifest.json").write_text(
        json.dumps(
            {
                "schema": "brieflow-run-manifest/v1",
                "plugin": {"version": "0.6.0"},
                "screen": {"repo_sha": "abc", "brieflow_submodule_sha": "def"},
                "env": {"hostname": "h", "python": "3.11"},
                "run": {"backend": "slurm", "argv": ["x", "--screen"]},
            }
        )
    )


ACTION = {
    "classification": "unknown",
    "rule": "call_reads",
    "reason": "KeyError: gene_symbol_1",
    "evidence": ["log line"],
    "should_file_issue": True,
    "slurm_log": "/l/x.out",
}


def test_draft_for_escalated_run_failure(dr, tmp_path):
    _manifest(tmp_path)
    md = dr.write_issue_draft(
        tmp_path, "run_1", 2, "sbs", action=ACTION, diag_bundle=tmp_path / "diag" / "b.txt"
    )
    js = json.loads((tmp_path / ".brieflow" / "issue_draft.json").read_text())
    assert md == tmp_path / ".brieflow" / "issue_draft.md"
    assert js["schema"] == "brieflow-issue-draft/v1" and js["trigger"] == "RUN_FAIL"
    assert (
        js["suggested_repo"] == "cheeseman-lab/brieflow"
        and js["manifest"]["plugin"]["version"] == "0.6.0"
    )
    text = md.read_text()
    assert "call_reads" in text and "brieflow commit: def" in text and "--screen" in text
    assert "backward-compatible" in js["agent_next_action"]
    assert "actual" in js["agent_next_action"]


def test_qc_gate_fail_suggests_no_repo_and_marks_fill(dr, tmp_path):
    _manifest(tmp_path)
    dr.write_issue_draft(tmp_path, "t", 1, "merge")
    js = json.loads((tmp_path / ".brieflow" / "issue_draft.json").read_text())
    assert js["suggested_repo"] is None
    assert dr.FILL in (tmp_path / ".brieflow" / "issue_draft.md").read_text()


def test_preflight_fail_suggests_repo(dr, tmp_path):
    _manifest(tmp_path)
    dr.write_issue_draft(tmp_path, "t", 6, "sbs")
    assert (
        json.loads((tmp_path / ".brieflow" / "issue_draft.json").read_text())["suggested_repo"]
        == "cheeseman-lab/brieflow"
    )


def test_history_appended_and_no_manifest_ok(dr, tmp_path):
    dr.write_issue_draft(tmp_path, "t", 2, "sbs", action=ACTION)
    dr.write_issue_draft(tmp_path, "t2", 2, "sbs", action=ACTION)
    lines = (tmp_path / ".brieflow" / "issue_drafts.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["tag"] == "t"
    assert json.loads((tmp_path / ".brieflow" / "issue_draft.json").read_text())["manifest"] is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission bits")
def test_write_survives_unwritable_brieflow_dir(dr, tmp_path):
    brieflow_dir = tmp_path / ".brieflow"
    brieflow_dir.mkdir()
    mode = brieflow_dir.stat().st_mode
    brieflow_dir.chmod(0o500)
    try:
        p = dr.write_issue_draft(tmp_path, "t", 2, "sbs", action=ACTION)
        assert p == brieflow_dir / "issue_draft.md"
    finally:
        brieflow_dir.chmod(mode)


def test_screen_name_comes_from_screen_yaml(dr, tmp_path):
    """The screen's own name, not the directory it happens to sit in."""
    repo = tmp_path / "pooled-round2-analysis"
    (repo / ".brieflow").mkdir(parents=True)
    (repo / "screen.yaml").write_text("name: examplescreen\n")
    (tmp_path / ".brieflow").mkdir(exist_ok=True)
    (tmp_path / ".brieflow" / "run_manifest.json").write_text(
        json.dumps({"screen": {"analysis_dir": str(repo)}})
    )
    dr.write_issue_draft(tmp_path, "t", 2, "sbs", action=ACTION)
    assert "examplescreen: sbs RUN_FAIL" in (tmp_path / ".brieflow" / "issue_draft.md").read_text()


def test_screen_name_falls_back_to_the_repo_dir(dr, tmp_path):
    """No screen.yaml name: the repo dir with the -analysis suffix stripped."""
    analysis = tmp_path / "examplescreen-analysis" / "analysis"
    analysis.mkdir(parents=True)
    (tmp_path / ".brieflow").mkdir(exist_ok=True)
    (tmp_path / ".brieflow" / "run_manifest.json").write_text(
        json.dumps({"screen": {"analysis_dir": str(analysis)}})
    )
    dr.write_issue_draft(tmp_path, "t", 2, "sbs", action=ACTION)
    assert "examplescreen: sbs RUN_FAIL" in (tmp_path / ".brieflow" / "issue_draft.md").read_text()

import json
from argparse import Namespace

import pytest

from brieflow_rollout_hook import IGNORE_RULES, enable
from brieflow_trace import traced


def _record(tmp_path):
    return json.loads(next((tmp_path / ".brieflow/traces").glob("*/trace.json")).read_text())


def test_exception_keeps_message_traceback_and_session(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-1")

    @traced
    def run(args):
        raise AttributeError("'Namespace' object has no attribute 'approved'")

    with pytest.raises(AttributeError):
        run(Namespace(analysis_dir=tmp_path))
    record = _record(tmp_path)
    assert "no attribute 'approved'" in record["exception_message"]
    assert "AttributeError" in record["traceback"] and "in run" in record["traceback"]
    assert record["agent_session_id"] == "session-1"


def test_session_id_absent_outside_an_agent(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    assert traced(lambda args: 0)(Namespace(analysis_dir=tmp_path)) == 0
    assert "agent_session_id" not in _record(tmp_path)


def test_enable_adds_ignore_rules_once(tmp_path):
    workspace, repo = tmp_path / "screen", tmp_path / "screen" / "screen-analysis"
    (repo / ".git").mkdir(parents=True)
    (repo / ".gitignore").write_text("analysis/logs/")
    for _ in range(2):
        assert enable(workspace, [repo]) == [repo.resolve() / ".gitignore"]
    lines = (repo / ".gitignore").read_text().splitlines()
    assert lines[0] == "analysis/logs/"
    assert all(lines.count(rule) == 1 for rule in IGNORE_RULES)
    assert json.loads((workspace / ".brieflow/record_rollouts.json").read_text())["enabled"]


def test_enable_finds_the_enclosing_repo(tmp_path):
    (tmp_path / ".git").mkdir()
    workspace = tmp_path / "analysis"
    assert enable(workspace) == [tmp_path.resolve() / ".gitignore"]
    assert IGNORE_RULES[0] in (tmp_path / ".gitignore").read_text()

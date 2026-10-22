import json
from argparse import Namespace

import pytest

from brieflow_trace import attach, traced
from brieflow_rollout_hook import capture, enable


def test_each_invocation_keeps_output_and_state(tmp_path):
    state = tmp_path / ".brieflow"
    state.mkdir()
    (state / "progress.json").write_text('{"stage": "before"}')

    @traced
    def run(args):
        print("operator input needed")
        (state / "progress.json").write_text('{"stage": "after"}')
        return 3

    for _ in range(2):
        assert run(Namespace(analysis_dir=tmp_path, tag="../not-a-path")) == 3
    traces = list((state / "traces").iterdir())
    assert len(traces) == 2
    for trace in traces:
        assert "operator input needed" in (trace / "runner.log").read_text()
        assert (trace / "before/progress.json").exists()
        assert json.loads((trace / "after/progress.json").read_text())["stage"] == "after"
        record = json.loads((trace / "trace.json").read_text())
        assert record["exit_code"] == 3
        assert record["agent_rollout"] == "not-supplied"


def test_exception_is_retained_and_reraised(tmp_path):
    @traced
    def run(args):
        raise RuntimeError("private details")

    with pytest.raises(RuntimeError):
        run(Namespace(analysis_dir=tmp_path))
    record = json.loads(next((tmp_path / ".brieflow/traces").glob("*/trace.json")).read_text())
    assert record["status"] == "exception"
    assert record["exception_type"] == "RuntimeError"


def test_hook_requires_opt_in_and_deduplicates_exports(tmp_path):
    transcript = tmp_path / "runtime.jsonl"
    transcript.write_text('{"type": "assistant", "message": "example"}\n')
    payload = {"cwd": str(tmp_path), "session_id": "../session", "transcript_path": str(transcript)}
    assert capture(payload) is None
    enable(tmp_path)
    first = capture(payload)
    assert first.read_bytes() == transcript.read_bytes()
    assert capture(payload) == first
    manifest = json.loads((first.parent / "trace.json").read_text())
    assert len(manifest["rollouts"]) == 1
    assert manifest["public_release"] is False
    assert first.stat().st_mode & 0o777 == 0o600
    transcript.write_text(transcript.read_text() + '{"type": "user"}\n')
    assert capture(payload) != first
    assert len(list(first.parent.glob("rollout-*"))) == 2
    (tmp_path / ".brieflow/record_rollouts.json").write_text('{"enabled": false}')
    assert capture(payload) is None


def test_import_rejects_symlink(tmp_path):
    (tmp_path / "trace.json").write_text('{"schema": "brieflow-trace/v1"}')
    source = tmp_path / "source"
    source.write_text("example")
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(ValueError, match="regular file"):
        attach(tmp_path, link, "test", "test")

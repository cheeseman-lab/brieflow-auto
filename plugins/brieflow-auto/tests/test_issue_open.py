"""Public issue submission uses mocked GitHub calls, never the live tracker."""

import json
import subprocess

import pytest

import brieflow_issue as issue

URL = "https://github.com/cheeseman-lab/brieflow/issues/321"


@pytest.fixture
def report(tmp_path):
    folder = tmp_path / ".brieflow"
    folder.mkdir()
    (folder / "run_manifest.json").write_text(
        json.dumps(
            {
                "screen": {"repo_sha": "a" * 40, "brieflow_submodule_sha": "b" * 40},
                "env": {"hostname": "private-host", "token": "private-token"},
            }
        )
    )
    fields = dict(
        title="Reader rejects supported metadata",
        description="The reader rejects a synthetic metadata fixture.",
        reproduction="Run the metadata reader with the minimal fixture described here.",
        expected_behavior="Valid acquisition metadata should be parsed.",
        environment="Linux; browser not applicable; local Python invocation.",
        context="Synthetic input; no private screen data. Root cause is a hypothesis.",
        proposed_fix="Extend the format adapter with a validated optional field.",
        compatibility="Keep existing defaults and the output schema unchanged.",
        verification="Proposed: test both legacy and new metadata; no tests run yet.",
    )
    return tmp_path, fields, issue.prepare_report(tmp_path, **fields)


def test_template_and_compatibility_fields_required(report):
    root, fields, path = report
    doc = json.loads(path.read_text())
    assert doc["title"].startswith("[BUG] ")
    for heading in (
        "Describe the bug",
        "To Reproduce",
        "Expected behavior",
        "Screenshots",
        "Desktop",
        "Additional context",
        "Proposed fix",
        "Backward compatibility",
        "Regression tests",
    ):
        assert heading in doc["body"]
    assert "private-host" not in doc["body"] and "private-token" not in doc["body"]
    assert "b" * 40 in doc["body"]
    for key in ("proposed_fix", "compatibility", "verification"):
        with pytest.raises(ValueError):
            issue.prepare_report(root, **{**fields, key: ""})


def test_missing_manifest_refuses_report(tmp_path, report):
    _, fields, _ = report
    with pytest.raises(ValueError, match="SHAs"):
        issue.prepare_report(tmp_path / "no-manifest", **fields)


def test_opens_once_and_records_receipt_and_journal(report, monkeypatch):
    root, fields, path = report
    calls = []

    def gh(*args, body=None):
        calls.append((args, body))
        return "[]" if args[1] == "list" else URL

    monkeypatch.setattr(issue, "_gh", gh)
    assert issue.open_report(path) == URL
    assert issue.open_report(path) == URL
    assert issue.prepare_report(root, **fields) == path
    assert json.loads(path.read_text())["filed_url"] == URL
    assert len(calls) == 2
    assert "--state" in calls[0][0] and "all" in calls[0][0]
    assert calls[1][0][-2:] == ("--body-file", "-")
    assert calls[1][1] == json.loads(path.read_text())["body"]
    journal = [
        json.loads(line) for line in (root / ".brieflow/decisions.jsonl").read_text().splitlines()
    ]
    assert len(journal) == 1 and journal[0]["choice"] == URL


def test_existing_remote_issue_reused(report, monkeypatch):
    _, _, path = report
    doc = json.loads(path.read_text())

    def gh(*args, **kwargs):
        assert args[1] == "list"  # Must not create, including for a closed match.
        return json.dumps([{"url": URL, "body": doc["body"]}])

    monkeypatch.setattr(issue, "_gh", gh)
    assert issue.open_report(path) == URL


def test_changed_content_rejected_before_network(report, monkeypatch):
    _, _, path = report
    doc = json.loads(path.read_text())
    doc["body"] = doc["body"].replace("defaults", "global defaults")
    path.write_text(json.dumps(doc))
    monkeypatch.setattr(issue, "_gh", lambda *a, **k: pytest.fail("Must not contact GitHub"))
    with pytest.raises(ValueError, match="changed"):
        issue.open_report(path)


@pytest.mark.parametrize("error", [RuntimeError("failed"), subprocess.TimeoutExpired("gh", 60)])
def test_uncertain_creation_never_blindly_retried(report, monkeypatch, error):
    _, _, path = report
    creates = []

    def gh(*args, **kwargs):
        if args[1] == "list":
            return "[]"
        creates.append(args)
        raise error

    monkeypatch.setattr(issue, "_gh", gh)
    with pytest.raises(type(error)):
        issue.open_report(path)
    assert json.loads(path.read_text())["status"] == "submission-unknown"
    with pytest.raises(RuntimeError, match="outcome is unknown"):
        issue.open_report(path)
    assert len(creates) == 1
    doc = json.loads(path.read_text())
    monkeypatch.setattr(
        issue, "_gh", lambda *a, **k: json.dumps([{"url": URL, "body": doc["body"]}])
    )
    assert issue.open_report(path) == URL


def test_auth_or_search_failure_does_not_attempt_creation(report, monkeypatch):
    _, _, path = report

    def gh(*args, **kwargs):
        assert args[1] == "list"
        raise RuntimeError("auth unavailable")

    monkeypatch.setattr(issue, "_gh", gh)
    with pytest.raises(RuntimeError):
        issue.open_report(path)
    assert json.loads(path.read_text())["status"] == "prepared"


def test_shell_metacharacters_are_only_data(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, URL, "")

    monkeypatch.setattr(issue.subprocess, "run", run)
    title = "title $(do-not-run) `also-not-run`"
    assert issue._gh("issue", "create", "--title", title, body="private $VARIABLE") == URL
    argv, kwargs = calls[0]
    assert argv[-1] == title
    assert not kwargs.get("shell")
    assert kwargs["input"] == "private $VARIABLE"
    assert kwargs["timeout"] == 60


def test_unexpected_url_not_recorded_as_success(report, monkeypatch):
    _, _, path = report
    monkeypatch.setattr(
        issue, "_gh", lambda *a, **k: "[]" if a[1] == "list" else "https://example.com/1"
    )
    with pytest.raises(ValueError, match="URL"):
        issue.open_report(path)
    assert json.loads(path.read_text())["filed_url"] is None


def test_receipt_survives_journal_error(report, monkeypatch):
    _, _, path = report
    monkeypatch.setattr(issue, "_gh", lambda *a, **k: "[]" if a[1] == "list" else URL)
    monkeypatch.setattr(
        issue, "record_decision", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only"))
    )
    assert issue.open_report(path) == URL
    assert json.loads(path.read_text())["filed_url"] == URL

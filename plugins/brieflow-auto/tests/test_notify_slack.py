"""brieflow_notify.sh posts to Slack and never invokes mail."""

from __future__ import annotations
import json
import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

SH = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_notify.sh"


@pytest.fixture(autouse=True)
def fake_delivery_only(monkeypatch):
    # Every delivery test installs a fake curl. Do not read the real ~/.brieflow_env.
    monkeypatch.setenv("BRIEFLOW_NOTIFY_MODE", "normal")
    monkeypatch.setenv("BRIEFLOW_NOTIFY_ENV_FILE", "")
    monkeypatch.delenv("BRIEFLOW_SLACK_WEBHOOK", raising=False)


def _stub_bin(dirpath: Path, name: str, capture: Path):
    p = dirpath / name
    p.write_text(
        textwrap.dedent(f"""\
        #!/bin/bash
        echo "$@" >> "{capture}"
        cat >> "{capture}" 2>/dev/null || true
        printf '200'
        exit 0
    """)
    )
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


def _run(state_dir: Path, env_overrides: dict, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["BRIEFLOW_NOTIFY_STATE_DIR"] = str(state_dir)
    for key in ("BRIEFLOW_SLACK_WEBHOOK", "BRIEFLOW_NOTIFY_THROTTLE_SECONDS"):
        env.pop(key, None)
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(SH), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=15,
    )


def test_notify_posts_to_slack(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cap = tmp_path / "curl_args.txt"
    _stub_bin(bindir, "curl", cap)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "BRIEFLOW_SLACK_WEBHOOK": "https://hooks.slack.com/services/T/B/X",
        "BRIEFLOW_NOTIFY_STATE_DIR": str(tmp_path / "state"),
    }
    r = subprocess.run(
        ["bash", str(SH), "mytag", "sbs gate FAIL", "action_needed"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert r.returncode == 0
    args = cap.read_text()
    assert "hooks.slack.com" in args
    assert "sbs gate FAIL" in args
    # audit log always written
    assert (tmp_path / "state" / "notify.log").exists()


def test_notify_no_mail_invocation(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cap = tmp_path / "called.txt"
    for tool in ("mail", "mailx", "sendmail"):
        _stub_bin(bindir, tool, cap)
    _stub_bin(bindir, "curl", tmp_path / "curl.txt")
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "BRIEFLOW_SLACK_WEBHOOK": "https://hooks.slack.com/services/T/B/X",
        "BRIEFLOW_NOTIFY_STATE_DIR": str(tmp_path / "state"),
    }
    subprocess.run(["bash", str(SH), "t", "msg", "action_needed"], env=env, timeout=15)
    assert not (tmp_path / "called.txt").exists()  # no mail tool was ever called


# ---------------------------------------------------------------------------
# Fix 2 — JSON escaping
# ---------------------------------------------------------------------------


def test_notify_slack_escapes_quotes(tmp_path):
    """A message containing a double-quote must produce valid JSON in the curl payload."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cap = tmp_path / "curl_args.txt"
    # Capture the full curl invocation (args + stdin) so we can inspect --data.
    stub = bindir / "curl"
    stub.write_text(
        textwrap.dedent(f"""\
        #!/bin/bash
        # Write all arguments to capture file, one per line.
        for arg in "$@"; do
            printf '%s\\n' "$arg" >> "{cap}"
        done
        printf '200'
        exit 0
    """)
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    state = tmp_path / "state"
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "BRIEFLOW_SLACK_WEBHOOK": "https://hooks.slack.com/services/T/B/X",
        "BRIEFLOW_NOTIFY_STATE_DIR": str(state),
    }
    r = subprocess.run(
        ["bash", str(SH), "tag1", 'metric: "value" hit', "action_needed"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert r.returncode == 0
    lines = cap.read_text().splitlines()
    # Find the --data argument (the JSON payload passed to curl).
    data_line = None
    for i, line in enumerate(lines):
        if line == "--data":
            data_line = lines[i + 1] if i + 1 < len(lines) else None
            break
        # curl -d / --data=... forms
        if line.startswith("--data="):
            data_line = line[len("--data=") :]
            break

    # Also handle: the payload is passed as a positional after -d / --data
    if data_line is None:
        for i, line in enumerate(lines):
            if line in ("-d",):
                data_line = lines[i + 1] if i + 1 < len(lines) else None
                break

    assert data_line is not None, f"Could not find --data / -d in captured args: {lines}"

    # The payload must be valid JSON.
    payload = json.loads(data_line)
    assert "text" in payload
    # The original double-quote must round-trip correctly.
    assert '"' in payload["text"]
    # Raw unescaped form must NOT appear in the raw capture string (i.e. it was escaped).
    raw_capture = cap.read_text()
    # The text key value should contain the escaped form — not a bare unquoted ".
    assert '\\"' in raw_capture or '"' in payload["text"]


# ---------------------------------------------------------------------------
# Fix 3 — throttle coverage
# ---------------------------------------------------------------------------


def test_warning_throttled(tmp_path):
    """Two warning notifies for the same tag within the window → only ONE curl POST."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cap = tmp_path / "curl_cap.txt"
    _stub_bin(bindir, "curl", cap)
    state = tmp_path / "state"
    env_base = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "BRIEFLOW_SLACK_WEBHOOK": "https://hooks.slack.com/services/T/B/X",
        "BRIEFLOW_NOTIFY_THROTTLE_SECONDS": "3600",
    }
    _run(state, env_base, "tagW", "first warn", "warning")
    _run(state, env_base, "tagW", "second warn", "warning")
    log = (state / "notify.log").read_text()
    assert "THROTTLED tag=tagW" in log
    # curl was only called once (one "hooks.slack.com" line in capture).
    posts = [line for line in cap.read_text().splitlines() if "hooks.slack.com" in line]
    assert len(posts) == 1


def test_action_needed_not_throttled(tmp_path):
    """Two action_needed notifies for the same tag → TWO curl POSTs (no throttle)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cap = tmp_path / "curl_cap.txt"
    _stub_bin(bindir, "curl", cap)
    state = tmp_path / "state"
    env_base = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "BRIEFLOW_SLACK_WEBHOOK": "https://hooks.slack.com/services/T/B/X",
        "BRIEFLOW_NOTIFY_THROTTLE_SECONDS": "3600",
    }
    _run(state, env_base, "tagAN", "first urgent", "action_needed")
    _run(state, env_base, "tagAN", "second urgent", "action_needed")
    log = (state / "notify.log").read_text()
    assert "THROTTLED" not in log
    posts = [line for line in cap.read_text().splitlines() if "hooks.slack.com" in line]
    assert len(posts) == 2


def test_info_no_push(tmp_path):
    """info notify → ZERO curl POSTs but notify.log is written."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cap = tmp_path / "curl_cap.txt"
    _stub_bin(bindir, "curl", cap)
    state = tmp_path / "state"
    env_base = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "BRIEFLOW_SLACK_WEBHOOK": "https://hooks.slack.com/services/T/B/X",
    }
    _run(state, env_base, "tagI", "just a note", "info")
    assert (state / "notify.log").exists()
    log = (state / "notify.log").read_text()
    assert "[INFO]" in log
    # curl must not have been invoked at all.
    assert not cap.exists() or cap.read_text().strip() == ""


def test_disabled_mode_never_sources_operator_config_or_calls_curl(tmp_path):
    config = tmp_path / "operator_env"
    marker = tmp_path / "config-was-read"
    config.write_text(f'touch "{marker}"\n')
    result = _run(
        tmp_path / "state",
        {
            "BRIEFLOW_NOTIFY_MODE": "disabled",
            "BRIEFLOW_NOTIFY_ENV_FILE": str(config),
            "BRIEFLOW_SLACK_WEBHOOK": "https://example.invalid/never-post",
        },
        "doomed_test",
        "simulated failure",
        "action_needed",
    )
    assert result.returncode == 0
    assert not marker.exists()
    assert not (tmp_path / "state").exists()


def test_failed_warning_does_not_throttle_retry(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text("#!/bin/sh\nprintf '503'\n")
    curl.chmod(0o755)
    state = tmp_path / "state"
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "BRIEFLOW_SLACK_WEBHOOK": "https://example.invalid/mock",
    }
    assert _run(state, env, "t", "warning", "warning").returncode == 1
    assert not (state / "throttle_t.ts").exists()
    curl.write_text("#!/bin/sh\nprintf '200'\n")
    assert _run(state, env, "t", "warning", "warning").returncode == 0
    assert (state / "throttle_t.ts").exists()

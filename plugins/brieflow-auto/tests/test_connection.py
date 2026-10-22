import json
import shlex
import subprocess
import sys

import pytest

import brieflow_connection as conn


def metadata(enabled):
    return {"items": [{"key": "enable-oslogin", "value": enabled}]}


@pytest.fixture
def gcp(monkeypatch):
    calls = []
    state = {
        "instance": {"id": "123456", "metadata": {}},
        "project": {"commonInstanceMetadata": metadata("TRUE")},
        "profile": {
            "posixAccounts": [{"username": "assigned_user", "operatingSystemType": "LINUX"}]
        },
    }

    def run(argv):
        calls.append(argv)
        assert "--account=operator@example.org" in argv
        assert "--project=example-project" in argv
        assert not any(x in argv for x in ("add-metadata", "create", "delete"))
        if "instances" in argv:
            return json.dumps(state["instance"])
        if "project-info" in argv:
            return json.dumps(state["project"])
        if "os-login" in argv:
            return json.dumps(state["profile"])
        assert "ssh" in argv
        return "Welcome\n" + conn.MARKER + json.dumps(state["observed"])

    monkeypatch.setattr(conn, "run", run)
    return state, calls


def plan(**kwargs):
    return conn.plan(
        transport="gcp-iap",
        project="example-project",
        zone="zone-a",
        instance="screen-vm",
        account="operator@example.org",
        workdir="/data/analysis",
        **kwargs,
    )


def test_oslogin_identity_is_independent_of_client_username(gcp, monkeypatch):
    monkeypatch.setenv("USER", "laptop_name")
    first = plan()
    monkeypatch.setenv("USER", "cluster_name")
    second = plan()
    assert conn.connection(first) == conn.connection(second)
    assert first["remote_user"] == "assigned_user"
    assert first["status"] == "unverified"
    assert "assigned_user@screen-vm" in conn.connection(first)
    assert "--tunnel-through-iap" in conn.connection(first)
    assert not any("ssh" in call for call in gcp[1])


def test_instance_metadata_overrides_project_and_requires_explicit_user(gcp):
    gcp[0]["instance"]["metadata"] = metadata("FALSE")
    with pytest.raises(ValueError, match="remote-user"):
        plan()
    result = plan(remote_user="chosen_user")
    assert result["auth_mode"] == "ssh-keys"
    assert result["remote_user"] == "chosen_user"


def test_profile_mismatch_or_ambiguity_never_guesses(gcp):
    with pytest.raises(ValueError, match="not in"):
        plan(remote_user="email_local_part")
    gcp[0]["profile"]["posixAccounts"].append({"username": "another_user"})
    with pytest.raises(ValueError, match="ambiguous"):
        plan()
    assert plan(remote_user="assigned_user")["remote_user"] == "assigned_user"


def observed(**kwargs):
    return dict(
        username="assigned_user",
        uid=1001,
        gid=1001,
        home="/home/assigned_user",
        hostname="screen-vm",
        workdir="/data/analysis",
        directory_exists=True,
        directory_access=True,
        directory_owner_uid=1001,
        **kwargs,
    )


def test_verify_records_real_identity_and_remote_only_handoff(gcp):
    record = plan()
    gcp[0]["observed"] = observed()
    assert conn.verify(record)["status"] == "verified"
    cmd = gcp[1][-1][-1].removeprefix("--command=")
    assert shlex.split(cmd) == ["python3", "-c", conn.PROBE, "/data/analysis"]
    text = conn.handoff(record)
    assert "tmux new-session -A" in text and "claude" in text
    assert "Cloud account: operator@example.org" in text
    assert record["verification"]["observed"]["uid"] == 1001


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("username", "wrong_user", "identity mismatch"),
        ("directory_access", False, "inaccessible"),
        ("directory_exists", False, "missing"),
        ("uid", 0, "identity mismatch"),
    ],
)
def test_failed_verification_cannot_retain_success(gcp, field, value, match):
    record = plan()
    record.update(status="verified", verification={"old": True})
    gcp[0]["observed"] = observed()
    gcp[0]["observed"][field] = value
    with pytest.raises(ValueError, match=match):
        conn.verify(record)
    assert record["status"] != "verified"
    assert "old" not in record.get("verification", {})


def test_recreated_vm_is_not_same_destination(gcp):
    record = plan()
    gcp[0]["instance"]["id"] = "different-id"
    with pytest.raises(ValueError, match="VM identity"):
        conn.verify(record)
    assert not any("ssh" in call for call in gcp[1])


def test_failed_cloud_lookup_never_falls_back(monkeypatch):
    def fail(argv):
        raise RuntimeError("permission denied")

    monkeypatch.setattr(conn, "run", fail)
    with pytest.raises(RuntimeError, match="permission denied"):
        plan(remote_user="chosen_user")


def test_subprocess_is_bounded_noninteractive_and_does_not_echo_credentials(monkeypatch):
    def failed(argv, **kwargs):
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["timeout"] == 120
        return subprocess.CompletedProcess(argv, 1, "", "sensitive diagnostic")

    monkeypatch.setattr(conn.subprocess, "run", failed)
    with pytest.raises(RuntimeError) as error:
        conn.run(["gcloud", "compute", "ssh", "operator@host"])
    assert "sensitive diagnostic" not in str(error.value)
    assert "interactively" in str(error.value)


def test_generic_ssh_and_shell_quoting(monkeypatch):
    path = "/data/a folder/$(do-not-run); 'quoted'"
    record = conn.plan(
        transport="ssh",
        host="server.example.org",
        port=2222,
        remote_user="assigned_user",
        workdir=path,
    )
    calls = []

    def run(argv):
        calls.append(argv)
        return conn.MARKER + json.dumps(observed())

    monkeypatch.setattr(conn, "run", run)
    conn.verify(record)
    assert calls[0][:6] == ["ssh", "-p", "2222", "-l", "assigned_user", "server.example.org"]
    assert shlex.split(calls[0][-1])[-1] == path


@pytest.mark.parametrize(
    "change",
    [
        {"remote_user": "operator@example.org"},
        {"remote_user": "root"},
        {"host": "-oProxyCommand=bad"},
        {"host": "other@host"},
        {"port": 0},
        {"workdir": "relative"},
        {"workdir": "/data\ncommand"},
    ],
)
def test_invalid_connection_values(change):
    args = dict(transport="ssh", host="server", port=22, remote_user="operator", workdir="/data")
    args.update(change)
    with pytest.raises(ValueError):
        conn.plan(**args)


def test_real_readonly_probe_and_private_record(tmp_path):
    # Exercise the exact probe locally, without an SSH connection or filesystem mutations.
    result = subprocess.run(
        [sys.executable, "-c", conn.PROBE, str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    info = json.loads(result.stdout.removeprefix(conn.MARKER))
    assert info["directory_access"] is True
    assert info["directory_exists"] is True
    record = tmp_path / "connection.json"
    conn.save(record, {"schema": conn.SCHEMA})
    assert record.stat().st_mode & 0o777 == 0o600
    assert json.loads(record.read_text())["schema"] == conn.SCHEMA


def test_remote_identity_remains_pinned_after_access_failure(gcp):
    record = plan()
    gcp[0]["observed"] = observed()
    conn.verify(record)
    gcp[0]["observed"]["uid"] = 1002
    for _ in range(2):
        with pytest.raises(ValueError, match="UID"):
            conn.verify(record)
        assert record["status"] == "unverified"
        assert record["remote_identity"]["uid"] == 1001


def test_cli_failed_verify_persists_unverified_state(tmp_path, monkeypatch):
    record = conn.plan(
        transport="ssh", host="host", port=22, remote_user="operator", workdir="/data"
    )
    record.update(status="verified", verification={"old": True})
    path = tmp_path / "connection.json"
    conn.save(path, record)
    monkeypatch.setattr(sys, "argv", ["brieflow_connection.py", "verify", "--record", str(path)])

    def fail(argv):
        raise subprocess.TimeoutExpired(argv, 120)

    monkeypatch.setattr(conn, "run", fail)
    with pytest.raises(SystemExit) as error:
        conn.main()
    assert error.value.code == 1
    result = json.loads(path.read_text())
    assert result["status"] == "unverified"
    assert "verification" not in result


def test_cli_plan_show_and_existing_record_protection(tmp_path):
    path = tmp_path / "connection.json"
    argv = [
        sys.executable,
        conn.__file__,
        "ssh",
        "--host",
        "example.org",
        "--remote-user",
        "operator",
        "--workdir",
        "/data/analysis",
        "--output",
        str(path),
    ]
    result = subprocess.run(argv, capture_output=True, text=True, check=True)
    assert "Connection status: unverified" in result.stdout
    before = path.read_bytes()
    repeated = subprocess.run(argv, capture_output=True, text=True)
    assert repeated.returncode == 1 and path.read_bytes() == before
    shown = subprocess.run(
        [sys.executable, conn.__file__, "show", "--record", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "ssh -p 22 -l operator example.org" in shown.stdout


@pytest.mark.parametrize("google,slurm,expected", [(0, 1, "vm"), (1, 0, "hpc"), (1, 1, "local")])
def test_host_detection_is_only_a_capability_hint(google, slurm, expected):
    from pathlib import Path

    common = Path(conn.__file__).parents[1] / "cloud/_common.sh"
    # Stub both probes: no metadata request or scheduler interaction takes place.
    command = f"""source {shlex.quote(str(common))}
curl() {{ return {google}; }}
command() {{ return {slurm}; }}
detect_host
"""
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == expected

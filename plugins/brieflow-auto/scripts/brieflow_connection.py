"""Save and verify a remote login handoff; never orchestrate pipeline work over SSH.

Planning reads cloud metadata only. Verification makes one SSH connection and runs
a read-only identity/directory probe. gcloud may register the client's SSH key and
bootstrap the explicitly selected account. The helper does not issue IAM, useradd,
ownership, SSH-config, or provisioning commands. Each client keeps its own credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "brieflow-connection/v1"
MARKER = "BRIEFLOW_CONNECTION="
PROBE = """import json, os, pwd, socket, sys
p = sys.argv[1]
u = pwd.getpwuid(os.geteuid())
d = os.path.isdir(p)
print('BRIEFLOW_CONNECTION=' + json.dumps(dict(
    username=u.pw_name, uid=u.pw_uid, gid=u.pw_gid, home=u.pw_dir,
    hostname=socket.gethostname(), workdir=os.path.realpath(p),
    directory_exists=d, directory_access=d and os.access(p, os.R_OK | os.W_OK | os.X_OK),
    directory_owner_uid=os.stat(p).st_uid if d else None)))
"""


def token(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:@-]*", value):
        raise ValueError(f"Invalid {label}")
    return value


def user(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_.-]*", value):
        raise ValueError("Specify an explicit Linux username, not an email or local-user guess")
    if value == "root":
        raise ValueError("Use the operator's non-root Linux account")
    return value


def validate(record):
    if record.get("schema") != SCHEMA or record.get("transport") not in ("gcp-iap", "ssh"):
        raise ValueError("Unsupported connection record")
    user(record["remote_user"])
    path = record["workdir"]
    if not isinstance(path, str) or not path.startswith("/") or any(ord(c) < 32 for c in path):
        raise ValueError("Workdir must be an absolute remote path without control characters")
    if record["transport"] == "gcp-iap":
        for key in ("project", "zone", "instance", "account", "instance_id"):
            token(record[key], key)
    else:
        token(record["host"], "host")
        if "@" in record["host"]:
            raise ValueError("Host must not embed a username; use --remote-user")
        if not isinstance(record["port"], int) or not 1 <= record["port"] <= 65535:
            raise ValueError("Port must be 1–65535")


def run(argv):
    result = subprocess.run(
        argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120
    )
    if result.returncode:
        raise RuntimeError(
            f"Connection check failed (exit {result.returncode}). Check the saved account, "
            "IAM/IAP permissions, SSH credentials, host reachability, and remote python3. "
            "Use the printed connection interactively for first-time authentication, then verify. "
            "Do not change the remote username or ownership to bypass this failure."
        )
    return result.stdout


def cloud(record, *args):
    return json.loads(
        run(
            [
                "gcloud",
                *args,
                f"--project={record['project']}",
                f"--account={record['account']}",
                "--format=json",
            ]
        )
    )


def os_login(project, instance):
    def value(metadata):
        return {x["key"]: x.get("value", "") for x in metadata.get("items", [])}

    merged = value(project.get("commonInstanceMetadata", {}))
    merged.update(value(instance.get("metadata", {})))
    enabled = str(merged.get("enable-oslogin", "FALSE")).upper()
    if enabled not in ("TRUE", "FALSE"):
        raise ValueError("Unrecognized enable-oslogin setting; resolve with the administrator")
    return enabled == "TRUE"


def discover(record, requested_user=None):
    instance = cloud(
        record, "compute", "instances", "describe", record["instance"], f"--zone={record['zone']}"
    )
    project = cloud(record, "compute", "project-info", "describe")
    if os_login(project, instance):
        profile = cloud(record, "compute", "os-login", "describe-profile")
        names = {
            a["username"]
            for a in profile.get("posixAccounts", [])
            if a.get("username") and a.get("operatingSystemType", "LINUX") == "LINUX"
        }
        if requested_user and requested_user not in names:
            raise ValueError("Requested Linux user is not in this cloud account's OS Login profile")
        if not requested_user and len(names) != 1:
            raise ValueError("OS Login username is missing or ambiguous; specify the assigned user")
        resolved = requested_user or next(iter(names))
        mode = "os-login"
    else:
        if not requested_user:
            raise ValueError("OS Login is off: --remote-user is required; do not infer it")
        resolved, mode = requested_user, "ssh-keys"
    return {"remote_user": user(resolved), "auth_mode": mode, "instance_id": str(instance["id"])}


def connection(record):
    validate(record)
    if record["transport"] == "gcp-iap":
        return [
            "gcloud",
            "compute",
            "ssh",
            f"{record['remote_user']}@{record['instance']}",
            f"--project={record['project']}",
            f"--zone={record['zone']}",
            f"--account={record['account']}",
            "--tunnel-through-iap",
        ]
    return ["ssh", "-p", str(record["port"]), "-l", record["remote_user"], record["host"]]


def plan(*, transport, workdir, remote_user=None, **fields):
    record = {
        "schema": SCHEMA,
        "transport": transport,
        "workdir": workdir,
        "status": "unverified",
        **fields,
    }
    if transport == "gcp-iap":
        for key in ("project", "zone", "instance", "account"):
            token(record[key], key)
        record.update(discover(record, remote_user))
    else:
        record.update(remote_user=user(remote_user), auth_mode="ssh-config")
    validate(record)
    return record


def verify(record):
    validate(record)
    # Invalidate the previous success before attempting a new connection.
    record["status"] = "unverified"
    record.pop("verification", None)
    if record["transport"] == "gcp-iap":
        current = discover(record, record["remote_user"])
        if any(current[k] != record[k] for k in ("instance_id", "auth_mode", "remote_user")):
            raise ValueError("VM identity or login mode changed; create a new connection record")
    command = shlex.join(["python3", "-c", PROBE, record["workdir"]])
    argv = connection(record)
    argv += [f"--command={command}"] if record["transport"] == "gcp-iap" else [command]
    output = run(argv)
    lines = [line[len(MARKER) :] for line in output.splitlines() if line.startswith(MARKER)]
    if len(lines) != 1:
        raise ValueError("Remote identity probe returned no unique result")
    observed = json.loads(lines[0])
    if observed["username"] != record["remote_user"] or observed["uid"] == 0:
        raise ValueError(
            "Remote identity mismatch; do not create a second user or change ownership"
        )
    identity = {k: observed[k] for k in ("username", "uid", "gid", "home")}
    if record.get("remote_identity", identity) != identity:
        raise ValueError("Remote UID, group, or home changed; review access before a new handoff")
    record["remote_identity"] = identity
    record["verification"] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "client_host": socket.gethostname(),
        "observed": observed,
    }
    if not observed["directory_exists"] or not observed["directory_access"]:
        record["status"] = "identity-verified-directory-blocked"
        raise ValueError(
            "Login verified, but the workspace is missing or inaccessible; complete "
            "on-machine setup as this user, then verify the analysis directory again"
        )
    record["status"] = "verified"
    return record


def save(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".connection-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream, indent=2)
            stream.write("\n")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def handoff(record):
    cmd = shlex.join(connection(record))
    remote = shlex.quote(record["workdir"])
    suffix = hashlib.sha256(record["workdir"].encode()).hexdigest()[:8]
    session = "brieflow-" + suffix
    return f"""Connection status: {record["status"]}
Remote Linux account: {record["remote_user"]}
Cloud account: {record.get("account", "not managed here; use your own SSH credentials")}
From your laptop OR setup workstation (same remote identity):
  {cmd}
On the remote machine, after confirming identity and directory access:
  whoami
  cd -- {remote}
  tmux new-session -A -s {shlex.quote(session)}
Inside the remote tmux session:
  cd -- {remote}
  claude
If attaching to an existing session, do not start another Claude process.
Authenticate Claude Code on this machine as yourself; never copy another user's credentials.
Claude Code, notebooks, and the runner execute HERE on the remote machine.
Detach with Ctrl-b then d; reconnect using the same command and tmux session.
Disconnecting your client does not stop tmux; stopping/rebooting the VM does.
Verify again from each new client before treating this as a working connection there.
No pipeline command is launched over SSH by this helper.
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for kind in ("gcp", "ssh"):
        p = sub.add_parser(kind)
        p.add_argument("--workdir", required=True)
        p.add_argument("--remote-user", required=kind == "ssh")
        p.add_argument("--output", type=Path, required=True)
        if kind == "gcp":
            p.add_argument("--project", required=True)
            p.add_argument("--zone", required=True)
            p.add_argument("--instance", required=True)
            p.add_argument("--account", required=True)
        else:
            p.add_argument("--host", required=True)
            p.add_argument("--port", type=int, default=22)
    for kind in ("show", "verify"):
        p = sub.add_parser(kind)
        p.add_argument("--record", type=Path, required=True)
    args = vars(parser.parse_args())
    action = args.pop("action")
    try:
        if action in ("gcp", "ssh"):
            path = args.pop("output")
            if path.exists():
                raise ValueError("Connection record already exists; verify it or choose a new path")
            record = plan(transport="gcp-iap" if action == "gcp" else "ssh", **args)
            save(path, record)
        else:
            path = args["record"]
            record = json.loads(path.read_text())
            if action == "verify":
                try:
                    verify(record)
                finally:
                    save(path, record)
        print(handoff(record))
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"Connection handoff blocked: {error}\n")


if __name__ == "__main__":
    main()

"""Claude Code Stop hook: retain transcripts only in explicitly enabled workspaces."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from brieflow_trace import _save, attach

IGNORE_RULES = ("**/.brieflow/rollouts/", "**/.brieflow/traces/")


def ignore_records(repo: Path) -> Path:
    """Append the private-record ignore rules to a repository's .gitignore, once."""
    path = repo.resolve() / ".gitignore"
    lines = path.read_text().splitlines() if path.is_file() else []
    missing = [rule for rule in IGNORE_RULES if rule not in lines]
    if missing:
        prefix = "\n" if lines and lines[-1].strip() else ""
        with path.open("a") as stream:
            stream.write(prefix + "# brieflow-auto private run records\n")
            stream.write("".join(f"{rule}\n" for rule in missing))
    return path


def enable(workspace: Path, repos: list[Path] = ()) -> list[Path]:
    state = workspace.resolve() / ".brieflow"
    state.mkdir(parents=True, exist_ok=True)
    _save(state / "record_rollouts.json", {"enabled": True, "local_only": True})
    # The workspace's own repository, if any, plus the screen repos named by the caller.
    root = workspace.resolve()
    enclosing = [d for d in (root, *root.parents) if (d / ".git").exists()]
    targets = {r.resolve() for r in repos} | set(enclosing[:1])
    return [ignore_records(r) for r in sorted(targets)]


def capture(payload):
    workspace = Path(payload["cwd"]).resolve()
    config = workspace / ".brieflow" / "record_rollouts.json"
    if not config.is_file() or json.loads(config.read_text()).get("enabled") is not True:
        return None
    session = str(payload["session_id"])
    # Runtime IDs never become path components without normalization.
    key = hashlib.sha256(session.encode()).hexdigest()[:24]
    root = workspace / ".brieflow" / "rollouts"
    root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    destination = root / key
    destination.mkdir(mode=0o700, exist_ok=True)
    if not (destination / "trace.json").exists():
        _save(
            destination / "trace.json",
            {
                "schema": "brieflow-trace/v1",
                "kind": "agent-session",
                "agent_rollout": "not-supplied",
                "public_release": False,
            },
        )
    return attach(destination, Path(payload["transcript_path"]), "claude-code", session)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-workspace", type=Path)
    parser.add_argument("--ignore-repo", type=Path, action="append", default=[])
    args = parser.parse_args()
    if args.enable_workspace:
        for path in enable(args.enable_workspace, args.ignore_repo):
            print(f"ignore rules present in {path}")
        return
    try:
        capture(json.load(sys.stdin))
    except (OSError, ValueError, KeyError) as error:
        # Never block the conversation, but do not silently claim capture succeeded.
        print(f"Brieflow rollout capture skipped: {error}", file=sys.stderr)


if __name__ == "__main__":
    main()

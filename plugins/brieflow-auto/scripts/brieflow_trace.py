"""Local, per-invocation harness records and explicit runtime transcript imports.

This captures Python runner output, not hidden model reasoning or every child process.
Pipeline subprocess logs remain in analysis/logs and are indexed, not duplicated.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import hashlib
import json
import os
import shutil
import sys
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

RECORDS = (
    "run_manifest.json",
    "interview.json",
    "decisions.jsonl",
    "findings.json",
    "progress.json",
    "issue_draft.json",
)


def _save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def _snapshot(analysis, destination):
    destination.mkdir()
    for name in RECORDS:
        source = analysis / ".brieflow" / name
        if source.is_file() and not source.is_symlink():
            shutil.copyfile(source, destination / name)


class Tee:
    def __init__(self, stream, log):
        self.stream, self.log = stream, log

    def write(self, value):
        self.log.write(value)
        self.log.flush()
        return self.stream.write(value)

    def flush(self):
        self.log.flush()
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def traced(function):
    """Keep start/end evidence even on early returns; tracing errors never launch work."""

    @functools.wraps(function)
    def wrapped(args):
        analysis = Path(args.analysis_dir)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run = analysis / ".brieflow" / "traces" / f"{stamp}-{uuid.uuid4().hex[:12]}"
        # Fail before execution if requested audit records cannot be written.
        run.mkdir(parents=True, mode=0o700)
        os.chmod(run.parent, 0o700)
        _snapshot(analysis, run / "before")
        manifest = {
            "schema": "brieflow-trace/v1",
            "started_at": stamp,
            "status": "running",
            "tag": getattr(args, "tag", None),
            "agent_rollout": "not-supplied",
            "public_release": False,
        }
        # Lets collect join this trace to the session rollout that launched it.
        if os.environ.get("CLAUDE_CODE_SESSION_ID"):
            manifest["agent_session_id"] = os.environ["CLAUDE_CODE_SESSION_ID"]
        _save(run / "trace.json", manifest)
        try:
            with (run / "runner.log").open("w") as log:
                with (
                    contextlib.redirect_stdout(Tee(sys.stdout, log)),
                    contextlib.redirect_stderr(Tee(sys.stderr, log)),
                ):
                    result = function(args)
            manifest.update(status="returned", exit_code=result)
            return result
        except BaseException as error:
            # Closing the console pipe (tee/tmux) or Ctrl-C is how operators stop a run.
            stopped = isinstance(error, (BrokenPipeError, KeyboardInterrupt))
            manifest.update(
                status="interrupted" if stopped else "exception",
                exception_type=type(error).__name__,
                exception_message=str(error)[:500],
                traceback=traceback.format_exc()[-4000:],
            )
            raise
        finally:
            try:
                _snapshot(analysis, run / "after")
                manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
                manifest["pipeline_logs"] = [
                    {"path": str(p.resolve()), "bytes": p.stat().st_size}
                    for p in sorted((analysis / "logs").glob("*.log"))
                    if p.is_file()
                ]
                _save(run / "trace.json", manifest)
            except OSError as error:
                print(f"Trace finalization failed: {error}", file=sys.stderr)

    return wrapped


def attach(trace: Path, source: Path, runtime: str, session_id: str):
    """Explicitly retain an exported runtime transcript, without scanning user homes."""
    manifest_path = trace / "trace.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema") != "brieflow-trace/v1":
        raise ValueError("Not a Brieflow trace directory")
    if source.is_symlink() or not source.is_file() or source.stat().st_size > 100_000_000:
        raise ValueError("Transcript must be a regular file no larger than 100 MB")
    if not runtime.strip() or not session_id.strip():
        raise ValueError("Runtime and session ID are required")
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    target = trace / f"rollout-{digest}.jsonl"
    if not target.exists():
        with target.open("xb") as stream:
            stream.write(data)
        target.chmod(0o600)
    item = {
        "file": target.name,
        "sha256": digest,
        "bytes": len(data),
        "runtime": runtime,
        "session_id": session_id,
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "completeness": "runtime-export; not independently verified",
    }
    imports = manifest.setdefault("rollouts", [])
    if not any(r["sha256"] == digest for r in imports):
        imports.append(item)
    manifest["agent_rollout"] = "supplied"
    _save(manifest_path, manifest)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-dir", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--session-id", required=True)
    args = parser.parse_args()
    print(attach(args.trace_dir, args.source, args.runtime, args.session_id))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""brieflow_watch.py — one status check of a runner launch, for a monitor loop.

Agents hand-rolled a dozen watchers on one screen (a sacct window cut at midnight, an
`[info]` filter that hid progress, another project's failed jobs counted as ours); they
missed failures for about 13 h. This is the one watcher: call it on an interval (Claude
Code Monitor or /loop) and act on its exit code.

Each call prints the runner state, the runner events new since the previous call (a cursor
in .brieflow/watch_cursor.json), and this launch's failed Slurm jobs by rule and state.
Exit codes: 0 running, 1 needs attention (paused, action_needed, or failed jobs),
2 finished, 3 runner not running and not finished (stopped or crashed).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

FAILED_STATES = "FAILED,OUT_OF_MEMORY,TIMEOUT,NODE_FAIL"
EVENT_RE = re.compile(r"\[(progress|action_needed|warning)\]")
RC_RUNNING, RC_ATTENTION, RC_DONE, RC_STOPPED = 0, 1, 2, 3


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def latest_trace(analysis_dir: Path) -> Path | None:
    traces = sorted(
        d for d in (analysis_dir / ".brieflow" / "traces").glob("*") if (d / "trace.json").is_file()
    )
    return traces[-1] if traces else None


def launch_start(analysis_dir: Path) -> datetime | None:
    """Local time this launch's manifest was written (the sacct window start)."""
    written = _load(analysis_dir / ".brieflow" / "run_manifest.json").get("written_at")
    try:
        return datetime.fromisoformat(written).astimezone().replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def runner_alive(analysis_dir: Path) -> bool:
    proc = subprocess.run(
        ["pgrep", "-f", f"brieflow_run.py --screen.*{analysis_dir}"],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0 and bool(proc.stdout.strip())


def failed_jobs(start: datetime | None) -> Counter:
    """{(rule, state): n} for this user's failed jobs since `start` (brieflow rules only)."""
    if start is None:
        return Counter()
    try:
        proc = subprocess.run(
            [
                "sacct", "-u", os.environ.get("USER", ""), "-X", "--noheader", "--parsable2",
                # With --state, sacct's end time defaults to the start time; ask up to now.
                f"--state={FAILED_STATES}", f"--starttime={start:%Y-%m-%dT%H:%M:%S}",
                "--endtime=now",
                "--format=Comment%120,State",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return Counter()
    counts: Counter = Counter()
    for line in proc.stdout.splitlines():
        comment, _, state = line.partition("|")
        # The executor tags brieflow jobs with `rule_<name>_wildcards_...`; other jobs are not ours.
        if comment.startswith("rule_"):
            rule = comment[len("rule_") :].split("_wildcards")[0]
            counts[(rule, state.split()[0] if state else "?")] += 1
    return counts


def new_events(
    analysis_dir: Path, trace: Path | None, cursor_path: Path | None = None
) -> list[str]:
    """Runner progress/action/warning lines written since the previous call."""
    if trace is None or not (trace / "runner.log").is_file():
        return []
    cursor_path = cursor_path or analysis_dir / ".brieflow" / "watch_cursor.json"
    cursor = _load(cursor_path)
    log = trace / "runner.log"
    offset = cursor.get("offset", 0) if cursor.get("trace") == trace.name else 0
    with log.open("rb") as stream:
        stream.seek(offset)
        text = stream.read().decode("utf-8", "replace")
        offset = stream.tell()
    try:
        cursor_path.write_text(json.dumps({"trace": trace.name, "offset": offset}))
    except OSError:
        pass
    return [ln for ln in text.splitlines() if EVENT_RE.search(ln)]


def watch(
    analysis_dir: Path, *, alive=runner_alive, failures=failed_jobs, cursor: Path | None = None
) -> tuple[int, list[str]]:
    analysis_dir = Path(analysis_dir).resolve()
    progress = _load(analysis_dir / ".brieflow" / "progress.json")
    trace = latest_trace(analysis_dir)
    record = _load(trace / "trace.json") if trace else {}
    events = new_events(analysis_dir, trace, cursor)
    failed = failures(launch_start(analysis_dir))
    running = alive(analysis_dir)
    paused = progress.get("paused") or {}
    if running:
        state = "running"
    elif record.get("status") == "returned" and record.get("exit_code") == 0:
        state = "finished"
    elif paused:
        state = f"paused at {paused.get('phase')}: {paused.get('reason')}"
    else:
        state = f"stopped (trace {record.get('status', 'missing')}, exit {record.get('exit_code')})"
    lines = [f"STATE: {state}", f"DONE: {', '.join(progress.get('done_phases') or []) or 'none'}"]
    lines += [f"EVENT: {e}" for e in events]
    if failed:
        summary = ", ".join(f"{rule} {st} x{n}" for (rule, st), n in sorted(failed.items()))
        lines.append(f"FAILED JOBS since launch: {summary}")
    if state == "finished":
        return RC_DONE, lines
    if not running:
        return (RC_ATTENTION if paused else RC_STOPPED), lines
    attention = failed or any("[action_needed]" in e for e in events)
    return (RC_ATTENTION if attention else RC_RUNNING), lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-dir", required=True, type=Path)
    parser.add_argument(
        "--cursor",
        type=Path,
        default=None,
        help="Cursor file (default <analysis-dir>/.brieflow/watch_cursor.json); set it to "
        "watch a screen without writing into it",
    )
    args = parser.parse_args()
    rc, lines = watch(args.analysis_dir, cursor=args.cursor)
    print("\n".join(lines))
    return rc


if __name__ == "__main__":
    sys.exit(main())

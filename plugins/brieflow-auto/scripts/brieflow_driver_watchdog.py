#!/usr/bin/env python3
"""
brieflow_driver_watchdog.py — sidecar that watches the runloop driver PID.

Solves HARDENING_TODO #24: the snakemake driver process can die from
causes external to brieflow itself (login-node OOM-killer, node panic,
slurmd restart, VM cgroup-killer). When that happens, the runloop and its
heartbeat ALSO die in the same memory event — there is no in-process
observer left to detect the loss.

This watchdog runs as a separate process (typically spawned by the runloop
with `start_new_session=True`, so it survives the runloop's death) and
polls the runloop PID. If the runloop dies before a terminal-marker
notification appears in `<analysis-dir>/.brieflow_runloop/<tag>/notify.log`,
it:

  1. Captures a diagnostic bundle: dmesg tail, df -h, ps aux | head,
     latest snakemake log tail, runloop notify.log tail.
  2. Writes the bundle to `<analysis-dir>/diag/<tag>_driver_death_<ts>.txt`.
  3. Calls brieflow_notify.sh (if executable) with an action_needed priority
     hint as the message prefix.
  4. Exits cleanly. Auto-restart is opt-in via `--auto-restart`; defaults
     OFF to avoid cascade thrash on persistent failure modes.

The watchdog also writes its own liveness file at
`<analysis-dir>/.brieflow_runloop/watchdog.pid` (containing pid + tag +
start ts), so an external monitor can probe the watchdog itself by
checking for that file plus pid-alive.
"""

from __future__ import annotations

import argparse
import atexit
import errno
import json
import os
import shlex
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
NOTIFY_SH = SCRIPT_DIR / "brieflow_notify.sh"

# Shared diagnostic-bundle helpers (HARDENING #14). Imported by name so the
# script keeps running even when launched outside a package context.
sys.path.insert(0, str(SCRIPT_DIR))
from brieflow_diag import write_diag_bundle  # noqa: E402


# ---------------------------------------------------------------------------
# Process probing
# ---------------------------------------------------------------------------


def pid_alive(pid: int) -> bool:
    """Return True iff a process with this PID is alive on this host."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM
    return True


# ---------------------------------------------------------------------------
# Liveness file for the watchdog itself
# ---------------------------------------------------------------------------


def write_watchdog_liveness(analysis_dir: Path, tag: str, runloop_pid: int) -> Path:
    lroot = analysis_dir / ".brieflow_runloop"
    lroot.mkdir(parents=True, exist_ok=True)
    p = lroot / "watchdog.pid"
    payload = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "tag": tag,
        "runloop_pid": runloop_pid,
        "started": datetime.now().isoformat(timespec="seconds"),
    }
    p.write_text(json.dumps(payload))
    atexit.register(lambda: p.unlink(missing_ok=True))
    return p


# ---------------------------------------------------------------------------
# Terminal-marker scan (avoids false-positive firing after success)
# ---------------------------------------------------------------------------


TERMINAL_MARKERS = (
    "SUCCESS attempt",  # emitted by run_one_phase on success
    "ALL ",  # emitted on multi-phase completion
    "phase exit code=0",
)


def terminal_marker_seen(analysis_dir: Path, tag: str) -> bool:
    """Scan the runloop's notify.log for an end-of-run marker.

    Multi-phase runs use per-phase sub-tags (`<tag>__pN_modules`), so the
    notify file we want is under the parent tag's state dir.
    """
    notify_log = analysis_dir / ".brieflow_runloop" / tag / "notify.log"
    if not notify_log.exists():
        return False
    try:
        text = notify_log.read_text(errors="replace")
    except OSError:
        return False
    return any(m in text for m in TERMINAL_MARKERS)


# ---------------------------------------------------------------------------
# Notify
# ---------------------------------------------------------------------------


def notify(analysis_dir: Path, tag: str, message: str, priority: str = "info") -> None:
    """Append to runloop notify.log + best-effort fan-out via brieflow_notify.sh.

    priority is a hint prefix (info|warning|action_needed) that
    brieflow_notify.sh routes on.
    """
    sd = analysis_dir / ".brieflow_runloop" / tag
    sd.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] [watchdog/{priority}] {message}"
    print(line, flush=True)
    with open(sd / "notify.log", "a") as f:
        f.write(line + "\n")
    if NOTIFY_SH.exists() and os.access(NOTIFY_SH, os.X_OK):
        try:
            subprocess.run(
                [str(NOTIFY_SH), tag, f"[{priority}] {message}"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            pass


# ---------------------------------------------------------------------------
# Watchdog loop
# ---------------------------------------------------------------------------


def watchdog_loop(
    analysis_dir: Path,
    runloop_pid: int,
    tag: str,
    poll_seconds: float,
    auto_restart: bool,
    restart_cmd: str | None,
    max_restarts: int,
) -> int:
    """Returns exit code: 0 on graceful end (runloop succeeded), 1 on driver
    death + escalation, 2 on auto-restart give-up."""
    notify(
        analysis_dir,
        tag,
        f"watchdog start: runloop_pid={runloop_pid} "
        f"poll={poll_seconds}s auto_restart={auto_restart} "
        f"max_restarts={max_restarts}",
        priority="info",
    )
    restarts_done = 0
    while True:
        time.sleep(poll_seconds)
        if not pid_alive(runloop_pid):
            # Runloop is gone. Distinguish "completed" from "died".
            if terminal_marker_seen(analysis_dir, tag):
                notify(
                    analysis_dir,
                    tag,
                    "runloop pid is gone but a terminal marker is "
                    "present in notify.log — assuming clean completion. "
                    "Watchdog exiting.",
                    priority="info",
                )
                return 0
            # Driver death.
            bundle = write_diag_bundle(
                analysis_dir,
                tag,
                "runloop pid gone before terminal marker",
                bundle_kind="driver_death",
                runloop_pid=runloop_pid,
            )
            notify(
                analysis_dir,
                tag,
                f"DRIVER DEATH: runloop pid {runloop_pid} disappeared "
                f"before terminal marker. Diag bundle: {bundle}",
                priority="action_needed",
            )
            if not auto_restart or not restart_cmd or restarts_done >= max_restarts:
                return 1
            restarts_done += 1
            notify(
                analysis_dir,
                tag,
                f"auto_restart {restarts_done}/{max_restarts}: "
                f"relaunching via: {restart_cmd}",
                priority="warning",
            )
            # Spawn the restart in a new session so we don't inherit it.
            try:
                new_proc = subprocess.Popen(
                    shlex.split(restart_cmd),
                    cwd=analysis_dir,
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                runloop_pid = new_proc.pid
                notify(
                    analysis_dir,
                    tag,
                    f"auto_restart spawned new runloop pid={runloop_pid}",
                    priority="warning",
                )
            except Exception as e:
                notify(
                    analysis_dir,
                    tag,
                    f"auto_restart FAILED: {e}",
                    priority="action_needed",
                )
                return 2


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--analysis-dir", required=True, type=Path)
    p.add_argument(
        "--runloop-pid",
        required=True,
        type=int,
        help="PID of the runloop process to watch.",
    )
    p.add_argument(
        "--tag",
        required=True,
        help="Runloop tag — used to scope notify.log + diag bundles.",
    )
    p.add_argument("--poll-seconds", type=float, default=30.0)
    p.add_argument(
        "--auto-restart",
        action="store_true",
        help="If driver dies, attempt to relaunch (default off).",
    )
    p.add_argument(
        "--restart-cmd",
        default=None,
        help="Shell command to relaunch the runloop on death. "
        "Required when --auto-restart is set.",
    )
    p.add_argument(
        "--max-restarts",
        type=int,
        default=2,
        help="Cap on auto-restart attempts before giving up.",
    )
    args = p.parse_args()

    analysis_dir = args.analysis_dir.resolve()
    if not analysis_dir.exists():
        print(f"ERROR: --analysis-dir {analysis_dir} does not exist.", file=sys.stderr)
        return 1
    if args.auto_restart and not args.restart_cmd:
        print("ERROR: --auto-restart requires --restart-cmd.", file=sys.stderr)
        return 1

    write_watchdog_liveness(analysis_dir, args.tag, args.runloop_pid)
    return watchdog_loop(
        analysis_dir=analysis_dir,
        runloop_pid=args.runloop_pid,
        tag=args.tag,
        poll_seconds=args.poll_seconds,
        auto_restart=args.auto_restart,
        restart_cmd=args.restart_cmd,
        max_restarts=args.max_restarts,
    )


if __name__ == "__main__":
    sys.exit(main())

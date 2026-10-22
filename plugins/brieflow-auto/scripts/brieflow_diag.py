"""brieflow_diag — shared diagnostic-bundle helpers.

HARDENING_TODO #14. When an escalation happens (runloop recover.py decides
to give up, driver watchdog detects driver death, etc.), the operator
needs full context in one place: log tails, system state, recent slurm
activity. This module owns the capture + serialize logic so the same
bundle format is produced regardless of who calls it.

Bundle contents (all best-effort, never raises on individual capture
failure):
  - dmesg tail (kernel ring buffer — catches oom-killer events)
  - df -h (disk pressure)
  - ps aux | head -n 20 (top processes by VSS)
  - snakemake master log tail
  - runloop notify.log tail

Backend-specific captures (slurm sacct, per-rule slurm logs) live behind
`BackendAdapter` in brieflow_backend.py.
"""

from __future__ import annotations

import os
import shlex
import socket
import subprocess
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Low-level capture helpers
# ---------------------------------------------------------------------------


def capture_cmd(cmd: list[str], timeout: float = 10.0) -> str:
    """Run cmd, return stdout+stderr. Never raises; returns error-prefixed
    string on failure so the bundle stays valid even when one capture errors."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return r.stdout + r.stderr
    except Exception as e:
        return f"[error running {shlex.join(cmd)}: {e}]"


def tail_file(path: Path, n: int = 50) -> str:
    """Return the last `n` lines of `path`. Error-prefixed string on failure."""
    try:
        with open(path, errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError as e:
        return f"[error reading {path}: {e}]"


def latest_snakemake_log(analysis_dir: Path) -> Path | None:
    """Find the most-recent .snakemake/log/*.snakemake.log under analysis_dir."""
    logs = sorted(
        (analysis_dir / ".snakemake" / "log").glob("*.snakemake.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return logs[0] if logs else None


# ---------------------------------------------------------------------------
# Bundle writer
# ---------------------------------------------------------------------------


def write_diag_bundle(
    analysis_dir: Path,
    tag: str,
    reason: str,
    *,
    bundle_kind: str = "diag",
    runloop_pid: int | None = None,
    extra_sections: list[tuple[str, str]] | None = None,
) -> Path:
    """Capture a diagnostic bundle and write it to disk. Returns the path.

    Args:
      analysis_dir: per-screen analysis dir.
      tag: runloop tag — used to locate the notify.log and namespace the file.
      reason: free-text reason for the bundle (driver-death, escalation, etc.).
      bundle_kind: filename suffix kind. Suggested values: "driver_death",
        "escalate", "diag".
      runloop_pid: optional PID to record in the bundle header.
      extra_sections: optional list of (title, body) tuples appended after
        the standard captures. Useful for caller-specific context (slurm
        sacct rows, per-rule logs, etc.).

    Bundle lives at <analysis-dir>/diag/<tag>_<bundle_kind>_<timestamp>.txt.
    """
    diag_dir = analysis_dir / "diag"
    diag_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = diag_dir / f"{tag}_{bundle_kind}_{ts}.txt"

    sections: list[tuple[str, str]] = []
    header_lines = [
        f"tag: {tag}",
        f"bundle_kind: {bundle_kind}",
        f"writer_pid: {os.getpid()}",
        f"host: {socket.gethostname()}",
        f"timestamp: {datetime.now().isoformat()}",
        f"reason: {reason}",
    ]
    if runloop_pid is not None:
        header_lines.insert(2, f"runloop_pid: {runloop_pid}")
    sections.append(("HEADER", "\n".join(header_lines) + "\n"))

    sections.append(
        ("dmesg | tail -n 50", capture_cmd(["bash", "-c", "dmesg 2>&1 | tail -n 50"]))
    )
    sections.append(("df -h", capture_cmd(["df", "-h"])))
    sections.append(
        ("ps aux | head -n 20", capture_cmd(["bash", "-c", "ps aux | head -n 20"]))
    )

    notify_log = analysis_dir / ".brieflow_runloop" / tag / "notify.log"
    if notify_log.exists():
        sections.append(
            (f"runloop notify.log tail ({notify_log})", tail_file(notify_log, n=80))
        )

    snake_log = latest_snakemake_log(analysis_dir)
    if snake_log is not None:
        sections.append(
            (f"snakemake log tail ({snake_log.name})", tail_file(snake_log, n=80))
        )

    if extra_sections:
        sections.extend(extra_sections)

    with open(out, "w") as f:
        for title, body in sections:
            f.write(f"========== {title} ==========\n")
            f.write(body.rstrip("\n") + "\n\n")
    return out

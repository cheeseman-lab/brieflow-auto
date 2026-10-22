#!/usr/bin/env python3
"""brieflow_monitor.py — adaptive liveness + resource monitor.

The survivability *signal/policy* layer of the run loop, extracted from
brieflow_runloop (move-not-rewrite). Everything here is **notify-free**: it
samples signals and decides cadence, and the caller (`brieflow_runloop.
start_monitor`, which stays in runloop) turns those signals into notifications
and subprocess actions. That split keeps this layer generic — only the notify +
launch glue is brieflow-specific.

Replaces a fixed heartbeat with a sampler that:
  - Computes three liveness signals per cycle (proc state, log-mtime delta, CPU%)
    by inspecting the current subprocess tree via psutil.
  - Adapts cadence: burst (5s) at phase / attempt boundary; steady (60s)
    mid-phase; idle (5min) after >=2 consecutive quiet cycles.
  - Detects a hang ONLY when all signals stay idle for >=5 min (caller warns,
    never kills), and watches for an OOM cascade and disk exhaustion.
"""

from __future__ import annotations

import errno
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from brieflow_backend import BackendAdapter, SlurmBackend


def _pid_alive(pid: int) -> bool:
    """Return True iff a process with this PID is alive on this host.

    Uses kill(0) — does not actually signal the process, just probes for
    existence. ESRCH = no such process; EPERM = exists but owned by another
    user (still counts as alive — we can't reclaim its lock).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM
    return True


def latest_snakemake_log(analysis_dir: Path) -> Path | None:
    logs = sorted(
        (analysis_dir / ".snakemake" / "log").glob("*.snakemake.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return logs[0] if logs else None


class MonitorCtx:
    """Shared state between the main runloop and the monitor thread.

    The monitor thread reads `current_pid` (the live brieflow_run.py / agg /
    recover subprocess pid) and the transition flags to decide cadence.
    """

    BURST_SEC = 5
    STEADY_SEC = 60
    IDLE_SEC = 300
    HANG_THRESHOLD_SEC = 300  # all signals idle ≥5 min → hang notify
    HANG_REPEAT_SEC = 300  # don't repeat the hang notify more often than this
    BURST_GRACE_SEC = 30  # treat first 30s after a transition as burst

    def __init__(self) -> None:
        # Subprocess being watched (set by run_capture_json while alive).
        self.current_pid: int | None = None
        self.current_label: str | None = None
        # Transition timestamps — burst when recent.
        self.phase_started_ts: float = 0.0
        self.attempt_started_ts: float = 0.0
        # Liveness rollup.
        self.last_active_ts: float = time.time()
        self.last_hang_notify_ts: float = 0.0
        # Cached log-mtime snapshot for "did the log grow" check.
        self.last_log_size: int = -1
        # Counts how many consecutive cycles showed no signal activity —
        # gates the steady→idle transition (HARDENING_TODO #4).
        self.quiet_cycles: int = 0
        # Last observed plate_filter (brieflow shards phases per plate). A
        # change emits a one-time "plate rollover" notice so the tree-size
        # jump reads as progress, not a regression.
        self.last_plate: str | None = None
        # Highest 10%-bucket of the snakemake DAG already pinged for the
        # current plate (0..10). Reset on each plate rollover.
        self.last_progress_bucket: int = 0
        # HARDENING #1: OOM-cascade watcher. When any rule's OOM count in
        # the current attempt window exceeds threshold, the monitor flips
        # this flag and sigterms the subprocess. run_one_phase then routes
        # to recover.py.
        self.oom_cascade_detected: bool = False
        self.oom_cascade_rule: str | None = None
        self.oom_cascade_count: int = 0
        self.oom_cascade_threshold: int = 100
        self.last_oom_poll_ts: float = 0.0
        self.oom_poll_interval_sec: float = 60.0
        self.user: str = os.environ.get("USER", "")
        # Backend adapter — drives queue introspection and gates cascade
        # detection. Defaults to SlurmBackend so existing tests that
        # construct a bare MonitorCtx() keep working unchanged.
        self.adapter: BackendAdapter = SlurmBackend()
        # Phase 1.2: disk-space monitoring. Bounded VM disk makes this a
        # correctness item, not polish. Warn at warn_pct, abort at abort_pct
        # (terminates subprocess + sets abort_signaled so outer loop escalates).
        self.last_disk_check_ts: float = 0.0
        self.last_disk_warn_ts: float = 0.0
        self.disk_check_interval_sec: float = 60.0
        self.disk_warn_pct: float = 0.99
        self.disk_abort_pct: float = 0.995
        # Absolute-free floor: abort if free < this many GB regardless of %. A flat
        # % is the wrong shape across disk sizes — 0.995 on a small disk still lets it
        # ENOSPC before the poll fires; this catches the near-zero case. OUTSTANDING #16.
        self.disk_abort_min_free_gb: float = 10.0
        self.disk_warn_repeat_sec: float = 600.0  # re-warn every 10min, not every cycle
        self.disk_abort_signaled: bool = False
        self.disk_abort_used_pct: float = 0.0


# Wall-time of the prime→read gap used to measure CPU busy-ness. psutil's
# cpu_percent(interval=None) returns 0.0 on the FIRST call for a given Process
# object; we re-enumerate the tree every cycle, so without a primed gap a busy
# worker reads as 0%. 0.3s once per monitor cycle (60s steady / 300s idle) is
# negligible and makes cpu_busy the robust liveness signal it was meant to be.
_CPU_SAMPLE_SEC = 0.3


def _detect_plate_filter(tree) -> str | None:
    """Best-effort read of plate_filter=N from a snakemake cmdline in the tree.

    brieflow's flow runs a phase once per plate (plate_filter=1, then 2, ...)
    inside a single brieflow_run.py subprocess. Surfacing the plate lets the
    monitor show a normal plate rollover as `plate=2` instead of an unexplained
    tree-size jump (combine tail collapses to a handful of jobs, then the next
    plate's full per-tile DAG re-expands). Returns None when no plate-scoped
    snakemake is running (phases that don't shard by plate).
    """
    for p in tree:
        try:
            cl = p.cmdline()
        except Exception:
            # psutil is imported locally in _process_signals (the only caller);
            # catch broadly here so a dead/zombie proc or missing psutil can't
            # raise NameError and break the whole monitor snapshot.
            continue
        for tok in cl:
            if tok.startswith("plate_filter="):
                return tok.split("=", 1)[1]
    return None


_PROGRESS_RE = re.compile(rb"(\d+) of (\d+) steps \((\d+)%\) done")


def _snakemake_progress(log_path) -> tuple[int, int, int] | None:
    """Parse the latest `X of Y steps (Z%) done` line from a snakemake log.

    Returns (done, total, pct) or None. Reads only the tail (64 KiB) so it
    stays cheap even on a multi-MB log. brieflow's per-plate sharding means
    each plate has its own log with its own counter, so this is naturally the
    *current plate's* progress.
    """
    if log_path is None:
        return None
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            chunk = f.read()
    except OSError:
        return None
    matches = _PROGRESS_RE.findall(chunk)
    if not matches:
        return None
    done, total, pct = matches[-1]
    return int(done), int(total), int(pct)


def _process_signals(pid: int | None) -> dict:
    """Snapshot liveness signals for the current subprocess.

    Returns a dict with keys:
      pid_alive (bool), proc_running (bool — at least one descendant in R),
      cpu_busy (bool — >5% CPU sampled over _CPU_SAMPLE_SEC), tree_size (int),
      plate (str|None — plate_filter of the live snakemake, if plate-sharded).
    Returns all-False when pid is None or the process has exited.
    """
    out = {
        "pid_alive": False,
        "proc_running": False,
        "cpu_busy": False,
        "tree_size": 0,
        "plate": None,
    }
    if pid is None:
        return out
    try:
        import psutil
    except ImportError:  # pragma: no cover
        # No psutil — fall back to /proc-based pid_alive only.
        out["pid_alive"] = _pid_alive(pid)
        return out
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return out
    out["pid_alive"] = True
    try:
        tree = [root] + root.children(recursive=True)
    except psutil.Error:
        tree = [root]
    out["tree_size"] = len(tree)
    out["plate"] = _detect_plate_filter(tree)
    running = 0
    # Two-pass CPU sample: prime each (freshly-enumerated) process so its
    # cpu_percent baseline is set, sleep briefly, then read the real delta.
    # A single pass would read 0.0 for every worker and make a CPU-bound serial
    # job (e.g. the per-plate combine tail) look idle → false hang-page.
    for p in tree:
        try:
            p.cpu_percent(interval=None)
        except psutil.Error:
            continue
    time.sleep(_CPU_SAMPLE_SEC)
    cpu_total = 0.0
    for p in tree:
        try:
            if p.status() in (psutil.STATUS_RUNNING, psutil.STATUS_DISK_SLEEP):
                running += 1
            cpu_total += p.cpu_percent(interval=None)
        except psutil.Error:
            continue
    out["proc_running"] = running > 0
    out["cpu_busy"] = cpu_total > 5.0
    return out


def _oom_count_since(start_ts: float, user: str) -> dict[str, int]:
    """Count OUT_OF_MEMORY jobs per rule since the given UNIX timestamp.

    Uses sacct's `Comment%80` field which brieflow's executor populates
    with `rule_<rule>_wildcards_...`. Returns {rule: count}.

    Falls back silently to an empty dict on any sacct issue — the cascade
    watcher only ABORTS on cascades, so a missed sample is harmless.
    """
    if not user:
        return {}
    start_iso = datetime.fromtimestamp(start_ts).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        proc = subprocess.run(
            [
                "sacct",
                "-u",
                user,
                "--starttime",
                start_iso,
                "--format=Comment%80,State",
                "--parsable2",
                "--noheader",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    counts: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        parts = line.split("|")
        if len(parts) < 2:
            continue
        comment, state = parts[0], parts[1]
        if "OUT_OF_MEMORY" not in state:
            continue
        if not comment.startswith("rule_"):
            continue
        rule = comment[len("rule_") :].split("_wildcards")[0]
        if not rule:
            continue
        counts[rule] = counts.get(rule, 0) + 1
    return counts


def _check_oom_cascade(ctx: MonitorCtx, now: float) -> None:
    """If due, poll the backend and set ctx.oom_cascade_detected when a rule
    exceeds the threshold. HARDENING_TODO #1.

    No-op on backends where cascade detection is meaningless (e.g. LocalBackend's
    sequential execution can't cascade the same way)."""
    if not ctx.adapter.supports_cascade_detection():
        return
    if ctx.attempt_started_ts <= 0:
        return
    if now - ctx.last_oom_poll_ts < ctx.oom_poll_interval_sec:
        return
    ctx.last_oom_poll_ts = now
    counts = _oom_count_since(ctx.attempt_started_ts, ctx.user)
    for rule, n in counts.items():
        if n >= ctx.oom_cascade_threshold:
            ctx.oom_cascade_detected = True
            ctx.oom_cascade_rule = rule
            ctx.oom_cascade_count = n
            return


def _terminate_subprocess_tree(pid: int) -> None:
    """SIGTERM the process and all descendants, then SIGKILL after a brief grace."""
    try:
        import psutil
    except ImportError:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
        return
    try:
        root = psutil.Process(pid)
        procs = [root] + root.children(recursive=True)
    except psutil.Error:
        return
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            continue
    psutil.wait_procs(procs, timeout=5)
    for p in procs:
        if p.is_running():
            try:
                p.kill()
            except psutil.Error:
                continue


def _check_disk_space(
    analysis_dir: Path, ctx: MonitorCtx, now: float
) -> tuple[bool, float]:
    """Check disk usage at the analysis-dir filesystem; warn/abort by threshold.

    Returns (abort_now, used_pct). When abort_now=True, the caller should
    terminate the subprocess + flag the outer loop for escalation. Phase 1.2.

    Throttle: disk check only runs every `disk_check_interval_sec` (default 60s).
    Warn notify throttled to `disk_warn_repeat_sec` (default 10min).
    """
    if now - ctx.last_disk_check_ts < ctx.disk_check_interval_sec:
        return False, 0.0
    ctx.last_disk_check_ts = now
    try:
        import shutil

        usage = shutil.disk_usage(analysis_dir)
        used_pct = usage.used / usage.total if usage.total else 0.0
        free_gb = usage.free / (1024 ** 3)
    except OSError:
        return False, 0.0

    low_abs = (ctx.disk_abort_min_free_gb is not None
               and free_gb < ctx.disk_abort_min_free_gb)
    if used_pct >= ctx.disk_abort_pct or low_abs:
        ctx.disk_abort_signaled = True
        ctx.disk_abort_used_pct = used_pct
        return True, used_pct
    if used_pct >= ctx.disk_warn_pct:
        if now - ctx.last_disk_warn_ts >= ctx.disk_warn_repeat_sec:
            ctx.last_disk_warn_ts = now
            return False, used_pct
    return False, 0.0


def _log_grew(analysis_dir: Path, ctx: MonitorCtx) -> bool:
    """Did the latest snakemake log file grow since the last cycle?

    Mutates ctx.last_log_size as a side-effect so the next cycle's delta is
    computed correctly.
    """
    log_p = latest_snakemake_log(analysis_dir)
    if log_p is None:
        return False
    try:
        size = log_p.stat().st_size
    except OSError:
        return False
    grew = ctx.last_log_size >= 0 and size > ctx.last_log_size
    ctx.last_log_size = size
    return grew


def monitor_snapshot(analysis_dir: Path, ctx: MonitorCtx) -> tuple[str, dict]:
    """Produce one monitor line + the underlying signals dict."""
    sig = _process_signals(ctx.current_pid)
    sig["log_grew"] = _log_grew(analysis_dir, ctx)

    parts: list[str] = []
    if ctx.current_label:
        parts.append(f"sub={ctx.current_label}(pid={ctx.current_pid or '-'})")
    parts.append(
        "live="
        f"{'p' if sig['pid_alive'] else '-'}"
        f"{'r' if sig['proc_running'] else '-'}"
        f"{'c' if sig['cpu_busy'] else '-'}"
        f"{'l' if sig['log_grew'] else '-'}"
        f" tree={sig['tree_size']}"
        + (f" plate={sig['plate']}" if sig.get("plate") else "")
    )

    # Backend queue + latest log tail for the operator's eyeball check.
    # SlurmBackend hits squeue; LocalBackend returns {} (no queue concept).
    try:
        states = ctx.adapter.queue_summary()
        parts.append(
            "queue=" + ",".join(f"{k}:{v}" for k, v in states.items())
            if states
            else "queue=empty"
        )
    except Exception as e:  # pragma: no cover
        parts.append(f"queue=err({e})")

    log_p = latest_snakemake_log(analysis_dir)
    if log_p is not None:
        try:
            tail = subprocess.run(
                ["tail", "-n", "1", str(log_p)],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
            if tail:
                parts.append(f"last={tail[:140]}")
        except Exception:
            pass

    return " | ".join(parts), sig


def decide_interval(ctx: MonitorCtx, sig: dict, now: float) -> int:
    """Pick the next monitor sleep interval given signals + recent transitions.

    Burst at phase/attempt boundaries (first BURST_GRACE_SEC after); steady
    when there's recent activity; idle after ≥2 consecutive quiet cycles.
    """
    in_burst_window = (
        ctx.phase_started_ts and now - ctx.phase_started_ts < ctx.BURST_GRACE_SEC
    ) or (ctx.attempt_started_ts and now - ctx.attempt_started_ts < ctx.BURST_GRACE_SEC)
    any_signal = sig.get("pid_alive") and (
        sig.get("proc_running") or sig.get("cpu_busy") or sig.get("log_grew")
    )
    if in_burst_window:
        return ctx.BURST_SEC
    if any_signal:
        return ctx.STEADY_SEC
    if ctx.quiet_cycles >= 2:
        return ctx.IDLE_SEC
    return ctx.STEADY_SEC

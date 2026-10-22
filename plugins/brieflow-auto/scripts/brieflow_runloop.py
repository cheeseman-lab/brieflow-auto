#!/usr/bin/env python3
"""
brieflow_runloop.py — Supervisor loop for brieflow pipeline runs.

Codifies in Python what /brieflow-module's markdown previously described
as steps for Claude to follow manually. The benefit: failures surface
without requiring an agent to poll. Run this in a tmux session and
get notify() pings on:

  - module transitions (e.g. preprocess → sbs marker observed)
  - recovery actions (which rule was bumped, old/new mem)
  - escalations (with rule, slurm log, evidence pointer)
  - completion (wall time, success/failure)
  - liveness heartbeats (adaptive cadence from brieflow_monitor, current squeue + last log line)

Pipeline:

    brieflow_run.py (blocking subprocess) → JSON summary
        success=true  → run aggregate_efficiency, notify, exit 0
        success=false → brieflow_recover.py → JSON action
            action=resume   → relaunch with --resume, attempt+=1
            action=escalate → notify reason+evidence, exit 1
            (cap at --max-attempts)

Notification channel is brieflow_notify.sh, which the operator can
configure (slack/email/log file). On a fresh install it just appends
to <analysis-dir>/.brieflow_runloop/notify.log.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
RUN_PY = SCRIPT_DIR / "brieflow_run.py"
RECOVER_PY = SCRIPT_DIR / "brieflow_recover.py"
AGG_EFF_PY = SCRIPT_DIR / "brieflow_aggregate_efficiency.py"
WATCHDOG_PY = SCRIPT_DIR / "brieflow_driver_watchdog.py"
SMOKETEST_PY = SCRIPT_DIR / "brieflow_smoketest.py"
DRYRUN_PREVIEW_PY = SCRIPT_DIR / "brieflow_dryrun_preview.py"
NOTIFY_SH = SCRIPT_DIR / "brieflow_notify.sh"

# Shared diagnostic-bundle helpers (HARDENING #14).
sys.path.insert(0, str(SCRIPT_DIR))
from brieflow_diag import write_diag_bundle  # noqa: E402
from brieflow_phases import NOTEBOOK_ONLY_PHASES  # noqa: E402
from brieflow_backend import get_backend  # noqa: E402
from brieflow_hooks import RunLifecycleHooks, get_hooks  # noqa: E402
from brieflow_monitor import (  # noqa: E402,F401  (re-export: the signal/policy layer)
    MonitorCtx,
    latest_snakemake_log,
    _pid_alive,
    _detect_plate_filter,
    _snakemake_progress,
    _process_signals,
    _oom_count_since,
    _check_oom_cascade,
    _terminate_subprocess_tree,
    _check_disk_space,
    _log_grew,
    monitor_snapshot,
    decide_interval,
)
try:
    import brieflow_qc as _qc  # noqa: E402  (SCRIPT_DIR already on sys.path)
except Exception:  # pragma: no cover - QC is best-effort
    _qc = None


# ---------------------------------------------------------------------------
# Notification + state directory
# ---------------------------------------------------------------------------


def state_dir(analysis_dir: Path, tag: str) -> Path:
    d = analysis_dir / ".brieflow_runloop" / tag
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Single-instance lock (HARDENING_TODO #17)
#
# One lock per analysis dir, NOT per tag — the goal is preventing two runloop
# processes from racing on the same workdir (state files, snakemake locks,
# output rewrites), regardless of the tag they're using.
# ---------------------------------------------------------------------------


def acquire_instance_lock(analysis_dir: Path, tag: str) -> Path:
    """Atomically claim the per-analysis-dir runloop lock, or die.

    Writes pid+host+tag+started JSON into
    `<analysis-dir>/.brieflow_runloop/instance.lock`. If the file already
    exists and names a live PID on this host, exits with a clear error.
    If the existing lock names a dead PID (or one on a different host that
    can't be probed), warns and reclaims it.

    Registers an atexit handler to delete the lock on graceful exit.
    """
    lock_root = analysis_dir / ".brieflow_runloop"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / "instance.lock"

    payload = json.dumps(
        {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "tag": tag,
            "started": datetime.now().isoformat(timespec="seconds"),
        }
    ).encode()

    def _write(fd: int) -> None:
        os.write(fd, payload)
        os.close(fd)

    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        _write(fd)
    except FileExistsError:
        # Inspect the existing lock and decide whether to reclaim.
        try:
            with open(lock_path) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing = {}
        ex_pid = int(existing.get("pid") or 0)
        ex_host = existing.get("host") or "?"
        same_host = ex_host == socket.gethostname()
        if same_host and _pid_alive(ex_pid):
            print(
                f"ERROR: another brieflow_runloop is running against "
                f"{analysis_dir}: pid={ex_pid} host={ex_host} "
                f"tag={existing.get('tag')} started={existing.get('started')}\n"
                f"  Lock file: {lock_path}\n"
                f"  If you're sure no runloop is alive, delete the lock "
                f"file and retry.",
                file=sys.stderr,
            )
            sys.exit(1)
        # Stale lock — reclaim. Either a dead PID on this host, or a
        # foreign-host lock we can't probe (best-effort assumption: the
        # previous host died; the operator can override by manually
        # removing the lock if they know better).
        reason = (
            f"dead pid {ex_pid}"
            if same_host
            else f"foreign host {ex_host} (cannot probe)"
        )
        print(
            f"WARN: stale brieflow_runloop lock at {lock_path} "
            f"({reason}, tag={existing.get('tag')}, "
            f"started={existing.get('started')}); reclaiming.",
            file=sys.stderr,
        )
        lock_path.unlink()
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        _write(fd)

    def _release() -> None:
        try:
            with open(lock_path) as f:
                cur = json.load(f)
            if int(cur.get("pid") or 0) == os.getpid():
                lock_path.unlink(missing_ok=True)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

    atexit.register(_release)
    return lock_path


_SCREEN_NAME_CACHE: dict[str, str] = {}


def _screen_name(analysis_dir) -> str:
    """Derive the screen name for notifications (e.g. 'damavand').

    Uses the canonical convention (CLAUDE.md): the analysis repo dir name with
    the '-analysis' suffix stripped. analysis_dir is typically '<screen>-analysis/
    analysis', so we climb past a bare 'analysis' leaf. Cached per dir.
    """
    key = str(analysis_dir)
    cached = _SCREEN_NAME_CACHE.get(key)
    if cached is not None:
        return cached
    base = Path(analysis_dir).name
    if base in ("analysis", ""):
        base = Path(analysis_dir).parent.name
    name = base[: -len("-analysis")] if base.endswith("-analysis") else base
    _SCREEN_NAME_CACHE[key] = name
    return name


def notify(
    analysis_dir: Path,
    tag: str,
    message: str,
    priority: str = "info",
) -> None:
    """Send `message` through the configured notification channel.

    Always appends to <state>/notify.log AND prints to stdout. If
    brieflow_notify.sh exists and is executable, calls it with
    (tag, message, priority) so it can fan-out to email/slack per
    HARDENING #12 priority tiers.

    Priority must be one of {info, progress, warning, action_needed}. info is
    log-only. progress pushes immediately at low urgency (milestone/heartbeat
    pings; deduped by the caller). warning is throttled to 1 push per 15min per
    tag. action_needed pushes immediately.
    """
    if priority not in ("info", "progress", "warning", "action_needed"):
        priority = "info"
    screen = _screen_name(analysis_dir)
    sd = state_dir(analysis_dir, tag)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] [{priority}] [{screen}] {message}"
    print(line, flush=True)
    with open(sd / "notify.log", "a") as f:
        f.write(line + "\n")
    if NOTIFY_SH.exists() and os.access(NOTIFY_SH, os.X_OK):
        try:
            subprocess.run(
                [str(NOTIFY_SH), tag, message, priority],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                env={**os.environ, "BRIEFLOW_SCREEN": screen},
            )
        except subprocess.TimeoutExpired:
            pass


def emit_phase_qc(analysis_dir, phase: str):
    """Build + persist + print the post-phase QC summary for `phase`.

    Best-effort: any failure here must NOT fail a phase that already completed.
    Resolves brieflow_output under the analysis dir. Returns the QCSummary or
    None. Stamps datetime.now(timezone.utc) and passes it to the clock-free
    build_phase_qc.
    """
    if _qc is None:
        return None
    try:
        analysis_dir = Path(analysis_dir)
        brieflow_output = analysis_dir / "brieflow_output"
        if not (brieflow_output / phase).exists():
            return None
        ts = datetime.now(timezone.utc).isoformat()
        summary = _qc.build_phase_qc(brieflow_output, phase, generated_at=ts)
        print(_qc.render_qc_text(summary))
        _qc.write_qc_report_html(
            summary, brieflow_output / phase / "eval" / "qc_summary.html"
        )
        return summary
    except Exception as e:
        print(f"[qc] post-phase QC skipped for {phase}: {e}")
        return None


LAST_ESCALATION_NAME = "last_escalation.json"


def write_last_escalation(analysis_dir, tag, attempt, modules, action, bundle) -> Path:
    """Persist the recover action + bundle path so the runner can draft an issue.

    Best-effort: a write failure here must NOT raise into the escalate branch
    that already handled the real failure. Logs via notify() and returns the
    (possibly unwritten) target path either way.
    """
    p = Path(analysis_dir) / ".brieflow" / LAST_ESCALATION_NAME
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {
                    "schema": "brieflow-last-escalation/v1",
                    "tag": tag,
                    "attempt": attempt,
                    "phase": ",".join(modules),
                    "action": action,
                    "diag_bundle": str(bundle),
                    "written_at": datetime.now().isoformat(),
                },
                indent=2,
                default=str,
            )
        )
    except OSError as e:
        notify(analysis_dir, tag, f"failed to write {LAST_ESCALATION_NAME}: {e}")
    return p


# ---------------------------------------------------------------------------
# Monitor thread — drives brieflow_monitor's signal/policy layer and turns each
# snapshot into notify() pings plus OOM-cascade / disk / hang actions. This is
# the brieflow-specific glue that stays; the signals themselves live in
# brieflow_monitor.
# ---------------------------------------------------------------------------


def start_monitor(
    analysis_dir: Path,
    tag: str,
    ctx: MonitorCtx,
    stop_event: threading.Event,
) -> threading.Thread:
    """Spawn the adaptive monitor daemon. The thread runs until stop_event."""

    def loop():
        # Prime psutil's per-process cpu_percent baseline (first call returns 0).
        _process_signals(ctx.current_pid)
        next_interval = ctx.BURST_SEC
        while not stop_event.wait(next_interval):
            try:
                now = time.time()
                snap_line, sig = monitor_snapshot(analysis_dir, ctx)
                any_signal = sig.get("pid_alive") and (
                    sig.get("proc_running")
                    or sig.get("cpu_busy")
                    or sig.get("log_grew")
                )
                if any_signal:
                    ctx.last_active_ts = now
                    ctx.quiet_cycles = 0
                else:
                    ctx.quiet_cycles += 1

                # Self-reporting progress (reduces operator check-in burden):
                # a Slack-reaching ping on each plate rollover and on each 10%
                # of the snakemake DAG. brieflow shards a phase per plate, so
                # the % is per-plate and the counter restarts each rollover.
                plate = sig.get("plate")
                if plate != ctx.last_plate:
                    if ctx.last_plate is not None and plate is not None:
                        notify(
                            analysis_dir,
                            tag,
                            f"{ctx.current_label or 'run'}: advanced to plate "
                            f"{plate} (was {ctx.last_plate}) — the DAG re-expands "
                            f"for the new plate; this is normal progress, not a "
                            f"restart.",
                            priority="progress",
                        )
                        ctx.last_active_ts = now
                        ctx.quiet_cycles = 0
                    ctx.last_plate = plate
                    ctx.last_progress_bucket = 0
                prog = _snakemake_progress(latest_snakemake_log(analysis_dir))
                if prog is not None:
                    done, total, pct = prog
                    bucket = pct // 10
                    if bucket > ctx.last_progress_bucket:
                        ctx.last_progress_bucket = bucket
                        plate_str = f"plate {plate} " if plate else ""
                        notify(
                            analysis_dir,
                            tag,
                            f"{ctx.current_label or 'run'}: {plate_str}{pct}% "
                            f"({done}/{total} steps done)",
                            priority="progress",
                        )

                # HARDENING #1: poll sacct for an OOM cascade on the
                # current attempt's rules. ONLY abort on cascade detection
                # (a concrete signal — multiple OOMs of the same rule),
                # never on suspected hang.
                if sig.get("pid_alive") and not ctx.oom_cascade_detected:
                    _check_oom_cascade(ctx, now)
                    if ctx.oom_cascade_detected:
                        notify(
                            analysis_dir,
                            tag,
                            f"OOM CASCADE detected: rule={ctx.oom_cascade_rule} "
                            f"with {ctx.oom_cascade_count} OUT_OF_MEMORY jobs "
                            f"in this attempt (threshold "
                            f"{ctx.oom_cascade_threshold}). Terminating "
                            f"subprocess pid={ctx.current_pid} so recover.py "
                            f"can bump the mem cap.",
                            priority="warning",
                        )
                        if ctx.current_pid:
                            _terminate_subprocess_tree(ctx.current_pid)

                # Phase 1.2: disk-space monitoring. Bounded VM disk makes
                # this a correctness item. Warn at 75%, abort at 90%.
                if sig.get("pid_alive") and not ctx.disk_abort_signaled:
                    abort_now, used_pct = _check_disk_space(analysis_dir, ctx, now)
                    if abort_now:
                        notify(
                            analysis_dir,
                            tag,
                            f"DISK ABORT: {used_pct:.1%} used on {analysis_dir} "
                            f"(threshold {ctx.disk_abort_pct:.0%}). Terminating "
                            f"subprocess pid={ctx.current_pid}; run cannot "
                            f"proceed without disk. Free space and relaunch.",
                            priority="action_needed",
                        )
                        if ctx.current_pid:
                            _terminate_subprocess_tree(ctx.current_pid)
                    elif used_pct >= ctx.disk_warn_pct:
                        notify(
                            analysis_dir,
                            tag,
                            f"DISK WARN: {used_pct:.1%} used on {analysis_dir} "
                            f"(warn threshold {ctx.disk_warn_pct:.0%}, abort "
                            f"{ctx.disk_abort_pct:.0%}). Free space soon.",
                            priority="warning",
                        )

                # HARDENING #8: only declare hang when ALL signals quiet
                # for ≥HANG_THRESHOLD_SEC. Notify, never kill.
                if (
                    sig.get("pid_alive")
                    and now - ctx.last_active_ts >= ctx.HANG_THRESHOLD_SEC
                    and now - ctx.last_hang_notify_ts >= ctx.HANG_REPEAT_SEC
                ):
                    quiet_s = int(now - ctx.last_active_ts)
                    notify(
                        analysis_dir,
                        tag,
                        f"WARN: liveness signals idle for {quiet_s}s "
                        f"(pid={ctx.current_pid}, tree={sig['tree_size']}) — "
                        f"investigating non-destructively; not killing. "
                        f"Last snapshot: {snap_line}",
                        priority="warning",
                    )
                    ctx.last_hang_notify_ts = now

                notify(analysis_dir, tag, f"monitor: {snap_line}")
                next_interval = decide_interval(ctx, sig, now)
            except Exception as e:  # pragma: no cover
                notify(analysis_dir, tag, f"monitor error: {e}")
                next_interval = ctx.STEADY_SEC

    t = threading.Thread(target=loop, daemon=True, name="runloop-monitor")
    t.start()
    return t


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def run_capture_json(
    cmd: list[str],
    state: Path,
    label: str,
    ctx: MonitorCtx | None = None,
) -> dict | None:
    """Run cmd, stream stderr to console + log, return parsed JSON from stdout.

    Uses Popen so the monitor thread can see the live PID via `ctx` while
    the subprocess runs. The ctx fields are reset on completion.
    """
    log_path = state / f"{label}.log"
    json_path = state / f"{label}.json"
    print(f"[runloop] $ {' '.join(shlex.quote(c) for c in cmd)}", flush=True)
    with open(log_path, "w") as logf:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=logf,
            text=True,
        )
        if ctx is not None:
            ctx.current_pid = proc.pid
            ctx.current_label = label
        try:
            stdout, _ = proc.communicate()
        finally:
            if ctx is not None:
                ctx.current_pid = None
                ctx.current_label = None
    stdout = stdout or ""
    json_path.write_text(stdout)
    if not stdout.strip():
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        # Fall back to last JSON object in output (some scripts print
        # progress before the JSON summary).
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        return None


def forcerun_for_modules(analysis_dir: Path, modules: list[str], rules: list[str]) -> list[str]:
    """Keep the --forcerun rules defined in these modules' .smk files; all of them if none is found."""
    defined: set[str] = set()
    for root in (analysis_dir.parent / "brieflow", analysis_dir / "brieflow"):
        for module in modules:
            smk = root / "workflow" / "rules" / f"{module}.smk"
            if smk.is_file():
                defined.update(re.findall(r"^rule (\w+)\s*:", smk.read_text(), re.MULTILINE))
        if defined:
            return [r for r in rules if r in defined]
    return list(rules)


def build_run_cmd(
    args: argparse.Namespace,
    modules: list[str],
    tag: str,
    notes: str,
    attempt: int,
) -> list[str]:
    """Build the brieflow_run.py invocation for one attempt of one phase.

    brieflow_run.py >= 2026-05-07 always keeps existing outputs; wipes are
    opt-in via `--clean-all` / `--clean-modules`. The runloop never wipes —
    each phase is expected to build incrementally on prior phases' outputs.
    Operators who want a cold restart should invoke brieflow_run.py directly
    with `--clean-all`.
    """
    cmd = [
        sys.executable,
        str(RUN_PY),
        "--analysis-dir",
        str(args.analysis_dir),
        "--modules",
        *modules,
        "--config",
        args.config,
        "--backend",
        args.backend,
        "--jobs",
        str(args.jobs),
        "--array-limit",
        str(args.array_limit),
        "--latency-wait",
        str(args.latency_wait),
        "--cpus-per-task",
        str(args.cpus_per_task),
        "--tag",
        tag,
    ]
    # Forward the operator's --cores so the local backend's concurrency (and thus
    # peak RAM) is bounded as requested; brieflow_run.py passes it through to flow.sh.
    # Without this, every phase ran at flow.sh's default `--cores all`.
    if getattr(args, "cores", None):
        cmd.extend(["--cores", str(args.cores)])
    if args.use_arrays:
        cmd.append("--use-arrays")
    if args.mem_recs:
        cmd.extend(["--mem-recs", str(args.mem_recs)])
    if notes:
        cmd.extend(["--notes", notes])
    forcerun = forcerun_for_modules(Path(args.analysis_dir), modules, args.forcerun or [])
    if forcerun:
        cmd.extend(["--forcerun", *forcerun])
    # Branch-and-converge: restrict this run to a single plate (runner SBS
    # per-plate pass). Off by default — full all-plates run when unset.
    if getattr(args, "only_plate", None) is not None:
        cmd.extend(["--only-plate", str(args.only_plate)])
    return cmd


def build_recover_cmd(
    args: argparse.Namespace,
    modules: list[str],
    attempt: int,
    oom_rule: str | None = None,
    since: float | None = None,
) -> list[str]:
    cmd = [
        sys.executable,
        str(RECOVER_PY),
        "--analysis-dir",
        str(args.analysis_dir),
        "--modules",
        *modules,
        "--attempt",
        str(attempt),
        "--max-attempts",
        str(args.max_attempts),
    ]
    if args.mem_recs:
        cmd.extend(["--mem-recs", str(args.mem_recs)])
    cmd.extend(["--backend", args.backend])
    if since is not None:
        cmd.extend(["--since", str(since)])
    if oom_rule:
        # cascade-terminated run: no `Error in rule` block exists yet, so hand
        # recover.py the rule the monitor caught OUT_OF_MEMORY
        cmd.extend(["--oom-rule", oom_rule])
    return cmd


def parse_phases(spec: str) -> list[list[str]]:
    """Parse a --phases spec string into a list of module lists.

    Format: phases separated by ';', modules within a phase separated by ','.
    Whitespace is ignored. Empty phases are dropped.

    Examples:
        Single phase (the operator-driven default — used by brieflow-module skill):
            "sbs"  →  [["sbs"]]

        Multi-phase, one bucket per phase (autonomous re-run where every
        phase's notebook is already configured and the operator doesn't
        need to insert configure-this-phase steps between runs):
            "preprocess;sbs;phenotype;merge;classify;aggregate;cluster" →
            [["preprocess"], ["sbs"], ["phenotype"], ["merge"],
             ["classify"], ["aggregate"], ["cluster"]]

        Bundled bucket (`,`-joined inside a `;`-bucket) is supported by the
        parser but only correct when the bundled phases have no
        notebook-configuration dependency on each other's outputs — which
        is not the case for any of brieflow's downstream phases. Prefer
        the per-phase bucket form.
    """
    phases: list[list[str]] = []
    for raw in spec.split(";"):
        mods = [m.strip() for m in raw.split(",") if m.strip()]
        if mods:
            phases.append(mods)
    if not phases:
        raise ValueError(f"--phases parsed to no modules: {spec!r}")
    return phases


def phase_tag(base_tag: str, idx: int, modules: list[str]) -> str:
    """Construct a per-phase tag from the base tag, phase index, and module list."""
    summary = (
        "_".join(modules) if len(modules) <= 3 else f"{modules[0]}_etc{len(modules)}"
    )
    return f"{base_tag}__p{idx}_{summary}"


# ---------------------------------------------------------------------------
# Dry-run preview gate (HARDENING_TODO #6 + #7 layer 2)
#
# Calls brieflow_dryrun_preview.py and decides whether the runloop should
# proceed. Returns (ok_to_proceed, classification, detail).
# ---------------------------------------------------------------------------


def dryrun_preview(
    args: argparse.Namespace,
    terminal_module: str,
    require_rerun: list[str] | None = None,
) -> tuple[bool, str, str]:
    cmd = [
        sys.executable,
        str(DRYRUN_PREVIEW_PY),
        "--analysis-dir",
        str(args.analysis_dir),
        "--config",
        args.config,
        "--terminal-module",
        terminal_module,
        "--json",
    ]
    if require_rerun:
        cmd.extend(["--require-rerun", *require_rerun])
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=400, check=False
        )
    except subprocess.TimeoutExpired:
        return False, "dryrun_timeout", "dryrun_preview timed out"
    if not proc.stdout.strip():
        return (
            False,
            "dryrun_no_output",
            (f"dryrun_preview emitted no JSON; rc={proc.returncode}"),
        )
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return False, "dryrun_parse_error", proc.stdout[:300]
    classification = result.get("classification", "unknown")
    detail = result.get("detail", "")
    # The standalone script's exit code already encodes abort_on; mirror
    # that contract here.
    ok = proc.returncode == 0
    return ok, classification, detail


# ---------------------------------------------------------------------------
# Same-error short-circuit (HARDENING_TODO #15)
#
# When recover.py's classification + bump can't actually fix the failure
# (e.g. a Python ValueError mis-classified as OOM in pre-#13 builds, or a
# legitimate code bug whose mem bump is irrelevant), the recovery loop will
# keep retrying the same failure up to --max-attempts. Caught 2026-05-13
# A reference run: eval_merge failed attempts 2 and 3 with identical ValueError;
# bump cycle wasted ~30 min before escalating.
#
# The fix: track per-rule error fingerprints across attempts. Two consecutive
# attempts with the same fingerprint = the bump doesn't help, escalate now.
# ---------------------------------------------------------------------------


def _diagnostics_fingerprint(action: dict) -> dict[str, str]:
    """Return {rule -> fingerprint_hash} for an action's diagnostics list.

    Fingerprint = sha1 of (rule, category, evidence_prefix). Evidence is
    truncated to 120 chars to avoid spurious differences on changing
    numerical details (different MaxRSS readings on each attempt).
    """
    import hashlib

    out: dict[str, str] = {}
    for d in action.get("diagnostics") or []:
        rule = d.get("rule") or ""
        category = d.get("category") or ""
        evidence = (d.get("evidence") or "")[:120]
        key = f"{rule}|{category}|{evidence}".encode()
        out[rule] = hashlib.sha1(key).hexdigest()[:16]
    return out


def _detect_same_fingerprint(
    prev: dict[str, str],
    cur: dict[str, str],
) -> str | None:
    """Return the rule name if any rule has an identical fingerprint
    in both `prev` and `cur`. None if no overlap."""
    for rule, fp in cur.items():
        if prev.get(rule) == fp:
            return rule
    return None


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run_one_phase(
    args: argparse.Namespace,
    modules: list[str],
    tag: str,
    notes: str,
) -> int:
    """Run a single phase's auto-recover loop. Returns exit code (0 = success)."""
    # Notebook-only phases (classify) configure config.yml but have no snakemake
    # module, so there is nothing to submit. Drop them; a bucket that contains
    # only notebook-only phases is a no-op success.
    runnable = [m for m in modules if m.strip() not in NOTEBOOK_ONLY_PHASES]
    if not runnable:
        notify(
            args.analysis_dir,
            tag,
            f"phase skip: modules={modules} are notebook-only (config written by the "
            f"phase notebook; no snakemake module to run)",
            priority="progress",
        )
        return 0
    modules = runnable
    state = state_dir(args.analysis_dir, tag)
    notify(
        args.analysis_dir,
        tag,
        f"phase start: modules={modules} max_attempts={args.max_attempts} "
        f"backend={args.backend}",
        priority="progress",
    )

    # HARDENING #7 layer 1: --require-rerun check. Before any compute,
    # verify the operator-declared edited rules actually appear in the
    # rebuild set. Bail with a clear error otherwise.
    terminal_module = modules[-1]
    if args.require_rerun:
        ok, classification, detail = dryrun_preview(
            args,
            terminal_module,
            require_rerun=args.require_rerun,
        )
        if not ok:
            notify(
                args.analysis_dir,
                tag,
                f"--require-rerun gate FAILED: classification={classification} | "
                f"{detail}\n"
                f"  Refusing to launch. Force a rebuild with --forcerun "
                f"or delete the rule's outputs manually.",
            )
            return 1
        notify(
            args.analysis_dir,
            tag,
            f"--require-rerun gate ok: classification={classification}",
        )

    stop_mon = threading.Event()
    ctx = MonitorCtx()
    ctx.phase_started_ts = time.time()
    ctx.oom_cascade_threshold = args.oom_cascade_threshold
    ctx.oom_poll_interval_sec = args.oom_poll_seconds
    ctx.adapter = get_backend(args.backend)
    ctx.disk_warn_pct = args.disk_warn_pct
    ctx.disk_abort_pct = args.disk_abort_pct
    ctx.disk_abort_min_free_gb = getattr(
        args, "disk_abort_min_free_gb", ctx.disk_abort_min_free_gb
    )
    mon = start_monitor(args.analysis_dir, tag, ctx, stop_mon)

    # Same-fingerprint short-circuit state (HARDENING #15). One entry per
    # rule we've seen recover.py classify; identical fingerprint twice in a
    # row → the bump is not fixing the failure, escalate.
    last_fingerprints: dict[str, str] = {}

    final_exit = 1
    try:
        for attempt in range(1, args.max_attempts + 1):
            ctx.attempt_started_ts = time.time()
            # Reset cascade detection state for this attempt.
            ctx.oom_cascade_detected = False
            ctx.oom_cascade_rule = None
            ctx.oom_cascade_count = 0
            ctx.last_oom_poll_ts = 0.0
            notify(
                args.analysis_dir,
                tag,
                f"attempt {attempt}/{args.max_attempts} starting",
            )
            run_cmd = build_run_cmd(args, modules, tag, notes, attempt)
            t0 = time.time()
            run_summary = run_capture_json(
                run_cmd,
                state,
                f"attempt{attempt}_run",
                ctx=ctx,
            )
            wall_min = (time.time() - t0) / 60

            # HARDENING #1: if the monitor flipped the cascade flag, the
            # subprocess return code is meaningless — the cascade is our
            # signal of failure. Skip the brieflow_run.py JSON read and
            # synthesize a failure summary so recover.py runs.
            if ctx.oom_cascade_detected:
                notify(
                    args.analysis_dir,
                    tag,
                    f"attempt {attempt}: cascade-detected — routing to "
                    f"recover.py for rule={ctx.oom_cascade_rule} mem bump",
                )
                run_summary = {
                    "success": False,
                    "exit_code": -1,
                    "terminal_marker_present": False,
                    "cascade_rule": ctx.oom_cascade_rule,
                    "cascade_count": ctx.oom_cascade_count,
                }

            if run_summary is None:
                notify(
                    args.analysis_dir,
                    tag,
                    f"attempt {attempt}: brieflow_run.py emitted no JSON; "
                    f"see {state}/attempt{attempt}_run.log — escalating",
                    priority="action_needed",
                )
                final_exit = 1
                break

            if run_summary.get("success"):
                notify(
                    args.analysis_dir,
                    tag,
                    f"SUCCESS attempt {attempt}: {modules} in "
                    f"{run_summary.get('wall_minutes', wall_min):.1f} min "
                    f"(terminal={run_summary.get('terminal_module')})",
                    priority="progress",
                )
                if not args.no_aggregate_efficiency:
                    agg_cmd = [
                        sys.executable,
                        str(AGG_EFF_PY),
                        "--analysis-dir",
                        str(args.analysis_dir),
                    ]
                    if args.mem_recs:
                        agg_cmd.extend(["--out", str(args.mem_recs)])
                    notify(
                        args.analysis_dir, tag, "refreshing mem_recommendations.json"
                    )
                    run_capture_json(
                        agg_cmd, state, f"attempt{attempt}_aggregate", ctx=ctx
                    )
                final_exit = 0
                break

            # Failure path — recover.
            notify(
                args.analysis_dir,
                tag,
                f"attempt {attempt}: brieflow_run.py reported failure "
                f"(exit={run_summary.get('exit_code')}, "
                f"terminal_marker_present={run_summary.get('terminal_marker_present')}); "
                f"invoking brieflow_recover.py",
            )
            rec_cmd = build_recover_cmd(
                args,
                modules,
                attempt,
                oom_rule=run_summary.get("cascade_rule"),
                since=ctx.attempt_started_ts,
            )
            action = run_capture_json(
                rec_cmd, state, f"attempt{attempt}_recover", ctx=ctx
            )
            if action is None:
                notify(
                    args.analysis_dir,
                    tag,
                    f"attempt {attempt}: brieflow_recover.py emitted no JSON; "
                    f"see {state}/attempt{attempt}_recover.log — escalating",
                    priority="action_needed",
                )
                final_exit = 1
                break

            act = action.get("action")
            if act == "resume":
                # HARDENING #15: refuse to re-bump after an identical
                # fingerprint two attempts in a row. The bump didn't help —
                # this isn't an OOM, it's a real bug that needs a human.
                cur_fingerprints = _diagnostics_fingerprint(action)
                repeat_rule = _detect_same_fingerprint(
                    last_fingerprints,
                    cur_fingerprints,
                )
                if repeat_rule is not None:
                    notify(
                        args.analysis_dir,
                        tag,
                        f"ESCALATE attempt {attempt}: same-fingerprint repeat "
                        f"for rule {repeat_rule!r} — recover.py's bump from "
                        f"the previous attempt did not change the failure "
                        f"signature, so this is not OOM-recoverable. "
                        f"Skipping the new bump and escalating.",
                        priority="action_needed",
                    )
                    final_exit = 1
                    break
                last_fingerprints = cur_fingerprints

                rule = action.get("rule")
                old_mem = action.get("old_mem_mb")
                new_mem = action.get("new_mem_mb")
                bits = [f"recovering attempt {attempt}: action=resume rule={rule}"]
                if old_mem is not None and new_mem is not None:
                    bits.append(f"mem {old_mem}→{new_mem} MB")
                if action.get("reason"):
                    bits.append(f"reason={action['reason']}")
                notify(args.analysis_dir, tag, " | ".join(bits), priority="warning")
                continue
            elif act == "success":
                # recover.py found the terminal marker present despite
                # brieflow_run.py reporting failure (typically: snakemake
                # exited non-zero on a no-op resume, OR a prior phase's
                # marker in a log written during this attempt). recover.py
                # ignores logs older than the attempt — phase is complete.
                marker_logs = action.get("marker_logs", [])
                notify(
                    args.analysis_dir,
                    tag,
                    f"attempt {attempt}: recover.py confirmed terminal marker "
                    f"present (in {len(marker_logs)} log(s)); treating as success",
                )
                final_exit = 0
                break
            elif act == "escalate":
                # HARDENING #14: write a diag bundle so the operator gets
                # full context in one place instead of running five greps.
                bundle = write_diag_bundle(
                    args.analysis_dir,
                    tag,
                    reason=f"recover.py escalated: {action.get('reason')}",
                    bundle_kind="escalate",
                    runloop_pid=os.getpid(),
                    extra_sections=[
                        (
                            "recover.py action JSON",
                            json.dumps(action, indent=2, default=str),
                        )
                    ],
                )
                write_last_escalation(
                    args.analysis_dir, tag, attempt, modules, action, bundle
                )
                notify(
                    args.analysis_dir,
                    tag,
                    f"ESCALATE attempt {attempt}: {action.get('reason')}\n"
                    f"  rule={action.get('rule')}\n"
                    f"  slurm_log={action.get('slurm_log')}\n"
                    f"  evidence={action.get('evidence')}\n"
                    f"  diag_bundle={bundle}",
                    priority="action_needed",
                )
                if action.get("should_file_issue"):
                    notify(
                        args.analysis_dir, tag,
                        "Agent action required: invoke /brieflow-issue to investigate and open "
                        "an upstream issue with a backward-compatible fix proposal. "
                        "Keep pipeline source unchanged and the analysis paused."
                    )
                final_exit = 1
                break
            else:
                notify(
                    args.analysis_dir,
                    tag,
                    f"unknown recover action {act!r}; escalating",
                    priority="action_needed",
                )
                final_exit = 1
                break
        else:
            notify(
                args.analysis_dir,
                tag,
                f"max_attempts={args.max_attempts} reached without success",
                priority="action_needed",
            )
            final_exit = 1

    finally:
        stop_mon.set()
        mon.join(timeout=2.0)
        notify(args.analysis_dir, tag, f"phase exit code={final_exit}")

    return final_exit


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--analysis-dir", required=True, type=Path)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--modules",
        nargs="+",
        help="Single-phase: run these modules in one auto-recover loop.",
    )
    mode.add_argument(
        "--phases",
        help="Multi-phase: ';'-separated phases of ','-separated "
        "modules, run sequentially with isolated auto-recover "
        "loops per phase. Halts on first phase escalation. "
        "Single-phase form preferred (the operator-driven path); use "
        "';'-separated per-phase form for autonomous re-runs with "
        "pre-configured notebooks. "
        "Example single: 'sbs'.  "
        "Example all phases: "
        "'preprocess;sbs;phenotype;merge;aggregate;cluster'",
    )
    p.add_argument("--config", default="config/config.yml")
    p.add_argument("--backend", default="slurm", choices=["local", "slurm"])
    p.add_argument(
        "--lifecycle-hooks",
        default="noop",
        choices=["noop", "gcs"],
        help="Data-movement hooks called at phase boundaries. noop (default) "
        "for slurm/NFS where data is already shared. gcs for cloud VMs — "
        "pulls inputs from gs://${BRIEFLOW_BUCKET_INPUT}/<screen-id>/ "
        "pre-phase-1, pushes outputs to gs://${BRIEFLOW_BUCKET_OUTPUT}/ "
        "post-success.",
    )
    p.add_argument(
        "--screen-id",
        default=None,
        help="Screen identifier for lifecycle hooks (GCS bucket prefix, etc.). "
        "Defaults to the analysis_dir basename.",
    )
    # Standalone-CLI defaults. `brieflow_run.py --screen` (the runner) sets its own
    # disk and OOM-cascade values; see the defaults there.
    p.add_argument(
        "--disk-warn-pct",
        type=float,
        default=0.75,
        help="Warn when analysis-dir filesystem reaches this fraction full "
        "(default 0.75). Phase 1.2 disk-space monitor.",
    )
    p.add_argument(
        "--disk-abort-pct",
        type=float,
        default=0.90,
        help="Abort the run when analysis-dir filesystem reaches this fraction "
        "full (default 0.90). Terminates the subprocess + escalates.",
    )
    p.add_argument("--jobs", type=int, default=400)
    p.add_argument("--array-limit", type=int, default=10)
    p.add_argument("--use-arrays", action="store_true")
    p.add_argument("--latency-wait", type=int, default=5)
    p.add_argument("--cpus-per-task", type=int, default=1)
    p.add_argument("--mem-recs", type=Path, default=None)
    p.add_argument("--max-attempts", type=int, default=3)
    p.add_argument(
        "--heartbeat-minutes",
        type=float,
        default=15.0,
        help="Deprecated. Monitor cadence is adaptive (5s burst / "
        "60s steady / 5min idle). "
        "Kept for arg-compat; ignored.",
    )
    p.add_argument("--tag", default=None)
    p.add_argument("--notes", default="")
    p.add_argument(
        "--no-aggregate-efficiency",
        action="store_true",
        help="Skip the post-success mem_recommendations refresh.",
    )
    p.add_argument(
        "--no-smoketest",
        action="store_true",
        help="Skip the env smoketest at launch (HARDENING #16). "
        "Use only when you trust the env state and want a "
        "faster startup.",
    )
    p.add_argument(
        "--require-rerun",
        nargs="*",
        default=[],
        help="Rule names the operator expects to be in the "
        "rebuild set (HARDENING #7). Refuses to launch if "
        "any aren't pending in dry-run.",
    )
    p.add_argument(
        "--forcerun",
        nargs="*",
        default=[],
        help="Rules to force-rerun via snakemake --forcerun "
        "(HARDENING #7 layer 1). Threaded through "
        "brieflow_run.py → flow.sh → snakemake. Use after "
        "editing a rule's script.",
    )
    p.add_argument(
        "--no-phase-gate",
        action="store_true",
        help="Skip the dry-run preview gate between phases "
        "(HARDENING #6). Default: enabled. Override only "
        "when you've verified state by hand.",
    )
    p.add_argument(
        "--oom-cascade-threshold",
        type=int,
        default=100,
        help="Per-rule OOM count in the current attempt above "
        "which the monitor sigterms the subprocess "
        "(HARDENING #1). Default 100.",
    )
    p.add_argument(
        "--oom-poll-seconds",
        type=float,
        default=60.0,
        help="How often the monitor polls sacct for the OOM "
        "cascade check (HARDENING #1). Default 60s.",
    )
    p.add_argument(
        "--no-driver-watchdog",
        action="store_true",
        help="Skip spawning the brieflow_driver_watchdog.py "
        "sidecar (HARDENING #24). Watchdog defaults on; use "
        "this only for ad-hoc / short interactive runs.",
    )
    p.add_argument(
        "--watchdog-poll-seconds",
        type=float,
        default=30.0,
        help="Watchdog poll interval (default 30s).",
    )
    p.add_argument(
        "--skip-instance-lock",
        action="store_true",
        help="Skip the per-analysis-dir runloop lock check. Use "
        "ONLY when you've verified no other runloop is "
        "alive (and accept the risk of state corruption).",
    )
    args = p.parse_args()

    args.analysis_dir = args.analysis_dir.resolve()
    if args.tag is None:
        args.tag = "runloop_" + datetime.now().strftime("%Y%m%d_%H%M%S")

    # Refuse to launch if another runloop is already claiming this analysis
    # dir. Stale locks (dead PID) are reclaimed automatically.
    if not args.skip_instance_lock:
        acquire_instance_lock(args.analysis_dir, args.tag)

    # HARDENING #16: env + config smoketest before any compute. Skip via
    # --no-smoketest for ad-hoc / interactive runs where you trust the env.
    if not args.no_smoketest:
        smoketest_cmd = [
            sys.executable,
            str(SMOKETEST_PY),
            "--analysis-dir",
            str(args.analysis_dir),
            "--backend",
            args.backend,
            "--config",
            args.config,
        ]
        rc = subprocess.run(smoketest_cmd).returncode
        if rc != 0:
            notify(
                args.analysis_dir,
                args.tag,
                f"smoketest failed (rc={rc}); refusing to launch. "
                f"Fix the env or pass --no-smoketest to override.",
            )
            return rc

    # Spawn the driver-death watchdog (HARDENING #24) as a sidecar in its
    # own session so it survives this process being SIGKILL'd (login-node
    # OOM-killer, kernel cgroup-killer on VMs).
    watchdog_proc: subprocess.Popen | None = None
    if not args.no_driver_watchdog:
        watchdog_cmd = [
            sys.executable,
            str(WATCHDOG_PY),
            "--analysis-dir",
            str(args.analysis_dir),
            "--runloop-pid",
            str(os.getpid()),
            "--tag",
            args.tag,
            "--poll-seconds",
            str(args.watchdog_poll_seconds),
        ]
        try:
            watchdog_proc = subprocess.Popen(
                watchdog_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            notify(
                args.analysis_dir,
                args.tag,
                f"driver watchdog sidecar started: pid={watchdog_proc.pid}",
            )
        except OSError as e:
            notify(
                args.analysis_dir,
                args.tag,
                f"WARN: failed to spawn driver watchdog: {e} — "
                f"runloop will continue without it.",
                priority="warning",
            )

        # Atexit cleanup: the watchdog detects our death from polling, but
        # on graceful exit we tell it explicitly so it doesn't fire.
        def _stop_watchdog() -> None:
            if watchdog_proc and watchdog_proc.poll() is None:
                try:
                    watchdog_proc.terminate()
                    watchdog_proc.wait(timeout=5)
                except (subprocess.TimeoutExpired, OSError):
                    try:
                        watchdog_proc.kill()
                    except OSError:
                        pass

        atexit.register(_stop_watchdog)

    # Lifecycle hooks (Phase 1.1) — data movement around phase boundaries.
    # NoopHooks for slurm/NFS (default), GcsHooks for cloud VMs.
    hooks: RunLifecycleHooks = get_hooks(args.lifecycle_hooks)
    screen_id = args.screen_id or args.analysis_dir.name
    atexit.register(
        lambda: hooks.on_teardown(args.analysis_dir, screen_id)  # noqa: B023
    )

    # Pre-phase-1 hook: GcsHooks pulls inputs from GCS here. NoopHooks no-ops.
    try:
        hooks.on_pre_phase_1(args.analysis_dir, screen_id)
    except Exception as e:
        notify(
            args.analysis_dir,
            args.tag,
            f"on_pre_phase_1 hook FAILED: {e} — aborting (can't proceed "
            f"without inputs).",
            priority="action_needed",
        )
        return 1

    # Single-phase mode: run with the user-supplied tag verbatim.
    if args.modules:
        rc = run_one_phase(args, args.modules, args.tag, args.notes)
        if rc == 0:
            _known_phases = [p for p in (_qc.PHASES if _qc else []) if p not in NOTEBOOK_ONLY_PHASES]
            for _module in args.modules:
                if _module.strip() in _known_phases:
                    emit_phase_qc(args.analysis_dir, _module.strip())
            try:
                hooks.on_post_terminal_phase(args.analysis_dir, screen_id)
            except Exception as e:
                notify(
                    args.analysis_dir,
                    args.tag,
                    f"on_post_terminal_phase hook FAILED: {e} — outputs may "
                    f"not be persisted off-VM.",
                    priority="action_needed",
                )
        else:
            try:
                hooks.on_failure(args.analysis_dir, screen_id, None)
            except Exception:
                pass
        return rc

    # Multi-phase mode: parse spec, drive each phase, halt on first failure.
    phases = parse_phases(args.phases)
    notify(
        args.analysis_dir,
        args.tag,
        f"phased runloop start: {len(phases)} phases={phases} "
        f"max_attempts={args.max_attempts} (per phase) backend={args.backend} "
        f"hooks={hooks.name} screen={screen_id}",
    )

    for idx, modules in enumerate(phases, start=1):
        ptag = phase_tag(args.tag, idx, modules)
        pnotes = (
            f"{args.notes} | phase {idx}/{len(phases)}: {modules}"
            if args.notes
            else f"phase {idx}/{len(phases)}: {modules}"
        )
        notify(
            args.analysis_dir,
            args.tag,
            f"==> entering phase {idx}/{len(phases)} tag={ptag} modules={modules}",
        )
        rc = run_one_phase(args, modules, ptag, pnotes)
        if rc != 0:
            notify(
                args.analysis_dir,
                args.tag,
                f"phase {idx}/{len(phases)} FAILED (exit={rc}); "
                f"halting phased run. Remaining phases skipped: "
                f"{phases[idx:]}",
            )
            try:
                hooks.on_failure(args.analysis_dir, screen_id, None)
            except Exception:
                pass
            return rc
        notify(args.analysis_dir, args.tag, f"phase {idx}/{len(phases)} complete")

        # Post-phase QC summary (best-effort): surfaces to the operator and is
        # the artifact the runner will gate on. `modules` is a list[str]; QC
        # keys off each known phase module present.
        _known_phases = [p for p in (_qc.PHASES if _qc else []) if p not in NOTEBOOK_ONLY_PHASES]
        for _module in modules:
            if _module.strip() in _known_phases:
                emit_phase_qc(args.analysis_dir, _module.strip())

        # HARDENING #6: dry-run preview gate between phases. The terminal
        # marker says snakemake satisfied this phase's all_X rule, but it
        # doesn't guarantee downstream consumers will see what they need
        # (caught on a reference run: all_phenotype marker present, then
        # all_aggregate dry-run wanted to re-run 1998 align_phenotype jobs).
        # If the upcoming phase's dry-run reports upstream_rerun, abort
        # before burning compute.
        if not args.no_phase_gate and idx < len(phases):
            next_terminal = phases[idx][-1]
            if next_terminal in NOTEBOOK_ONLY_PHASES:
                # No all_<phase> rule exists to dry-run; nothing to gate on.
                continue
            ok, classification, detail = dryrun_preview(args, next_terminal)
            if not ok:
                notify(
                    args.analysis_dir,
                    args.tag,
                    f"PHASE-GATE FAIL: between phase {idx} and {idx + 1} "
                    f"({modules} → {phases[idx]}): "
                    f"dry-run for `all_{next_terminal}` classified as "
                    f"{classification!r}. {detail}\n"
                    f"  Aborting before phase {idx + 1} so operator can "
                    f"investigate. Pass --no-phase-gate to override.",
                    priority="action_needed",
                )
                return 1
            notify(
                args.analysis_dir,
                args.tag,
                f"phase-gate ok: dry-run for next phase "
                f"`{next_terminal}` is {classification}",
            )

    notify(args.analysis_dir, args.tag, f"ALL {len(phases)} PHASES COMPLETE")
    # Post-terminal-phase hook: GcsHooks pushes brieflow_output/ to GCS.
    try:
        hooks.on_post_terminal_phase(args.analysis_dir, screen_id)
    except Exception as e:
        notify(
            args.analysis_dir,
            args.tag,
            f"on_post_terminal_phase hook FAILED: {e} — outputs may not be "
            f"persisted off-VM. Run cloud/teardown_vm.sh to rescue, or push "
            f"manually via gsutil.",
            priority="action_needed",
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

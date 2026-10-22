#!/usr/bin/env python3
"""brieflow_backend.py — BackendAdapter for failure classification + job introspection.

Decouples recover.py + runloop.py from slurm-specific calls (sacct, squeue,
scancel, per-rule slurm log discovery). Two concrete adapters:

  SlurmBackend  — wraps sacct/squeue/scancel; per-rule logs under
                  analysis_dir/slurm/slurm_output/rule/.
  LocalBackend  — uses snakemake's master log + exit-code parsing for
                  failure classification. Minimum-viable: no dmesg parsing
                  yet. OOM detected
                  via exit code 137 / 9 in snakemake's recorded job exits.
                  Cascade detection is a no-op for local (sequential
                  execution).

The split keeps recover.py's classify_failure / compute_oom_bump as pure
logic on already-extracted dicts, so they work unchanged under either
backend.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol


# ---------------------------------------------------------------------------
# Public datatypes
# ---------------------------------------------------------------------------


@dataclass
class FailedJob:
    """One failed job's observation. Returned by query_failed_jobs.

    `state` is normalized across backends:
      OUT_OF_MEMORY  — kernel/slurm OOM-killed
      FAILED         — non-zero exit, not OOM
      TIMEOUT        — wall time exceeded (slurm only today)
      NODE_FAIL      — node went away (slurm only)
    """

    rule: str
    state: str
    exit_code: str
    max_rss_mb: float
    req_mem_mb: float
    elapsed: str
    jobid: str | None
    extra: dict = field(default_factory=dict)

    def as_legacy_dict(self) -> dict:
        """Shape expected by classify_failure() / compute_oom_bump() in recover.py.

        Those callers were written against the original sacct-dict shape; this
        method preserves backward compat without forcing them to know about
        FailedJob.
        """
        return {
            "state": self.state,
            "exit_code": self.exit_code,
            "max_rss_mb": self.max_rss_mb,
            "req_mem_mb": self.req_mem_mb,
            "elapsed": self.elapsed,
        }


@dataclass
class JobState:
    """Single-job state lookup. Returned by query_job_state.

    `jobid` is backend-specific: slurm jobid string for SlurmBackend;
    LocalBackend returns None from query_job_state (no persistent job
    records to introspect post-hoc).
    """

    jobid: str
    state: str
    exit_code: str
    max_rss_mb: float
    req_mem_mb: float
    elapsed: str
    comment: str = ""
    extra: dict = field(default_factory=dict)

    def as_legacy_dict(self) -> dict:
        """Match the dict shape returned by the original sacct_lookup() in
        recover.py."""
        return {
            "jobid": self.jobid,
            "state": self.state,
            "exit_code": self.exit_code,
            "max_rss_mb": self.max_rss_mb,
            "req_mem_mb": self.req_mem_mb,
            "elapsed": self.elapsed,
            "comment": self.comment,
        }


class BackendAdapter(Protocol):
    """Failure-classification + job-introspection contract.

    All methods are best-effort: implementations return empty results
    (not exceptions) on transient backend errors so callers can degrade
    gracefully. Callers wanting strict error semantics should check
    return shapes explicitly.
    """

    name: str

    def collect_rule_logs(
        self, analysis_dir: Path, rule: str, first_output: str | None
    ) -> list[Path]: ...

    def extract_jobid(self, log_path: Path) -> str | None: ...

    def query_failed_jobs(
        self, analysis_dir: Path, hours_back: int = 2
    ) -> dict[str, list[FailedJob]]: ...

    def query_job_state(self, jobid: str) -> JobState | None: ...

    def count_oom_jobs_since(
        self, analysis_dir: Path, start_ts: float
    ) -> dict[str, int]: ...

    def queue_summary(self) -> dict[str, int]: ...

    def supports_cascade_detection(self) -> bool: ...


# ---------------------------------------------------------------------------
# Helpers (shared)
# ---------------------------------------------------------------------------


def _parse_rss_to_kb(raw: str) -> float:
    """Parse slurm MaxRSS strings ('500K' / '2.5G' / '1024M' / '4096') into KB."""
    if not raw:
        return 0.0
    try:
        if raw.endswith("K"):
            return float(raw[:-1])
        if raw.endswith("M"):
            return float(raw[:-1]) * 1024
        if raw.endswith("G"):
            return float(raw[:-1]) * 1024 * 1024
        return float(raw)
    except ValueError:
        return 0.0


def _parse_mem_to_mb(raw: str) -> float:
    """Parse slurm ReqMem strings ('16G' / '32000M' / '4096') into MB."""
    if not raw:
        return 0.0
    try:
        if raw.endswith("M"):
            return float(raw[:-1])
        if raw.endswith("G"):
            return float(raw[:-1]) * 1024
        return float(raw)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Slurm implementation (logic moved from recover.py + runloop.py)
# ---------------------------------------------------------------------------


_SLURM_JOBID_RE = re.compile(r"(?:Submitted batch job|JOB\s+|StepId=|JobId=)(\d+)")


def _slurm_collect_rule_logs(
    analysis_dir: Path, rule: str, first_output: str | None
) -> list[Path]:
    """Walk analysis_dir/slurm/slurm_output/rule/ for the given rule's logs.

    Matches by grepping each log file for the output filename. mtime is
    unreliable (array tasks land in any order) — see brieflow-speed CLAUDE.md.
    """
    # plugin-slurm >= 2.6.0 manages logs at .snakemake/slurm_logs/<rule>/.../<jobid>.log.
    # Prefer that; fall back to the legacy --output paths for pre-2.6.0 runs.
    log_root = None
    for cand in (
        analysis_dir / ".snakemake" / "slurm_logs",
        analysis_dir / "slurm" / "slurm_output" / "rule",
        analysis_dir / "slurm_logs",
    ):
        if cand.exists():
            log_root = cand
            break
    if log_root is None:
        return []

    target_basename = ""
    if first_output:
        target_basename = Path(first_output).name

    candidates_dirs: list[Path] = []
    for name in (rule, f"rule_{rule}"):
        d = log_root / name
        if d.exists():
            candidates_dirs.append(d)
    for d in log_root.glob("group_*"):
        if rule in d.name:
            candidates_dirs.append(d)
    if not candidates_dirs:
        candidates_dirs = [log_root]

    matches: list[Path] = []
    for cd in candidates_dirs:
        for log_p in cd.rglob("*.log"):
            try:
                with open(log_p, "rb") as f:
                    f.seek(0)
                    head = f.read(200_000)
                    f.seek(max(0, log_p.stat().st_size - 200_000))
                    tail = f.read(200_000)
                blob = head + tail
            except OSError:
                continue
            text = blob.decode("utf-8", errors="replace")
            if target_basename and target_basename in text:
                matches.append(log_p)
            elif not target_basename and rule in log_p.name:
                matches.append(log_p)
    return matches


def _slurm_extract_jobid(log_path: Path) -> str | None:
    """Pull slurm jobid from a per-rule log filename or its contents."""
    stem = log_path.stem
    if stem.isdigit():
        return stem
    try:
        with open(log_path) as f:
            for line in f:
                m = _SLURM_JOBID_RE.search(line)
                if m:
                    return m.group(1)
                if line.startswith("[brieflow]") or line.startswith("Building DAG"):
                    break
    except OSError:
        pass
    return None


def _slurm_query_failed_jobs(hours_back: int = 2) -> dict[str, list[FailedJob]]:
    """sacct window query — return {rule: [FailedJob...]} for OOM jobs.

    Rule name parsed from sacct's Comment field (brieflow's executor
    invocation sets this as `rule_<name>_wildcards_...`).
    """
    from datetime import datetime, timedelta

    start_iso = (datetime.now() - timedelta(hours=hours_back)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    try:
        proc = subprocess.run(
            [
                "sacct",
                "-u",
                os.environ.get("USER", ""),
                "--starttime",
                start_iso,
                "--format=JobID,Comment%80,State,MaxRSS,ReqMem",
                "--parsable2",
                "--noheader",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}

    out: dict[str, list[FailedJob]] = {}
    for line in proc.stdout.splitlines():
        parts = line.split("|")
        if len(parts) < 5:
            continue
        jid, comment, state, rss, req = parts[:5]
        if "OUT_OF_MEMORY" not in state:
            continue
        if not comment.startswith("rule_"):
            continue
        rule_name = comment[len("rule_") :].split("_wildcards")[0]
        if not rule_name:
            continue
        max_rss_mb = _parse_rss_to_kb(rss) / 1024.0
        req_mem_mb = _parse_mem_to_mb(req)
        out.setdefault(rule_name, []).append(
            FailedJob(
                rule=rule_name,
                state="OUT_OF_MEMORY",
                exit_code="0:125",
                max_rss_mb=max_rss_mb,
                req_mem_mb=req_mem_mb,
                elapsed="",
                jobid=jid or None,
                extra={"raw_comment": comment},
            )
        )
    return out


def _slurm_query_job_state(jobid: str) -> JobState | None:
    """sacct -j <jobid> — collapse step rows, return JobState."""
    if not jobid:
        return None
    try:
        proc = subprocess.run(
            [
                "sacct",
                "-j",
                jobid,
                "--format=JobID,State,ExitCode,MaxRSS,ReqMem,Elapsed,Comment%80",
                "--parsable2",
                "--noheader",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None

    state = ""
    exit_code = ""
    max_rss_kb = 0.0
    req_mem_mb = 0.0
    elapsed = ""
    comment = ""
    for line in proc.stdout.splitlines():
        parts = line.split("|")
        if len(parts) < 7:
            continue
        _, st, ec, rss, req, el, cmt = parts[:7]
        if "OUT_OF_MEMORY" in st:
            state = "OUT_OF_MEMORY"
        elif st in ("FAILED", "TIMEOUT", "NODE_FAIL") and state != "OUT_OF_MEMORY":
            state = st
        elif state == "":
            state = st
        if exit_code == "" or ":" in ec:
            exit_code = ec
        max_rss_kb = max(max_rss_kb, _parse_rss_to_kb(rss))
        if req_mem_mb == 0.0:
            req_mem_mb = _parse_mem_to_mb(req)
        if elapsed == "" and el:
            elapsed = el
        if cmt and not comment:
            comment = cmt

    return JobState(
        jobid=jobid,
        state=state,
        exit_code=exit_code,
        max_rss_mb=round(max_rss_kb / 1024, 1) if max_rss_kb else 0.0,
        req_mem_mb=req_mem_mb,
        elapsed=elapsed,
        comment=comment,
    )


def _slurm_count_oom_since(start_ts: float) -> dict[str, int]:
    """{rule: count} of OOM jobs since UNIX timestamp. Cascade-detector input."""
    user = os.environ.get("USER", "")
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


def _slurm_queue_summary() -> dict[str, int]:
    """squeue -u $USER → {state: count}."""
    user = os.environ.get("USER", "")
    if not user:
        return {}
    try:
        proc = subprocess.run(
            ["squeue", "-u", user, "-h", "-o", "%T"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    counts: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        s = line.strip()
        if s:
            counts[s] = counts.get(s, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Local implementation (minimum viable: snakemake-log parse + exit-code OOM)
# ---------------------------------------------------------------------------

# Pattern for `Error in rule X:` blocks in snakemake's master log.
_LOCAL_ERROR_RE = re.compile(
    r"^Error in rule (?P<rule>[a-zA-Z0-9_]+):\s*$", re.MULTILINE
)
# Snakemake records subprocess exit on `Exiting because a job execution failed` /
# `exited with non-zero exit code` / sometimes `Returncode: N`.
_LOCAL_EXITCODE_RE = re.compile(
    r"(?:exited with non-zero exit code\s+|Returncode:\s+|exit\s+code\s+)(\d+)",
    re.IGNORECASE,
)


def _latest_snakemake_log(analysis_dir: Path) -> Path | None:
    log_dir = analysis_dir / ".snakemake" / "log"
    if not log_dir.exists():
        return None
    logs = sorted(
        log_dir.glob("*.snakemake.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return logs[0] if logs else None


def _local_read_log_text(analysis_dir: Path, max_bytes: int = 2_000_000) -> str:
    log_p = _latest_snakemake_log(analysis_dir)
    if log_p is None:
        return ""
    try:
        with open(log_p, "rb") as f:
            f.seek(max(0, log_p.stat().st_size - max_bytes))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _local_collect_rule_logs(
    analysis_dir: Path, rule: str, first_output: str | None
) -> list[Path]:
    """Local snakemake captures rule stdout/stderr inline in the master log
    rather than per-rule files. Return the master log itself so callers that
    grep for error context still find the right text."""
    log_p = _latest_snakemake_log(analysis_dir)
    return [log_p] if log_p else []


def _local_query_failed_jobs(
    analysis_dir: Path, hours_back: int = 2
) -> dict[str, list[FailedJob]]:
    """Parse the snakemake master log for `Error in rule X` blocks.

    For each, look for an exit code in the surrounding text — 137 / 9 → OOM,
    anything else → FAILED. No req_mem_mb / max_rss_mb introspection (snakemake's
    local executor doesn't track these; would need psutil sampling at rule
    runtime, deferred).
    """
    text = _local_read_log_text(analysis_dir)
    if not text:
        return {}

    out: dict[str, list[FailedJob]] = {}
    # Find each error block and the chunk of text following it (within 4 KB).
    for m in _LOCAL_ERROR_RE.finditer(text):
        rule = m.group("rule")
        context = text[m.end() : m.end() + 4000]
        exit_code = ""
        state = "FAILED"
        em = _LOCAL_EXITCODE_RE.search(context)
        if em:
            exit_code = em.group(1)
            if exit_code in ("137", "9"):
                state = "OUT_OF_MEMORY"
        out.setdefault(rule, []).append(
            FailedJob(
                rule=rule,
                state=state,
                exit_code=exit_code,
                max_rss_mb=0.0,
                req_mem_mb=0.0,
                elapsed="",
                jobid=None,
                extra={"context_head": context[:200]},
            )
        )
    return out


def _local_count_oom_since(analysis_dir: Path, start_ts: float) -> dict[str, int]:
    """Local runs jobs sequentially — at most one OOM per moment. Returns
    {rule: 1} for any OOM matching exit-code 137/9 in the snakemake log
    since start_ts (best-effort; no per-event timestamp filtering yet — the
    snakemake log doesn't reliably timestamp every error block)."""
    failed = _local_query_failed_jobs(analysis_dir)
    counts: dict[str, int] = {}
    for rule, jobs in failed.items():
        oom = sum(1 for j in jobs if j.state == "OUT_OF_MEMORY")
        if oom:
            counts[rule] = oom
    return counts


# ---------------------------------------------------------------------------
# Adapter classes
# ---------------------------------------------------------------------------


class SlurmBackend:
    """sacct/squeue/scancel + slurm/slurm_output/rule/ per-rule log walk."""

    name = "slurm"

    def collect_rule_logs(
        self, analysis_dir: Path, rule: str, first_output: str | None
    ) -> list[Path]:
        return _slurm_collect_rule_logs(analysis_dir, rule, first_output)

    def extract_jobid(self, log_path: Path) -> str | None:
        return _slurm_extract_jobid(log_path)

    def query_failed_jobs(
        self, analysis_dir: Path, hours_back: int = 2
    ) -> dict[str, list[FailedJob]]:
        # analysis_dir unused for slurm (sacct queries are global).
        return _slurm_query_failed_jobs(hours_back)

    def query_job_state(self, jobid: str) -> JobState | None:
        return _slurm_query_job_state(jobid)

    def count_oom_jobs_since(
        self, analysis_dir: Path, start_ts: float
    ) -> dict[str, int]:
        # analysis_dir unused for slurm.
        return _slurm_count_oom_since(start_ts)

    def queue_summary(self) -> dict[str, int]:
        return _slurm_queue_summary()

    def supports_cascade_detection(self) -> bool:
        return True


class LocalBackend:
    """snakemake-log parse + exit-code OOM detection. No dmesg yet."""

    name = "local"

    def collect_rule_logs(
        self, analysis_dir: Path, rule: str, first_output: str | None
    ) -> list[Path]:
        return _local_collect_rule_logs(analysis_dir, rule, first_output)

    def extract_jobid(self, log_path: Path) -> str | None:
        # Local has no jobid concept.
        return None

    def query_failed_jobs(
        self, analysis_dir: Path, hours_back: int = 2
    ) -> dict[str, list[FailedJob]]:
        return _local_query_failed_jobs(analysis_dir, hours_back)

    def query_job_state(self, jobid: str) -> JobState | None:
        # Local has no persistent job state — recover.py should rely on
        # query_failed_jobs() for the FailedJob payload instead.
        return None

    def count_oom_jobs_since(
        self, analysis_dir: Path, start_ts: float
    ) -> dict[str, int]:
        return _local_count_oom_since(analysis_dir, start_ts)

    def queue_summary(self) -> dict[str, int]:
        # No queue — single sequential snakemake process.
        return {}

    def supports_cascade_detection(self) -> bool:
        # Sequential local execution doesn't cascade the same way; disable
        # the watcher to avoid false positives from a single OOM event.
        return False


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_backend(name: str) -> BackendAdapter:
    """Return a BackendAdapter for the given name."""
    if name == "slurm":
        return SlurmBackend()
    if name == "local":
        return LocalBackend()
    raise ValueError(f"unknown backend: {name!r} (choose: slurm, local)")

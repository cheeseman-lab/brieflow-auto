#!/usr/bin/env python3
"""brieflow_failure_logs.py — slurm / snakemake failure-log discovery + parsing.

The stack-specific *extraction* layer of the recover pipeline (split out of
brieflow_recover, move-not-rewrite): find the snakemake master log and parse per-rule
error contexts. Per-rule scheduler logs and job state come from the adapters in
brieflow_backend. This is the layer a different execution stack would re-implement; brieflow_classify
(the taxonomy) and brieflow_recover (the memory-bump policy + orchestration)
consume what this returns.
"""

from __future__ import annotations

import re
from pathlib import Path


def find_master_log(analysis_dir: Path, flow_log: Path | None) -> Path | None:
    """Find the snakemake master log most relevant to this flow.sh run.

    Strategy:
      1. If flow_log is provided, parse it for `Complete log(s):` lines, take
         the most recent one referenced.
      2. Else, take the most recent .snakemake/log/*.snakemake.log by mtime.
    """
    snakelog_dir = analysis_dir / ".snakemake" / "log"
    if not snakelog_dir.exists():
        return None

    if flow_log and flow_log.exists():
        # Pick the LAST 'Complete log(s):' line — that's the most recent phase.
        last_logged: Path | None = None
        try:
            with open(flow_log) as f:
                for line in f:
                    if "Complete log(s):" in line:
                        # Format: "Complete log(s): /path/to/.snakemake/log/<ts>.snakemake.log"
                        m = re.search(r"Complete log\(s\):\s*(\S+)", line)
                        if m:
                            cand = Path(m.group(1))
                            if cand.exists():
                                last_logged = cand
        except OSError:
            pass
        if last_logged:
            return last_logged

    candidates = sorted(
        snakelog_dir.glob("*.snakemake.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def find_terminal_marker_logs(
    analysis_dir: Path, terminal_module: str, since: float | None = None
) -> list[Path]:
    """Find snakemake master logs with the all_<terminal> marker, written at or after ``since``."""
    snakelog_dir = analysis_dir / ".snakemake" / "log"
    if not snakelog_dir.exists():
        return []
    marker = f"Finished jobid: 0 (Rule: all_{terminal_module})"
    found: list[Path] = []
    for p in snakelog_dir.glob("*.snakemake.log"):
        try:
            if since is not None and p.stat().st_mtime < since:
                continue
            with open(p) as f:
                if marker in f.read():
                    found.append(p)
        except OSError:
            continue
    return found


# ---------------------------------------------------------------------------
# Error parsing
# ---------------------------------------------------------------------------


_ERROR_IN_RULE_RE = re.compile(r"^\s*Error in rule (\S+):\s*$", re.MULTILINE)
_RULE_CONTEXT_RE = re.compile(
    r"^\s*Error in rule (\S+):[^\n]*\n"  # 1: rule name (indented in group jobs)
    r"(?:[^\n]*\n){0,80}?"  # up to 80 lines of context
    r"^\s*jobid:\s*(\d+)[^\n]*\n"  # 2: snakemake jobid
    r"(?:[^\n]*\n){0,80}?"
    r"^\s*output:\s*(.+)$",  # 3: output file(s)
    re.MULTILINE,
)

# Snakemake's slurm executor logs `log:` paths inside Error blocks AND
# `Job <N> has been submitted with SLURM jobid <M>` lines elsewhere. The log
# path is the most authoritative way to find the per-rule slurm log for the
# specific failing attempt — beats filename grep + mtime, especially when the
# same output was retried multiple times across resumes.
_ERROR_LOG_PATH_RE = re.compile(
    r"^\s*Error in rule (\S+):[^\n]*\n"  # 1: rule
    r"(?:[^\n]*\n){0,80}?"
    r"^\s*jobid:\s*(\d+)[^\n]*\n"  # 2: snakemake jobid
    r"(?:[^\n]*\n){0,80}?"
    r"^\s*log:\s*(\S+\.log)",  # 3: slurm log path
    re.MULTILINE,
)

# `Job <snakemake_jobid> has been submitted with SLURM jobid <slurm_jobid>`
_JOB_TO_SLURM_RE = re.compile(
    r"Job\s+(\d+)\s+has been submitted with SLURM jobid\s+(\d+)"
)


_SBATCH_CALL_RE = re.compile(r"^sbatch call: (.*)$", re.MULTILINE)
_SBATCH_PARTITION_RE = re.compile(r"(?<!\S)(?:-p|--partition)[ =](\S+)")


def find_submission_failure(master_log: Path) -> dict | None:
    """Snakemake called sbatch but no job ever reached Slurm: every submission was rejected.

    Returns the call count and the partitions the calls asked for, or None when at least
    one job was submitted (or nothing was attempted)."""
    if not master_log or not master_log.exists():
        return None
    try:
        text = master_log.read_text(errors="replace")
    except OSError:
        return None
    calls = _SBATCH_CALL_RE.findall(text)
    if not calls or "has been submitted with SLURM jobid" in text:
        return None
    if not _ERROR_IN_RULE_RE.search(text):
        return None
    partitions = sorted({m for c in calls for m in _SBATCH_PARTITION_RE.findall(c)})
    return {"sbatch_calls": len(calls), "submitted": 0, "partitions": partitions}


def parse_errors_from_master_log(master_log: Path) -> list[dict]:
    """Parse `Error in rule X` blocks. Returns dicts with rule, outputs, AND
    (when snakemake's slurm executor recorded them) the slurm log path and
    slurm jobid for the specific failing attempt.
    """
    if not master_log or not master_log.exists():
        return []
    try:
        text = master_log.read_text(errors="replace")
    except OSError:
        return []

    # Build snakemake_jobid -> slurm_jobid map from submission lines.
    snake_to_slurm: dict[str, str] = {}
    for m in _JOB_TO_SLURM_RE.finditer(text):
        snake_to_slurm[m.group(1)] = m.group(2)

    # Build snakemake_jobid -> slurm log path map from `log:` lines inside
    # Error blocks (most authoritative — points at the actual failing job's log).
    snake_to_log: dict[str, str] = {}
    for m in _ERROR_LOG_PATH_RE.finditer(text):
        snake_jobid = m.group(2)
        log_path = m.group(3)
        snake_to_log.setdefault(snake_jobid, log_path)

    errors: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for m in _RULE_CONTEXT_RE.finditer(text):
        rule = m.group(1)
        snakemake_jobid = m.group(2)
        outputs_line = m.group(3).strip()
        first_output = outputs_line.split(",")[0].strip() if outputs_line else ""
        key = (rule, first_output)
        if key in seen:
            continue
        seen.add(key)
        errors.append(
            {
                "rule": rule,
                "snakemake_jobid": snakemake_jobid,
                "first_output": first_output,
                "outputs_line": outputs_line,
                "slurm_log_from_master": snake_to_log.get(snakemake_jobid),
                "slurm_jobid_from_master": snake_to_slurm.get(snakemake_jobid),
            }
        )

    # Fallback for rules without full context blocks — capture rule name only.
    if not errors:
        for m in _ERROR_IN_RULE_RE.finditer(text):
            rule = m.group(1)
            if any(e["rule"] == rule for e in errors):
                continue
            errors.append(
                {
                    "rule": rule,
                    "snakemake_jobid": "",
                    "first_output": "",
                    "outputs_line": "",
                    "slurm_log_from_master": None,
                    "slurm_jobid_from_master": None,
                }
            )

    return errors


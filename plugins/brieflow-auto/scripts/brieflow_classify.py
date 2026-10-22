#!/usr/bin/env python3
"""brieflow_classify.py — failure taxonomy (classify, don't blind-retry).

The classification layer of the recover pipeline (split out of brieflow_recover,
move-not-rewrite): given a rule's log text + its sacct row, decide the failure
class (OOM / config-mismatch / NFS-latency / FD-limit / unknown) that
brieflow_recover's policy then acts on. Pure text/regex over the log + the sacct
dict — no I/O, no slurm calls.
"""

from __future__ import annotations

import re


_OOM_HINT_RES = [
    # Strict signals only — patterns that are unambiguous evidence of an
    # actual OOM kill. Loose phrases like "Out Of Memory" or the bare
    # "OUT_OF_MEMORY" literal could match log lines that merely MENTION
    # OOM as context, not the failure itself (caught: a downstream
    # ValueError log that referenced OOM in its traceback was mis-routed
    # as recoverable_oom and wasted a bump cycle). HARDENING #13.
    re.compile(r"slurmstepd:.*Detected\s+\d+\s+oom_kill", re.IGNORECASE),
    re.compile(r"oom-killer", re.IGNORECASE),
    re.compile(r"\bOOMKilled\b"),
    re.compile(r"\bexit code 137\b", re.IGNORECASE),
]

_CONFIG_MISMATCH_RES = [
    # ValueError("Unknown <key>: <value>. Choose ...") — the cp_method case
    re.compile(r"ValueError:\s*Unknown\s+\w+:.*Choose"),
    # KeyError on a config key during script execution
    re.compile(r"KeyError:\s*'(?:[a-z_]+)'\s*$", re.MULTILINE),
]

_NFS_LATENCY_RES = [
    re.compile(r"MissingOutputException"),
    re.compile(r"output\s+files?\s+after\s+\d+\s+seconds.\s*This"),
]

_FD_LIMIT_RES = [
    # Open-file-descriptor exhaustion (soft `ulimit -n` too low for the job
    # fan-out). zarr stores each image as thousands of tiny files, so at high
    # --cores the default soft nofile=1024 is blown through. Caught jannu
    # 2026-06-22: preprocess died at 60% with
    #   OSError: [Errno 24] Too many open files: '.../.snakemake/metadata/...zarr.json'
    # Fixed at the source by brieflow_run.py raising the soft limit to the hard
    # cap before launch; a retry then succeeds, so this is a clean resume.
    re.compile(r"\[Errno 24\]\s*Too many open files", re.IGNORECASE),
    re.compile(r"\bEMFILE\b"),
]


def classify_failure(rule_log_text: str, sacct: dict | None) -> tuple[str, str]:
    """Return (category, evidence) where category ∈
    {recoverable_oom, nfs_latency, config_mismatch, unknown}."""
    # OOM signals (any of: slurmstepd line, sacct State, exit 137, or
    # FAILED-with-MaxRSS-near-cap which is an OOM that slurmstepd happened
    # to mark as plain FAILED instead of OUT_OF_MEMORY — caught 2026-05-11
    # A reference run: final_merge MaxRSS 273GB on 350GB cap, sacct State=FAILED
    # exit=1:0, recover.py missed it as OOM and escalated)
    if sacct and sacct.get("state") == "OUT_OF_MEMORY":
        return (
            "recoverable_oom",
            f"sacct State=OUT_OF_MEMORY (exit {sacct.get('exit_code')})",
        )
    if sacct and sacct.get("exit_code", "").startswith("137"):
        return (
            "recoverable_oom",
            f"sacct ExitCode={sacct['exit_code']} (137 = SIGKILL, OOM)",
        )
    if sacct and "oom" in (sacct.get("comment") or "").lower():
        return "recoverable_oom", (
            f"sacct Comment contains 'oom': {sacct['comment'][:120]}"
        )
    if sacct and sacct.get("state") == "FAILED":
        peak = sacct.get("max_rss_mb", 0) or 0
        cap = sacct.get("req_mem_mb", 0) or 0
        if cap > 0 and peak >= 0.80 * cap:
            return "recoverable_oom", (
                f"sacct State=FAILED with MaxRSS {peak:.0f}MB ≥ 80% of "
                f"ReqMem {cap:.0f}MB — OOM in disguise (slurmstepd marked "
                f"FAILED instead of OUT_OF_MEMORY)"
            )
    for r in _OOM_HINT_RES:
        m = r.search(rule_log_text)
        if m:
            return "recoverable_oom", f"log match: {m.group(0)[:80]}"

    # Config mismatch (ValueError Unknown / KeyError on config)
    for r in _CONFIG_MISMATCH_RES:
        m = r.search(rule_log_text)
        if m:
            return "config_mismatch", f"log match: {m.group(0)[:120]}"

    # NFS latency (MissingOutputException)
    for r in _NFS_LATENCY_RES:
        m = r.search(rule_log_text)
        if m:
            return "nfs_latency", f"log match: {m.group(0)[:80]}"

    # Open-file-descriptor exhaustion (raise ulimit -n at source, then resume)
    for r in _FD_LIMIT_RES:
        m = r.search(rule_log_text)
        if m:
            return "recoverable_fd_limit", f"log match: {m.group(0)[:80]}"

    return "unknown", "no recognized signature"

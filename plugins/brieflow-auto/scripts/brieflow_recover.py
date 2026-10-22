#!/usr/bin/env python3
"""
brieflow_recover.py — Failure classifier + memory bumper for brieflow runs.

Reads a flow.sh aggregate log and the snakemake master log it points at,
locates per-rule slurm logs for any errored rules (matching by output filename
grep, NEVER by mtime), runs sacct on those
jobs, and emits a structured recovery action as JSON on stdout.

Designed to be invoked from /brieflow-module as the failure-classification step.
The script does NOT relaunch — it only decides what to do. The slash command
applies the action.

Failure taxonomy:
  recoverable_oom   — exit 137 / State=OUT_OF_MEMORY / slurmstepd oom_kill_event.
                      Action: bump that rule's mem_mb in mem_recommendations.json
                      and resume.
  nfs_latency       — MissingOutputException in the master log.
                      Action: bump --latency-wait and resume.
  recoverable_fd_limit — [Errno 24] Too many open files (soft nofile exhausted by
                      the zarr job fan-out). Action: resume — brieflow_run.py
                      raises the soft limit to the hard cap before relaunch.
  config_mismatch   — ValueError("Unknown ...") / KeyError on config in the
                      script traceback. Action: escalate (preflight should
                      have caught this; if it didn't, run preflight).
  unknown           — anything else (real rule-code bug, novel mode).
                      Action: escalate. The runner writes .brieflow/issue_draft.md;
                      /brieflow-issue investigates and automatically opens a report
                      proposing a backward-compatible fix for an identified defect.

Exit codes:
  0  — action JSON written to stdout; caller should apply it
  1  — script error (couldn't read inputs, etc.)
  2  — no failure detected (run looks clean per the marker check)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from brieflow_backend import get_backend  # noqa: E402
from brieflow_failure_logs import (  # noqa: E402,F401  (re-export: log/sacct extraction)
    find_master_log,
    find_submission_failure,
    find_terminal_marker_logs,
    parse_errors_from_master_log,
)
from brieflow_classify import classify_failure  # noqa: E402,F401  (re-export: failure taxonomy)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# How aggressively to bump memory after an OOM. The observed peak underestimates
# the true peak when the rule was OOM-clipped at the cap (slurmstepd kills before
# the rule allocates further). Tonight's data: merge_phenotype peak observed at
# 32 GB cap, true peak with headroom turned out to be 83 GB.
#   - First-time bump: 4× the observed peak, ceiling at 4× the cap-at-peak.
#   - On repeated OOM (cap-at-peak >= 8 GB): 4× the cap-at-peak, since observed
#     peak is already known to be at-cap and unreliable as a bottom line.
OOM_FIRST_BUMP_X = 4.0
OOM_REPEAT_BUMP_X = 4.0

# Threshold to call "peak-at-cap" — within 5% of the request, indistinguishable
# from "killed before allocating more."
PEAK_AT_CAP_FRAC = 0.95
DEFAULT_MEM_MB = 3000  # slurm profile default-resources mem_mb (brieflow_compute)

# Caps on absolute mem_mb to prevent runaway escalation. If a recommendation
# would exceed this, we escalate instead of writing it.
MAX_REASONABLE_MEM_MB = 1_500_000  # 1.5 TB — cluster's largest u20 nodes have
# 1.9 TB; nvidia-A100-20 has 1.9 TB; manual caps already running at 322 GB on
# the reference screen's calculate_ic_phenotype. The previous 512 GB ceiling was too tight —
# a reference run (phase 2) escalated when final_merge needed ~300 GB and recover.py's
# 4× cap-at-peak computed 714 GB > 512 GB. Anything beyond 1.5 TB legitimately
# warrants asking cluster IT first.

# --latency-wait bump (seconds), hard-capped at 10. A longer wait only delays
# surfacing a genuinely-missing output; the real fix is mem/retry, not waiting.
LATENCY_BUMP_LADDER = [10]

# Per-rule log searches are filename greps over the slurm log tree. A mass failure
# (thousands of identical errors) must not turn classification into an hours-long stall.
MAX_DIAGNOSED_PER_RULE = 20

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def emit(action: dict) -> None:
    """Write the action JSON to stdout for the caller to consume."""
    json.dump(action, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def info(msg: str) -> None:
    """Stderr logging — does not pollute the action JSON."""
    sys.stderr.write(f"[brieflow_recover] {msg}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Recovery: memory bump
# ---------------------------------------------------------------------------


def _ceil_to_step(val_mb: float, step: int = 1000) -> int:
    return int(((val_mb // step) + 1) * step)


def compute_oom_bump(
    sacct: dict,
    current_recommendation: dict | None,
    formula_floor: int | None = None,
) -> tuple[int, str]:
    """Compute the new mem_mb_recommended after an OOM.

    Returns (new_mem_mb, reasoning_string).
    """
    observed_peak = sacct.get("max_rss_mb", 0.0) or 0.0
    cap_at_peak = sacct.get("req_mem_mb", 0.0) or 0.0

    # If observed peak is at-cap (>= 95% of req), the true peak is unknown —
    # the rule was killed before allocating further. Use cap-at-peak as the
    # bottom line and bump aggressively.
    at_cap = cap_at_peak > 0 and observed_peak >= cap_at_peak * PEAK_AT_CAP_FRAC

    if current_recommendation:
        prev_rec = current_recommendation.get("mem_mb_recommended", 0)
    else:
        prev_rec = 0

    if at_cap:
        # observed_peak unreliable — bump from cap-at-peak.
        new_mem = _ceil_to_step(cap_at_peak * OOM_REPEAT_BUMP_X)
        reason = (
            f"peak-at-cap ({observed_peak:.0f} MB at {cap_at_peak:.0f} MB cap); "
            f"bumping {OOM_REPEAT_BUMP_X}x cap → {new_mem} MB"
        )
    else:
        # First-time OOM with peak below cap — bump from observed peak.
        new_mem = _ceil_to_step(observed_peak * OOM_FIRST_BUMP_X)
        reason = (
            f"observed peak {observed_peak:.0f} MB (< cap {cap_at_peak:.0f} MB); "
            f"bumping {OOM_FIRST_BUMP_X}x peak → {new_mem} MB"
        )

    # Never recommend less than the previous recommendation.
    if prev_rec and new_mem < prev_rec * 1.5:
        new_mem = _ceil_to_step(prev_rec * 1.5)
        reason += f" (raised to 1.5x previous rec {prev_rec})"

    # A rule killed at the default cap (formula unresolved) needs its geometry estimate.
    if formula_floor and new_mem < formula_floor:
        new_mem = _ceil_to_step(formula_floor)
        reason += f" (raised to the rule's formula estimate {formula_floor} MB)"

    return new_mem, reason


def write_mem_recommendation(
    mem_recs_path: Path,
    rule: str,
    new_mem_mb: int,
    sacct: dict,
    tier: str = "well",
) -> dict:
    """Upsert a manual-override entry in mem_recommendations.json.

    The override is flagged with obs_source="manual_override_after_OOM_<date>"
    so the aggregator (brieflow_aggregate_efficiency.py) can preserve it on
    refresh — this is the fix for tonight's "merge_phenotype wiped from
    mem_recommendations after re-aggregating" issue.
    """
    if mem_recs_path.exists():
        try:
            with open(mem_recs_path) as f:
                recs = json.load(f)
        except (json.JSONDecodeError, OSError):
            recs = {}
    else:
        recs = {}

    today = datetime.now().strftime("%Y-%m-%d")
    entry = {
        "WARNING": (f"manual_override_after_OOM_{today}_actual_peak_may_be_higher"),
        "mem_mb_recommended": new_mem_mb,
        "obs_cap_at_peak_mb": sacct.get("req_mem_mb", 0.0),
        "obs_n": (recs.get(rule, {}).get("obs_n", 0) or 0) + 1,
        "obs_peak_rss_mb": sacct.get("max_rss_mb", 0.0),
        "obs_source": f"manual_override_after_OOM_{today}",
        "overhead": "explicit_post_OOM_bump",
        "tier": tier,
    }
    recs[rule] = entry

    mem_recs_path.parent.mkdir(parents=True, exist_ok=True)
    with open(mem_recs_path, "w") as f:
        json.dump(recs, f, indent=2, sort_keys=True)
    return entry


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _profile_partition(analysis_dir: Path) -> str | None:
    """The slurm_partition the base profile names, as text (None if absent)."""
    try:
        import yaml

        cfg = yaml.safe_load((analysis_dir / "slurm" / "config.yaml").read_text()) or {}
    except Exception:
        return None
    value = (cfg.get("default-resources") or {}).get("slurm_partition")
    return None if value is None else str(value)


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--analysis-dir",
        required=True,
        type=Path,
        help="Screen analysis dir (contains config/, slurm/, "
        "brieflow_output*/, .snakemake/).",
    )
    p.add_argument(
        "--flow-log",
        type=Path,
        default=None,
        help="flow.sh aggregate log path (optional; "
        "if omitted, the most recent .snakemake/log/*.log is used).",
    )
    p.add_argument(
        "--modules",
        nargs="+",
        required=True,
        help="Modules in scope, in order. Last one is the terminal module.",
    )
    p.add_argument(
        "--mem-recs",
        type=Path,
        default=None,
        help="Path to mem_recommendations.json. "
        "Default: <analysis-dir>/slurm/mem_recommendations.json "
        "(legacy locations under cluster_optimization/results/ "
        "and harness/results/ also discovered). "
        "Created if missing.",
    )
    p.add_argument(
        "--attempt",
        type=int,
        default=1,
        help="Current attempt number (informational; included in output).",
    )
    p.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Cap; if attempt >= max_attempts the action becomes 'escalate' "
        "even for otherwise-recoverable failures.",
    )
    p.add_argument(
        "--oom-rule",
        nargs="+",
        default=None,
        help="Rule(s) the runloop's OOM-cascade monitor caught OUT_OF_MEMORY before "
        "Snakemake logged an `Error in rule` block (it killed the run early on "
        "purpose). Treated as recoverable_oom at the rule's current cap.",
    )
    p.add_argument(
        "--since",
        type=float,
        default=None,
        help="Epoch seconds the attempt started; a terminal marker in an older log is "
        "not success.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't write to mem_recommendations.json; only emit JSON action.",
    )
    p.add_argument(
        "--backend",
        default="slurm",
        choices=["slurm", "local"],
        help="Failure-classification backend. slurm: sacct/squeue/per-rule slurm "
        "logs (default). local: snakemake-log parse + exit-code OOM detection.",
    )
    args = p.parse_args()
    adapter = get_backend(args.backend)

    analysis_dir = args.analysis_dir.resolve()
    if not analysis_dir.exists():
        info(f"ERROR: --analysis-dir {analysis_dir} does not exist.")
        return 1

    # Auto-discover mem_recommendations.json. Canonical location is
    # <analysis-dir>/slurm/ (next to slurm/config.yaml); legacy locations
    # under cluster_optimization/results/ + harness/results/ are still
    # discovered for backwards compat.
    def _discover_mem_recs(adir: Path) -> Path:
        candidates = [
            adir / "slurm" / "mem_recommendations.json",
            adir / "cluster_optimization" / "results" / "mem_recommendations.json",
            adir / "harness" / "results" / "mem_recommendations.json",
            adir / "mem_recommendations.json",
        ]
        for c in candidates:
            if c.exists():
                return c
        return candidates[0]

    mem_recs_path = args.mem_recs or _discover_mem_recs(analysis_dir)

    # Load current recommendations (if any) for previous-rec lookups.
    current_recs: dict = {}
    if mem_recs_path.exists():
        try:
            with open(mem_recs_path) as f:
                current_recs = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    try:
        from brieflow_resolve_resources import formula_caps

        formula_floors = formula_caps(analysis_dir)
    except Exception as e:  # the floor is a refinement; never block classification on it
        info(f"formula floor unavailable: {e}")
        formula_floors = {}

    terminal_module = args.modules[-1]
    info(f"analysis_dir={analysis_dir}")
    info(f"terminal_module={terminal_module} modules={args.modules}")
    info(f"attempt={args.attempt}/{args.max_attempts}")

    # ---- success check ----
    success_logs = find_terminal_marker_logs(analysis_dir, terminal_module, since=args.since)
    if success_logs:
        info(
            f"all_{terminal_module} marker present in: {[str(p) for p in success_logs]}"
        )
        emit(
            {
                "action": "success",
                "terminal_module": terminal_module,
                "marker_logs": [str(p) for p in success_logs],
                "attempt": args.attempt,
            }
        )
        return 2  # 2 means "no failure detected"

    # ---- find master log + parse errors ----
    master_log = find_master_log(analysis_dir, args.flow_log)
    if not master_log:
        info("ERROR: no snakemake master log found.")
        emit(
            {
                "action": "escalate",
                "reason": "no snakemake master log under .snakemake/log/",
                "attempt": args.attempt,
            }
        )
        return 0

    info(f"master_log={master_log}")
    errors = parse_errors_from_master_log(master_log)
    diagnostics: list[dict] = []
    if not errors and args.oom_rule:
        # The runloop's cascade monitor terminated Snakemake as soon as N jobs
        # of a rule went OUT_OF_MEMORY — before any `Error in rule` block could
        # land in the master log. The rule name IS the diagnosis: treat it as a
        # peak-at-cap OOM at its current recommendation so the bump path runs.
        for rule in args.oom_rule:
            cap = float((current_recs.get(rule) or {}).get("mem_mb_recommended") or DEFAULT_MEM_MB)
            diagnostics.append(
                {
                    "rule": rule,
                    "category": "recoverable_oom",
                    "evidence": "OOM cascade reported by the runloop monitor (sacct OUT_OF_MEMORY)",
                    "snakemake_jobid": None,
                    "first_output": None,
                    "sacct": {"state": "OUT_OF_MEMORY", "max_rss_mb": cap, "req_mem_mb": cap},
                }
            )
        info(f"cascade hint: treating {args.oom_rule} as recoverable_oom at current cap")
    elif not errors:
        info(
            "master log has no `Error in rule X:` blocks but no terminal marker either"
        )
        emit(
            {
                "action": "escalate",
                "reason": "no terminal marker but no rule errors parsed; check flow.sh for non-snakemake failure",
                "master_log": str(master_log),
                "attempt": args.attempt,
            }
        )
        return 0

    info(f"parsed {len(errors)} unique rule-error context(s)")

    # ---- submission failure: no job reached the scheduler ----
    submission = find_submission_failure(master_log) if args.backend == "slurm" else None
    if submission:
        configured = _profile_partition(analysis_dir)
        asked = submission["partitions"]
        reason = (
            f"{submission['sbatch_calls']} sbatch call(s), none submitted: every job was "
            f"rejected before reaching Slurm (partition(s) requested: {asked or 'none'})"
        )
        if configured and asked and any(a != configured for a in asked):
            reason += (
                f"; the profile names partition {configured!r}. Snakemake sums numeric "
                f"resources across a job group, so quote slurm_partition (and slurm_account) "
                f"as strings in slurm/config.yaml"
            )
        emit(
            {
                "action": "escalate",
                "classification": "submission_failure",
                "reason": reason,
                "submission": submission,
                "attempt": args.attempt,
                "should_file_issue": False,
            }
        )
        return 0

    # ---- classify each errored rule ----
    # When the master log records `log:` and `Job <X> ... SLURM jobid <Y>` lines
    # (which it does for plain slurm jobs), use those directly — they identify
    # the SPECIFIC failing attempt, beats filename-grep + mtime heuristics that
    # otherwise pick up later successful attempts of the same rule.
    per_rule_seen: dict[str, int] = {}
    for err in errors:
        rule = err["rule"]
        per_rule_seen[rule] = per_rule_seen.get(rule, 0) + 1
        if per_rule_seen[rule] > MAX_DIAGNOSED_PER_RULE:
            continue
        sl: Path | None = None
        slurm_jobid: str | None = err.get("slurm_jobid_from_master")

        # Prefer master-log path
        master_log_path = err.get("slurm_log_from_master")
        if master_log_path:
            cand = Path(master_log_path)
            if cand.exists():
                sl = cand

        # Fallback: filename-grep search via adapter
        if not sl:
            slurm_logs = adapter.collect_rule_logs(
                analysis_dir, rule, err["first_output"]
            )
            if slurm_logs:
                # Prefer the log that contains the failure marker; use mtime as last resort
                sl = max(slurm_logs, key=lambda p: p.stat().st_mtime)

        if not sl:
            diagnostics.append(
                {
                    "rule": rule,
                    "category": "unknown",
                    "evidence": "no per-rule slurm log found (no master-log log: line, no filename match)",
                    "snakemake_jobid": err["snakemake_jobid"],
                    "first_output": err["first_output"],
                }
            )
            continue

        try:
            with open(sl, "rb") as f:
                f.seek(0)
                head = f.read(200_000)
                f.seek(max(0, sl.stat().st_size - 200_000))
                tail = f.read(200_000)
            text = (head + tail).decode("utf-8", errors="replace")
        except OSError:
            text = ""

        if not slurm_jobid:
            slurm_jobid = adapter.extract_jobid(sl)
        js = adapter.query_job_state(slurm_jobid) if slurm_jobid else None
        sacct = js.as_legacy_dict() if js else None
        category, evidence = classify_failure(text, sacct)

        diagnostics.append(
            {
                "rule": rule,
                "category": category,
                "evidence": evidence,
                "snakemake_jobid": err["snakemake_jobid"],
                "first_output": err["first_output"],
                "slurm_log": str(sl),
                "slurm_jobid": slurm_jobid,
                "sacct": sacct,
            }
        )

    capped = {r: n for r, n in per_rule_seen.items() if n > MAX_DIAGNOSED_PER_RULE}
    if capped:
        info(f"classified the first {MAX_DIAGNOSED_PER_RULE} error(s) per rule; skipped {capped}")

    # ---- decide on action ----
    # Priority of categories: recoverable_oom > nfs_latency > config_mismatch > unknown
    # (within recoverable_oom, fix the specific rule)
    by_category: dict[str, list[dict]] = {}
    for d in diagnostics:
        by_category.setdefault(d["category"], []).append(d)

    # Attempts cap
    if args.attempt >= args.max_attempts:
        emit(
            {
                "action": "escalate",
                "reason": f"reached max_attempts={args.max_attempts}",
                "diagnostics": diagnostics,
                "attempt": args.attempt,
                "should_file_issue": (
                    "unknown" in by_category or "config_mismatch" in by_category
                ),
            }
        )
        return 0

    if "recoverable_oom" in by_category and args.backend == "local":
        # On a single local VM, mem_mb is ADVISORY (not a hard kill cap) and there is
        # no scheduler to grant more memory — bumping mem_mb is a no-op and cannot
        # recover an OOM. Local per-rule log identification is also fuzzy (no slurm
        # jobid to key on), so a NON-OOM failure (e.g. a rule ValueError) can be
        # mis-tagged recoverable_oom here in the first place. Either way, escalate
        # immediately instead of burning a pointless mem-bump→retry cycle. A genuine
        # local RAM OOM is fixed by lowering --cores (peak concurrency), not mem_mb.
        oom_rules = sorted({d["rule"] for d in by_category["recoverable_oom"]})
        emit(
            {
                "action": "escalate",
                "reason": (
                    f"recoverable_oom flagged for {oom_rules} but backend=local: "
                    f"mem_mb is advisory on local (no kill cap, no scheduler), so a "
                    f"bump cannot help. If this is a real RAM OOM, lower --cores to "
                    f"reduce peak concurrency; otherwise it is a NON-OOM failure — "
                    f"check the rule traceback in the diagnostics. Not wasting a "
                    f"mem-bump retry on a single VM."
                ),
                "diagnostics": diagnostics,
                "attempt": args.attempt,
                "should_file_issue": False,
            }
        )
        return 0

    if "recoverable_oom" in by_category:
        # Bump EVERY distinct rule that OOM'd in this attempt — not just one.
        # Previous behavior picked a single "best" rule (highest cap-at-peak),
        # which burned through max-attempts when 2+ rules OOM'd in the same
        # attempt: each cycle bumped one rule, the next attempt OOM'd on the
        # untouched rules, recover bumped one more, etc. Caught 2026-05-09
        # a reference run (phase 2): combine_reads + combine_cells both OOM'd in
        # attempt 1, recover bumped only combine_reads, attempt 2 OOM'd on
        # combine_cells, attempt 3 hit eval_mapping + eval_segmentation_sbs
        # simultaneously → escalated with rule=None.
        oom_diags = by_category["recoverable_oom"]

        # Group by rule (multiple OOM diagnostics can hit the same rule with
        # different wildcards — we want one bump per rule, using the worst
        # cap-at-peak across that rule's diagnostics).
        per_rule: dict[str, dict] = {}
        for d in oom_diags:
            r = d["rule"]
            cur_best = per_rule.get(r)
            cur_best_cap = (
                (cur_best or {}).get("sacct", {}).get("req_mem_mb", 0)
                if cur_best
                else 0
            )
            this_cap = (d.get("sacct") or {}).get("req_mem_mb", 0)
            if cur_best is None or this_cap > cur_best_cap:
                per_rule[r] = d

        bumps_written: list[dict] = []
        bumps_skipped: list[dict] = []
        for rule, d in sorted(per_rule.items()):
            sacct = d.get("sacct") or {}
            new_mem, reason = compute_oom_bump(
                sacct, current_recs.get(rule), formula_floors.get(rule)
            )

            if new_mem > MAX_REASONABLE_MEM_MB:
                bumps_skipped.append(
                    {
                        "rule": rule,
                        "would_be_new_mem_mb": new_mem,
                        "reason": (
                            f"exceeds MAX_REASONABLE_MEM_MB={MAX_REASONABLE_MEM_MB}"
                        ),
                    }
                )
                continue

            tier = (current_recs.get(rule) or {}).get("tier", "well")
            if not args.dry_run:
                entry = write_mem_recommendation(
                    mem_recs_path, rule, new_mem, sacct, tier=tier
                )
            else:
                entry = {"mem_mb_recommended": new_mem, "tier": tier, "DRY_RUN": True}

            bumps_written.append(
                {
                    "rule": rule,
                    "old_mem_mb": (current_recs.get(rule) or {}).get(
                        "mem_mb_recommended"
                    ),
                    "new_mem_mb": new_mem,
                    "reasoning": reason,
                    "tier": tier,
                    "entry": entry,
                }
            )

        # If every bump was skipped for exceeding MAX_REASONABLE_MEM_MB, escalate.
        if not bumps_written:
            emit(
                {
                    "action": "escalate",
                    "reason": (
                        f"all {len(bumps_skipped)} OOM-rule bumps exceed "
                        f"MAX_REASONABLE_MEM_MB={MAX_REASONABLE_MEM_MB}; "
                        "dataset-scale issue, ask cluster IT or revisit script."
                    ),
                    "skipped_bumps": bumps_skipped,
                    "diagnostics": diagnostics,
                    "attempt": args.attempt,
                    "should_file_issue": True,
                }
            )
            return 0

        # Primary rule for stdout/log visibility = highest new_mem_mb (the most
        # consequential bump). Runloop's notify line cites this one; all bumps
        # are persisted in mem_recommendations.json regardless.
        primary = max(bumps_written, key=lambda b: b["new_mem_mb"])

        emit(
            {
                "action": "resume",
                "rule": primary["rule"],
                "old_mem_mb": primary["old_mem_mb"],
                "new_mem_mb": primary["new_mem_mb"],
                "reasoning": primary["reasoning"],
                "tier": primary["tier"],
                "mem_recs_path": str(mem_recs_path),
                "entry_written": primary["entry"],
                "all_bumps": bumps_written,  # full list for visibility
                "skipped_bumps": bumps_skipped,
                "diagnostics": diagnostics,
                "attempt": args.attempt,
            }
        )
        return 0

    if "recoverable_fd_limit" in by_category:
        # FD exhaustion is fixed at the source: brieflow_run.py raises the soft
        # nofile limit to the hard cap before launching snakemake. A plain resume
        # then proceeds (snakemake skips the work already completed).
        emit(
            {
                "action": "resume",
                "subaction": "raise_nofile",
                "reason": (
                    "[Errno 24] Too many open files — soft nofile limit exhausted by "
                    "zarr job fan-out; brieflow_run.py now raises it to the hard cap, "
                    "resume to continue"
                ),
                "diagnostics": diagnostics,
                "attempt": args.attempt,
            }
        )
        return 0

    if "nfs_latency" in by_category:
        # Bump latency_wait per the ladder (caller passes current latency_wait
        # via env or trial JSON; we just suggest the next step).
        emit(
            {
                "action": "resume",
                "subaction": "bump_latency_wait",
                "ladder": LATENCY_BUMP_LADDER,
                "reason": "MissingOutputException matched but file likely on disk; bump --latency-wait",
                "diagnostics": diagnostics,
                "attempt": args.attempt,
            }
        )
        return 0

    if "config_mismatch" in by_category:
        emit(
            {
                "action": "escalate",
                "reason": "config↔rule-code mismatch — run /brieflow-preflight to identify the offending key",
                "diagnostics": diagnostics,
                "attempt": args.attempt,
                "should_file_issue": False,  # user-fixable
            }
        )
        return 0

    # ---- sacct-window fallback ----
    # If log parsing yielded no recoverable_oom diagnostics but sacct shows
    # OOMs in the recent run window, treat them as recoverable_oom and bump.
    # Without this, multi-rule OOM cascades that the master log can't cleanly
    # attribute to specific rules (or that fall outside the per-rule slurm log
    # discovery) escalate as "unknown" with no actual recovery attempt.
    # Caught on a reference run (phase 2, attempt 3): eval_mapping +
    # eval_segmentation_sbs both OOM'd simultaneously, recover.py couldn't
    # classify, escalated.
    # Adapter-dispatched sacct/snakemake-log window query. SlurmBackend returns
    # OOM jobs from sacct; LocalBackend returns OOM-classified failures from
    # the snakemake master log (exit-code 137/9).
    # Skip the fallback on the LOCAL backend: mem_mb is advisory there (a bump is a
    # no-op), and LocalBackend.query_failed_jobs parses the same master log we already
    # classified per-rule above — re-scanning it here only risks re-tagging non-OOM
    # failures as OOM (the merge fast_alignment ValueError that bumped mem 23900→83000
    # uselessly on a single VM). Real local OOMs are already caught as recoverable_oom
    # above (and escalated); everything else escalates as unknown below.
    _failed_jobs = (
        {}
        if args.backend == "local"
        else adapter.query_failed_jobs(analysis_dir, hours_back=2)
    )
    sacct_oom_rules = {
        rule: [fj.as_legacy_dict() for fj in fjs] for rule, fjs in _failed_jobs.items()
    }
    if sacct_oom_rules:
        info(
            f"sacct fallback: found {sum(len(v) for v in sacct_oom_rules.values())} "
            f"OOM jobs across {len(sacct_oom_rules)} distinct rules: "
            f"{list(sacct_oom_rules.keys())}"
        )
        # Pick worst-cap sacct entry per rule and bump
        bumps_written: list[dict] = []
        for rule, sacct_list in sorted(sacct_oom_rules.items()):
            best_sacct = max(sacct_list, key=lambda s: s.get("req_mem_mb", 0))
            new_mem, reason = compute_oom_bump(
                best_sacct, current_recs.get(rule), formula_floors.get(rule)
            )
            if new_mem > MAX_REASONABLE_MEM_MB:
                info(
                    f"  skipping {rule}: would-be cap {new_mem} > MAX_REASONABLE_MEM_MB"
                )
                continue
            tier = (current_recs.get(rule) or {}).get("tier", "well")
            if not args.dry_run:
                entry = write_mem_recommendation(
                    mem_recs_path, rule, new_mem, best_sacct, tier=tier
                )
            else:
                entry = {"mem_mb_recommended": new_mem, "tier": tier, "DRY_RUN": True}
            bumps_written.append(
                {
                    "rule": rule,
                    "old_mem_mb": (current_recs.get(rule) or {}).get(
                        "mem_mb_recommended"
                    ),
                    "new_mem_mb": new_mem,
                    "reasoning": reason,
                    "tier": tier,
                    "entry": entry,
                }
            )
        if bumps_written:
            primary = max(bumps_written, key=lambda b: b["new_mem_mb"])
            emit(
                {
                    "action": "resume",
                    "rule": primary["rule"],
                    "old_mem_mb": primary["old_mem_mb"],
                    "new_mem_mb": primary["new_mem_mb"],
                    "reasoning": primary["reasoning"] + " (via sacct-window fallback)",
                    "tier": primary["tier"],
                    "mem_recs_path": str(mem_recs_path),
                    "entry_written": primary["entry"],
                    "all_bumps": bumps_written,
                    "via": "sacct_window_fallback",
                    "diagnostics": diagnostics,
                    "attempt": args.attempt,
                }
            )
            return 0

    # All unknowns
    emit(
        {
            "action": "escalate",
            "reason": "no recognized failure signature — likely real rule-code bug or novel mode",
            "diagnostics": diagnostics,
            "attempt": args.attempt,
            "should_file_issue": True,  # candidate for /brieflow-issue
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

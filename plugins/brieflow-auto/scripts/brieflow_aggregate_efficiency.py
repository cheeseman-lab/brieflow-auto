#!/usr/bin/env python3
"""
brieflow_aggregate_efficiency.py — Roll up worst-case peak RSS per rule
across all logs/efficiency_*/*.csv files in a brieflow analysis dir, write
recommended `mem_mb` per rule into mem_recommendations.json.

Self-correcting memory tuning: each successful run drops a fresh
efficiency CSV, this command rolls them all up so recommendations
improve over time without manual intervention.

PRESERVES manual override entries (those with obs_source containing
"manual_override" or "manual_from_sacct"). This is the fix for the
2026-04-30 issue where re-aggregating wiped a merge_phenotype manual
bump that wasn't yet visible in any efficiency CSV.

Ported from the lab's earlier benchmarking harness
(harness.py:cmd_aggregate_efficiency), with these changes:
- accepts --analysis-dir / --output as args (parametric, not fixed paths)
- preserves manual override entries
- bundled RULE_MEMORY_PROFILE (no harness import)
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants — bundled from harness.py for self-containment
# ---------------------------------------------------------------------------

RULE_MEMORY_PROFILE: dict[str, dict] = {
    # preprocess
    "convert_sbs": {"scales_with": "tile", "dag_sensitive": False},
    "convert_phenotype": {"scales_with": "tile", "dag_sensitive": False},
    "extract_metadata_sbs": {"scales_with": "tile", "dag_sensitive": True},
    "extract_metadata_phenotype": {"scales_with": "tile", "dag_sensitive": True},
    "calculate_ic_sbs": {"scales_with": "well", "dag_sensitive": True},
    "calculate_ic_phenotype": {"scales_with": "well", "dag_sensitive": True},
    "combine_metadata_sbs": {"scales_with": "well", "dag_sensitive": True},
    "combine_metadata_phenotype": {"scales_with": "well", "dag_sensitive": True},
    # sbs per-tile
    "align_sbs": {"scales_with": "tile", "dag_sensitive": False},
    "log_filter": {"scales_with": "tile", "dag_sensitive": False},
    "compute_standard_deviation": {"scales_with": "tile", "dag_sensitive": False},
    "find_peaks": {"scales_with": "tile", "dag_sensitive": False},
    "max_filter": {"scales_with": "tile", "dag_sensitive": False},
    "apply_ic_field_sbs": {"scales_with": "tile", "dag_sensitive": False},
    "segment_sbs": {"scales_with": "tile", "dag_sensitive": False},
    "extract_bases": {"scales_with": "tile", "dag_sensitive": False},
    "call_reads": {"scales_with": "tile", "dag_sensitive": False},
    "call_cells": {"scales_with": "tile", "dag_sensitive": False},
    "extract_sbs_info": {"scales_with": "tile", "dag_sensitive": False},
    # sbs aggregation
    "combine_reads": {"scales_with": "well", "dag_sensitive": True},
    "combine_cells": {"scales_with": "well", "dag_sensitive": True},
    "combine_sbs_info": {"scales_with": "well", "dag_sensitive": True},
    # phenotype per-tile
    "apply_ic_field_phenotype": {"scales_with": "tile", "dag_sensitive": False},
    "align_phenotype": {"scales_with": "tile", "dag_sensitive": False},
    "segment_phenotype": {"scales_with": "tile", "dag_sensitive": False},
    "identify_cytoplasm": {"scales_with": "tile", "dag_sensitive": False},
    "extract_phenotype_info": {"scales_with": "tile", "dag_sensitive": False},
    "extract_phenotype": {"scales_with": "tile", "dag_sensitive": False},
    # phenotype aggregation
    "combine_phenotype_info": {"scales_with": "well", "dag_sensitive": True},
    "merge_phenotype": {"scales_with": "well", "dag_sensitive": True},
    "eval_features": {"scales_with": "well", "dag_sensitive": True},
    "eval_segmentation_phenotype": {"scales_with": "well", "dag_sensitive": True},
    "eval_segmentation_sbs": {"scales_with": "well", "dag_sensitive": True},
    # merge / classify / aggregate / cluster (downstream)
    "merge": {"scales_with": "well", "dag_sensitive": True},
    "merge_phenotype_cp": {"scales_with": "well", "dag_sensitive": True},
    "deduplicate_merge": {"scales_with": "well", "dag_sensitive": True},
    "format_merge": {"scales_with": "well", "dag_sensitive": True},
    "clean_merge": {"scales_with": "well", "dag_sensitive": True},
    "eval_merge": {"scales_with": "well", "dag_sensitive": True},
    "eval_mapping": {"scales_with": "well", "dag_sensitive": True},
    "final_merge": {"scales_with": "well", "dag_sensitive": True},
    "filter": {"scales_with": "well", "dag_sensitive": True},
    "fast_alignment": {"scales_with": "well", "dag_sensitive": True},
    "split_datasets": {"scales_with": "well", "dag_sensitive": True},
    "aggregate": {"scales_with": "well", "dag_sensitive": True},
    "clean_aggregate": {"scales_with": "well", "dag_sensitive": True},
    "eval_aggregate": {"scales_with": "well", "dag_sensitive": True},
    "generate_feature_table": {"scales_with": "well", "dag_sensitive": True},
    "phate_leiden_clustering": {"scales_with": "well", "dag_sensitive": True},
    "benchmark_clusters": {"scales_with": "well", "dag_sensitive": True},
    "bootstrap_gene": {"scales_with": "well", "dag_sensitive": True},
    "combine_bootstrap": {"scales_with": "well", "dag_sensitive": True},
    "initiate_bootstrap": {"scales_with": "well", "dag_sensitive": True},
    "initiate_montage": {"scales_with": "well", "dag_sensitive": True},
    "generate_montage": {"scales_with": "well", "dag_sensitive": True},
    "prepare_montage_data": {"scales_with": "well", "dag_sensitive": True},
    # Added 2026-05-04: rules that ran in v2/v3b but were missing from the
    # allow-list and got silently dropped during aggregation.
    "align": {"scales_with": "well", "dag_sensitive": True},
    "fast_merge": {"scales_with": "well", "dag_sensitive": True},
    "prepare_bootstrap_data": {"scales_with": "well", "dag_sensitive": True},
    "sbs_tile_group": {"scales_with": "tile", "dag_sensitive": False},
    "phenotype_tile_group": {"scales_with": "tile", "dag_sensitive": False},
}

AGGREGATE_OVERHEAD = 1.5
PEAK_AT_CAP_THRESHOLD = 0.95


# ---------------------------------------------------------------------------
# Snakemake group membership.
#
# brieflow uses snakemake's `group:` directive to bundle all per-tile sbs and
# phenotype work into single slurm jobs (one per tile). At submission time
# slurm sees only `sbs_tile_group` / `phenotype_tile_group` JobNames (the
# concatenation of constituent rules, truncated by sacct at ~101 chars), so
# efficiency CSVs only ever observe the GROUP, never the individual rules.
#
# Constituent rule lookups (e.g. "is align_sbs covered?") must resolve to
# the parent group before checking mem_recommendations.json. Without this
# map, dry-run previews and gap-checking tools manufacture false alarms
# claiming the per-tile rules are uncovered when they share a group cap.
# ---------------------------------------------------------------------------

SBS_TILE_GROUP_MEMBERS: tuple[str, ...] = (
    "align_sbs",
    "apply_ic_field_sbs",
    "log_filter",
    "compute_standard_deviation",
    "max_filter",
    "segment_sbs",
    "find_peaks",
    "extract_bases",
    "extract_sbs_info",
    "call_reads",
    "call_cells",
)

PHENOTYPE_TILE_GROUP_MEMBERS: tuple[str, ...] = (
    "align_phenotype",
    "apply_ic_field_phenotype",
    "segment_phenotype",
    "identify_cytoplasm",
    "extract_phenotype_info",
    "extract_phenotype",
    "extract_phenotype_cp",
)

GROUP_MEMBERSHIP: dict[str, tuple[str, ...]] = {
    "sbs_tile_group": SBS_TILE_GROUP_MEMBERS,
    "phenotype_tile_group": PHENOTYPE_TILE_GROUP_MEMBERS,
}


# Recognized markers indicating a manual override to PRESERVE on aggregation.
MANUAL_OVERRIDE_MARKERS = (
    "manual_override",
    "manual_from_sacct",
    "manual_post_oom",
)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def is_manual_override(entry: dict) -> bool:
    src = (entry or {}).get("obs_source") or ""
    return any(m in src for m in MANUAL_OVERRIDE_MARKERS)


def aggregate(log_dir: Path) -> tuple[dict, list[dict], list[Path]]:
    """Walk all logs/efficiency_*/*.csv. Returns (per-rule agg, per-job
    observation rows, source csv paths). The observation rows are the input
    that HARDENING_TODO #23 (per-rule scaling formulas) needs to fit
    coefficients; the per-rule agg is the input to build_recommendations.
    """
    csvs: list[Path] = []
    if log_dir.exists():
        for d in log_dir.iterdir():
            if d.is_dir() and d.name.startswith("efficiency_"):
                csvs.extend(d.glob("*.csv"))
    agg, observations = _aggregate_from_csvs(csvs)
    return agg, observations, csvs


def _aggregate_from_csvs(csvs: list[Path]) -> tuple[dict, list[dict]]:
    agg: dict[str, dict] = {}
    observations: list[dict] = []
    # A grouped job reports one row per job step (8025341.0 .. .4); count it once.
    by_job: dict[tuple[str, str], dict] = {}
    for csv_path in csvs:
        source_report = csv_path.parent.name
        try:
            with open(csv_path) as f:
                for row in csv.DictReader(f):
                    rule_raw = row.get("RuleName") or ""
                    parts = rule_raw.split("_wildcards", 1)
                    rule = parts[0].replace("rule_", "")
                    wildcards = parts[1].lstrip("_") if len(parts) == 2 else ""
                    # sacct truncates RuleName at ~101 chars; tile_group jobs
                    # are concatenations of every per-tile rule and the
                    # truncated tail loses the `_wildcards` marker, so the
                    # generic split above can't normalize them. Map any name
                    # starting with sbs_tile_group_/phenotype_tile_group_ to
                    # that prefix bucket.
                    for prefix in ("sbs_tile_group", "phenotype_tile_group"):
                        if rule.startswith(prefix + "_") or rule == prefix:
                            rule = prefix
                            break
                    if not rule or rule not in RULE_MEMORY_PROFILE:
                        continue
                    try:
                        rss = float(row["MaxRSS_MB"]) if row.get("MaxRSS_MB") else 0.0
                        cap = (
                            float(row["RequestedMem_MB"])
                            if row.get("RequestedMem_MB")
                            else 0.0
                        )
                        elapsed = (
                            float(row["Elapsed_sec"]) if row.get("Elapsed_sec") else 0.0
                        )
                        cpu_eff = (
                            float(row["CPU Efficiency (%)"])
                            if row.get("CPU Efficiency (%)")
                            else 0.0
                        )
                    except ValueError:
                        continue
                    if rss <= 0:
                        continue
                    cur = agg.setdefault(
                        rule, {"peak": 0.0, "cap_at_peak": 0.0, "n": 0}
                    )
                    main = row.get("MainJobID") or (row.get("JobID") or "").split(".")[0]
                    seen = by_job.get((rule, main)) if main else None
                    if seen is not None:
                        seen["MaxRSS_MB"] = max(seen["MaxRSS_MB"], rss)
                        seen["Elapsed_s"] += elapsed
                    else:
                        obs = {
                            "rule": rule,
                            "wildcards": wildcards,
                            "MaxRSS_MB": rss,
                            "ReqMem_MB": cap,
                            "Elapsed_s": elapsed,
                            "CPU_Eff_pct": cpu_eff,
                            "source_report": source_report,
                        }
                        observations.append(obs)
                        if main:
                            by_job[(rule, main)] = obs
                        cur["n"] += 1
                    if rss > cur["peak"]:
                        cur["peak"] = rss
                        cur["cap_at_peak"] = cap
        except OSError as e:
            print(
                f"[brieflow_aggregate_efficiency] WARN: skipping {csv_path}: {e}",
                file=sys.stderr,
            )
    return agg, observations


OBSERVATION_CSV_COLUMNS = (
    "rule",
    "wildcards",
    "MaxRSS_MB",
    "ReqMem_MB",
    "Elapsed_s",
    "CPU_Eff_pct",
    "source_report",
)


def write_observations_csv(out_path: Path, observations: list[dict]) -> None:
    """Emit per-job rows for HARDENING_TODO #23 formula-fitter consumption.

    Schema matches the per-job observations table the baseline formulas were
    bootstrapped from.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OBSERVATION_CSV_COLUMNS)
        w.writeheader()
        w.writerows(observations)


def _ceil_to_100(x: float) -> int:
    return int(((x // 100) + 1) * 100)


def build_recommendations(
    agg: dict,
    existing: dict,
    overhead: float = AGGREGATE_OVERHEAD,
    peak_at_cap_frac: float = PEAK_AT_CAP_THRESHOLD,
) -> dict:
    """Build the final recommendations dict, preserving manual overrides
    and merging in observed-peak-derived recs for everything else.
    """
    recommendations: dict = {}

    # 1. Carry forward manual overrides verbatim.
    preserved: list[str] = []
    for rule, entry in (existing or {}).items():
        if is_manual_override(entry):
            recommendations[rule] = entry
            preserved.append(rule)

    # 2. For each observed rule, if it's a manual override don't touch it.
    #    Otherwise compute a recommendation from observed peak — but take
    #    max(new, existing) to preserve any non-manual safety bumps already
    #    in the file. Without this, a successful run with low MaxRSS clobbers
    #    a higher cap that was set after a prior OOM, regressing the safety
    #    margin and causing the next phase to OOM again.
    for rule, cur in sorted(agg.items()):
        if rule in recommendations:  # manual override already preserved
            continue
        tier = RULE_MEMORY_PROFILE[rule]["scales_with"]
        new_rec = _ceil_to_100(cur["peak"] * overhead)
        existing_entry = (existing or {}).get(rule) or {}
        existing_rec = int(existing_entry.get("mem_mb_recommended") or 0)
        rec = max(new_rec, existing_rec)
        entry = {
            "tier": tier,
            "obs_peak_rss_mb": cur["peak"],
            "obs_cap_at_peak_mb": cur["cap_at_peak"],
            "obs_n": cur["n"],
            "obs_source": "efficiency_aggregated",
            "overhead": overhead,
            "mem_mb_recommended": rec,
        }
        if rec > new_rec:
            entry["preserved_existing_higher"] = existing_rec
        if (
            cur["cap_at_peak"] > 0
            and cur["peak"] >= cur["cap_at_peak"] * peak_at_cap_frac
        ):
            entry["WARNING"] = "peak_was_at_cap_actual_peak_may_be_higher"
        recommendations[rule] = entry

    # 3. Carry forward existing non-manual entries for rules that didn't
    #    appear in this aggregation (e.g. rules whose efficiency CSV wasn't
    #    in this run's logs/efficiency_*/). Without this they'd silently
    #    drop out of the recommendations file on every aggregation.
    for rule, entry in (existing or {}).items():
        if rule not in recommendations and not is_manual_override(entry):
            recommendations[rule] = entry

    # 4. Annotate group entries with their members, and constituent rules
    #    with a back-reference to the group that owns them. This makes the
    #    JSON self-describing: any downstream tool (dry-run preview, gap
    #    check, etc.) can resolve a rule to its effective cap without
    #    knowing about snakemake group directives. Stop manufacturing
    #    false-alarm "missing rule" reports for grouped rules.
    for group, members in GROUP_MEMBERSHIP.items():
        if group in recommendations:
            recommendations[group]["group_members"] = list(members)
        for member in members:
            if member in recommendations:
                # Don't clobber a manually overridden member's entry; just
                # mark the group it belongs to.
                recommendations[member]["group"] = group
            else:
                # Synthesize a stub so consumers can find the group from
                # the rule name without needing the GROUP_MEMBERSHIP map.
                recommendations[member] = {
                    "obs_source": "group_member_stub",
                    "group": group,
                }

    return recommendations, preserved


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_table(recommendations: dict, preserved: list[str]) -> None:
    print(
        f"{'Rule':<32} {'Tier':>5} {'N':>5} {'PeakMB':>10} "
        f"{'CapMB':>10} {'RecMB':>10}  Flag"
    )
    print("-" * 88)
    for rule, v in sorted(
        recommendations.items(), key=lambda kv: (kv[1].get("tier", ""), kv[0])
    ):
        flag = v.get("WARNING", "")
        if rule in preserved:
            flag = (flag + " manual_override").strip()
        n = v.get("obs_n", "")
        peak = v.get("obs_peak_rss_mb", 0)
        cap = v.get("obs_cap_at_peak_mb", 0)
        rec = v.get("mem_mb_recommended", 0)
        print(
            f"{rule:<32} {v.get('tier', ''):>5} {n:>5} "
            f"{peak:>10.1f} {cap:>10.1f} {rec:>10}  {flag}"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def discover_mem_recs_path(analysis_dir: Path) -> Path:
    """Find the conventional mem_recommendations.json location for this
    analysis dir. Returns the first existing file from a priority list, or
    the first candidate path if none exists yet (used for new-file creation).

    Canonical location is `<analysis-dir>/slurm/mem_recommendations.json`
    — sits next to `slurm/config.yaml` since both are per-screen operational
    sizing config that the runtime consumes. Legacy locations are still
    discovered for backwards compat with existing screens.
    """
    candidates = [
        analysis_dir / "slurm" / "mem_recommendations.json",
        analysis_dir / "cluster_optimization" / "results" / "mem_recommendations.json",
        analysis_dir / "harness" / "results" / "mem_recommendations.json",
        analysis_dir / "mem_recommendations.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--analysis-dir",
        required=True,
        type=Path,
        help="Screen analysis dir (contains logs/, "
        "and the target mem_recommendations.json).",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Path to write mem_recommendations.json. "
        "Default: auto-discover in this order: "
        "<analysis-dir>/cluster_optimization/results/, "
        "<analysis-dir>/harness/results/, "
        "<analysis-dir>/mem_recommendations.json",
    )
    p.add_argument(
        "--observation-csv",
        type=Path,
        default=None,
        help="Path to write per-job observation rows. Default: "
        "<analysis-dir>/cluster_optimization/results/"
        "observations_raw_<YYYY-MM-DD>.csv. Pass an explicit "
        "path to override; pass '' or omit --observation-csv "
        "to suppress (currently writes by default).",
    )
    p.add_argument(
        "--no-observation-csv",
        action="store_true",
        help="Skip writing the per-job observation CSV.",
    )
    p.add_argument(
        "--overhead",
        type=float,
        default=AGGREGATE_OVERHEAD,
        help=f"Multiplier on observed peak. Default {AGGREGATE_OVERHEAD}.",
    )
    p.add_argument("--quiet", action="store_true", help="Suppress the per-rule table.")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't write the JSON; just print what would be written.",
    )
    p.add_argument(
        "--no-baseline-seed",
        action="store_true",
        help="Skip seeding from the plugin's "
        "mem_recommendations_baseline.json on first aggregation. "
        "Used by tests and for fully-isolated runs.",
    )
    args = p.parse_args()

    analysis_dir = args.analysis_dir.resolve()
    if not analysis_dir.exists():
        print(f"ERROR: --analysis-dir {analysis_dir} does not exist.", file=sys.stderr)
        return 1

    out = args.output or discover_mem_recs_path(analysis_dir)

    log_dir = analysis_dir / "logs"
    agg, observations, csvs = aggregate(log_dir)

    existing: dict = {}
    if out.exists():
        try:
            with open(out) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing = {}

    # Seed from the plugin's baseline if the screen has no JSON yet, so a
    # first-time aggregation on a new screen doesn't lose the baseline values
    # (e.g. manual overrides for rules that didn't run yet on this screen).
    plugin_baseline = (
        Path(__file__).resolve().parent.parent
        / "data"
        / "mem_recommendations_baseline.json"
    )
    if not existing and plugin_baseline.exists() and not args.no_baseline_seed:
        try:
            with open(plugin_baseline) as f:
                existing = json.load(f)
            print(
                f"[aggregate_efficiency] seeded from plugin baseline "
                f"({len(existing)} rules) — first-time aggregation on "
                f"this screen.",
                file=sys.stderr,
            )
        except (json.JSONDecodeError, OSError):
            existing = {}

    if not agg and not existing:
        print(
            f"ERROR: no efficiency CSVs under {log_dir}/efficiency_*/ "
            f"and no existing {out.name} to preserve.",
            file=sys.stderr,
        )
        return 1

    recommendations, preserved = build_recommendations(
        agg, existing, overhead=args.overhead
    )

    n_obs = sum(v["n"] for v in agg.values())
    print(
        f"\n=== brieflow_aggregate_efficiency "
        f"({len(csvs)} CSVs, {n_obs} rule-observations, "
        f"{len(preserved)} manual-override preserved) ==="
    )

    if not args.quiet:
        print_table(recommendations, preserved)

    obs_out: Path | None = None
    if observations and not args.no_observation_csv:
        if args.observation_csv is not None:
            obs_out = args.observation_csv
        else:
            # Audit/raw data lives in cluster_optimization/results/ regardless
            # of where mem_recs is written; the two have different lifetimes
            # (raw observations are gitignored audit data, mem_recs is the
            # trusted-cap config the runtime consumes).
            from datetime import date

            obs_out = (
                analysis_dir
                / "cluster_optimization"
                / "results"
                / f"observations_raw_{date.today().isoformat()}.csv"
            )

    if args.dry_run:
        print(f"\n[DRY RUN] would write to {out}", file=sys.stderr)
        if obs_out is not None:
            print(
                f"[DRY RUN] would write {len(observations)} observations to {obs_out}",
                file=sys.stderr,
            )
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        json.dump(recommendations, f, indent=2, sort_keys=True)
    print(f"\nSaved to {out}")

    if obs_out is not None:
        write_observations_csv(obs_out, observations)
        print(f"Wrote {len(observations)} observations to {obs_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

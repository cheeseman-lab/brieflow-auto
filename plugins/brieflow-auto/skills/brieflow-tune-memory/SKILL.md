---
name: brieflow-tune-memory
description: Use to refresh memory recommendations (mem_mb per rule) for a brieflow screen from accumulated efficiency CSVs. Rolls up worst-case peak RSS across all prior runs, preserves manual-override entries from post-OOM bumps. Triggered by "tune memory", "refresh memory recommendations", "update mem_recs", etc.
---

# Brieflow — Tune memory recommendations

Self-correcting hygiene step. Rolls up worst-case peak RSS per rule across
`logs/efficiency_*/*.csv` in the analysis dir; writes recommended `mem_mb` to
`slurm/mem_recommendations.json` (legacy `cluster_optimization/results/`,
`harness/results/` auto-discovered for backwards compat). The runner
(`brieflow_run.py`) consumes this as `set-resources: <rule>: mem_mb: <X>`.

**Preserves manual override entries** (entries with `obs_source` containing
`manual_override`). Protects post-OOM bumps from being silently wiped.

## When to run

- **After every successful pipeline run** — keeps recommendations current.
- **Before launching a new run** — `brieflow-module` invokes this automatically;
  explicit use is mostly ad-hoc tuning or recovery diagnostics.

## Steps

1. **Detect screen repo.**
2. **Run aggregator:**
   ```bash
   # SCRIPTS_DIR = the plugin's scripts/ dir; resolve from this skill's base dir
   # (printed as "Base directory for this skill:" at invocation):
   SCRIPTS_DIR=$(realpath "<skill-base-dir>/../../scripts")
   $SCRIPTS_DIR/brieflow_aggregate_efficiency.py --analysis-dir <abs>
   ```
3. **Show per-rule table** in the response. Highlight any rule flagged
   `WARNING: peak_was_at_cap_actual_peak_may_be_higher` — those recs are lower
   bounds (the rule got OOM-clipped).
4. **List preserved manual overrides** so the operator sees what survived
   the refresh.

## Output schema

Per-rule table: `tier`, `obs_n`, `obs_peak_rss_mb`, `obs_cap_at_peak_mb`,
`mem_mb_recommended`, plus any `WARNING` or `manual_override` flag.

JSON to `mem_recommendations.json` consumed by `brieflow_run.py` when
building the per-trial slurm profile.

## Rules

- **Idempotent** — no new CSVs → identical output (overrides preserved).
- **Preserves overrides** — never wipes entries with `obs_source` matching
  `manual_override*` or `manual_from_sacct*`.
- **`--overhead` multiplier**: default 1.5× on observed peak (held up across
  2026-04 well-tier runs). Use `2.0` for conservative new-screen runs.
- **`--dry-run`** to preview without writing.

## Workarounds are findings

Every workaround made in place (a `screen.yaml` edit, an input symlink tree, a resource
stopgap, a sticky interview-row rewrite) and every defect you cannot fix is a finding:
call `record_finding()` from `scripts/brieflow_findings.py` before continuing (router rule
R5).

## Plugin script

`<plugin>/scripts/brieflow_aggregate_efficiency.py` — invoke with absolute path.

## Exit

Return to router (`brieflow-auto`).

---
name: brieflow-preflight
description: Use to statically validate a brieflow analysis config.yml against the brieflow submodule's rule code before launching a pipeline run. Catches config↔rule-code mismatches in seconds (the cp_method-style "Unknown" errors). Triggered by "preflight", "validate config", "check config", "before I launch", etc.
---

# Brieflow — Preflight config validation

Statically validates `config/config.yml` against the brieflow submodule's
rule code. Pure AST + regex + YAML; never executes anything. Sub-second.

## When to use

- **Before every pipeline launch.** Catches `ValueError: Unknown X: {X}` at
  submit time, not after 200 jobs queued.
- **Postmortem** on a `ValueError: Unknown ...` failure — confirms cause + finds
  other latent mismatches.

## What it checks

| Check | Severity | What it does |
|---|---|---|
| Accepted-value guards | HIGH | Walks brieflow scripts for `raise ValueError("Unknown X: ...")` patterns; validates `config[X]` against the accepted set |
| Required keys | HIGH | Keys accessed via direct `config[K]` or `config.get(K)` without default — `KeyError` at run-time if absent |
| `snakemake.params.X` refs | LOW | Informational — some come from `params:` blocks not config |
| Rules without `resources:` block | LOW | Informational — these inherit global `mem_mb=3000` and may OOM at scale |
| Channel names in `screen.yaml` | HIGH | Phenotype needs a `DAPI` channel and no `_` in names (aggregate splits combos on it); four SBS spot channels must be named G/T/A/C; DAPI first when its cycle differs from the background channel's |

## Steps

1. **Detect screen repo** (look for `screen.yaml`/`brieflow/`/`config/config.yml`).
2. **Run validator** under the screen's conda env:
   ```bash
   # SCRIPTS_DIR = the plugin's scripts/ dir; resolve from this skill's base dir
   # (printed as "Base directory for this skill:" at invocation):
   SCRIPTS_DIR=$(realpath "<skill-base-dir>/../../scripts")
   $SCRIPTS_DIR/brieflow_preflight.py --analysis-dir <abs-analysis-dir>
   ```
3. **HIGH severity** → exit 1 → show the report. The suggestion field tells
   the operator exactly what to change (file + line in brieflow source).
4. **LOW only** → exit 0 → show the report; run is safe to launch.
5. **Re-run after fix** — idempotent.

## Output modes

- Default human-readable, grouped by severity.
- `--json` for downstream tooling / `brieflow-issue` attachments.
- `--severity-threshold MEDIUM` to make MEDIUM non-zero-exit (default HIGH).

## Plugin script

`<plugin>/scripts/brieflow_preflight.py` — invoke with absolute path.

## False positives

- **Accepted-value guards**: never false-positive — only flags what brieflow
  author actively guards against.
- **Required-key checks**: occasionally trip on keys set in a rule's `params:`
  block from a non-config source. Apply judgment.

## Workarounds are findings

Every workaround made in place (a `screen.yaml` edit, an input symlink tree, a resource
stopgap, a sticky interview-row rewrite) and every defect you cannot fix is a finding:
call `record_finding()` from `scripts/brieflow_findings.py` before continuing (router rule
R5).

## Exit

Return to the router skill (`brieflow-auto`) when done. If preflight failed,
the operator typically needs `brieflow-notebook` (re-configure) or
manual config edit.

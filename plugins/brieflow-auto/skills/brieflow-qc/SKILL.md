---
name: brieflow-qc
description: Use to consolidate and gate a brieflow phase's QC after it runs — reads the phase's eval/ outputs, thresholds them against qc_gates.yaml, returns pass/fail (+ optional HTML report). The same gate the runner uses, run standalone. Triggered by "QC", "did the phase pass", "check sbs quality", "gate this phase", etc.
---

# Brieflow — Post-phase QC gate

Consolidates one phase's QC from the well-defined `eval/` outputs the brieflow
library already wrote, thresholds each metric against `qc_gates.yaml`, and
reduces it to an advance/escalate verdict. This is the **qc module** of the
per-phase cycle (`notebook → preflight → run → qc`) — the identical gate both
modes use: the runner calls `build_phase_qc` / `qc_gate_verdict` in-process; the
operator runs it standalone via this skill.

**Reads eval outputs rigidly — never recomputes a metric in-plugin.** If a
metric is missing, that's an extractor/eval gap, not something to fabricate.

## Inputs

- Screen repo in cwd or parents (`screen.yaml` / `brieflow/` / `config/config.yml`).
  Analysis dir holds `brieflow_output/<phase>/eval/`.
- `--phase` — one of `preprocess sbs phenotype merge aggregate cluster`, or `all`.

## Steps

1. **Detect screen repo** and resolve the absolute analysis dir.
2. **Run the gate** under the screen's conda env:
   ```bash
   # SCRIPTS_DIR = the plugin's scripts/ dir; resolve from this skill's base dir
   # (printed as "Base directory for this skill:" at invocation):
   SCRIPTS_DIR=$(realpath "<skill-base-dir>/../../scripts")
   $SCRIPTS_DIR/brieflow_qc.py --analysis-dir <abs-analysis-dir> --phase <phase>
   ```
   Add `--html` to also write `<phase>/eval/qc_report.html`, `--json` for a
   machine-readable verdict, `--warn-is-fail` to make warns block.
3. **Exit 0** = gate passed (advance). **Exit 1** = a metric failed its gate —
   show the failing metrics + rationale; do not advance to the next phase.
4. The run also writes `<phase>/eval/qc_summary.json` (the persisted summary).

## What it gates

Per-phase metric→threshold pairs live in `qc_gates.yaml` (the QC foundation —
referenced, not duplicated here). Cluster's `genes_per_cluster` uses the
`cluster_selection` warn-band from `lab_defaults.yaml`. Phenotype retention gates scale with
the screen's expected tile-edge loss (`phenotype.edge_reference` in `qc_gates.yaml`; measured
`cell_diameter` and tile size from `config/config.yml`); the scaled gate and its factor are in
the metric's threshold.

## Plugin script

`<plugin>/scripts/brieflow_qc.py` — invoke with an absolute analysis dir.

## Workarounds are findings

Every workaround made in place (a `screen.yaml` edit, an input symlink tree, a resource
stopgap, a sticky interview-row rewrite) and every defect you cannot fix is a finding:
call `record_finding()` from `scripts/brieflow_findings.py` before continuing (router rule
R5).

## Exit

Return to the router skill (`brieflow-auto`). If the gate failed, surface the
failing metrics so the operator can retune the phase notebook (`brieflow-notebook`).

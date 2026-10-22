---
description: Consolidate + gate a phase's QC from its eval/ outputs. Reads well-defined eval files, applies qc_gates.yaml, prints a verdict, optionally writes an HTML report.
---

# /brieflow-qc

Activates the `brieflow-qc` skill. The post-phase QC module — the same gate both run modes use (the runner calls it in-process; the operator runs it standalone here). Reads the phase's `eval/` outputs, thresholds them against `qc_gates.yaml`, and returns pass/fail. Never recomputes metrics.

See `skills/brieflow-qc/SKILL.md` for the full protocol.

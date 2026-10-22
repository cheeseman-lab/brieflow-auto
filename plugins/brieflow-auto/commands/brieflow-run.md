---
description: Run a screen through the per-phase cycle (notebook → preflight → run → QC) over any range of phases — whole screen, --only one phase, or --from/--through — on slurm, local or a cloud VM, pausing only on a classified blocker.
---

# /brieflow-run

Activates the `brieflow-run` skill. One runner for every screen run: resolves the
interview, writes `.brieflow/launch_review.html` for the operator to read, and after
`--approved` loops the same cycle for each phase in range. Pauses and notifies on a real
blocker (template gap, preflight failure, QC-gate failure, run failure after recovery,
upstream-rerun guard, disk shortfall, notebook failure); a blocker inside the phase loop
leaves `.brieflow/issue_draft.md` as evidence for `/brieflow-issue` (a pre-launch gap does not). The agent invokes that skill automatically for an
identified pipeline defect, opening an issue with a backward-compatible fix proposal.

```bash
bin/brieflow-run --analysis-dir <dir>                              # whole screen
bin/brieflow-run --analysis-dir <dir> --only sbs                   # one phase, full cycle
bin/brieflow-run --analysis-dir <dir> --through merge --backend local --lifecycle-hooks gcs --screen-id <id>
```

Preconditions: `screen.yaml` authored and `pipeline.conda_env` set (run `/brieflow-setup`
first). See `skills/brieflow-run/SKILL.md` for the protocol.

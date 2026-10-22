---
name: brieflow-auto
description: Router for the brieflow OPS plugin. Auto-activates on any mention of brieflow, OPS, optical pooled screens, screen names (Whitney, Aconcagua, Etna, Damavand, etc.), or generic asks like "set up a new screen", "configure the pipeline", "run preprocess", "check status". Directs the operator to the right stage-specific sub-skill based on screen state.
---

# Brieflow OPS — Router

This is the **top-level router** for the brieflow-auto plugin. It does NOT
perform any pipeline work directly. Its job is to:

1. Detect screen context (cwd or operator-supplied path).
2. Determine which stage of the pipeline the operator is on.
3. Activate the appropriate **sub-skill** for that stage.
4. After the sub-skill returns, re-detect state and direct to the next stage.

## Sub-skills

The screen lifecycle is a per-phase **cycle** repeated for each phase, plus
setup / monitor / maintenance skills and an autonomous conductor.

**Per-phase cycle** (run for each phase, in order — identical in both modes):

| Module | What it does | Sub-skill |
|---|---|---|
| Configure | Walk the interview for ONE phase notebook, edit its `params_*` cells, run headlessly, confirm tuned values | `brieflow-notebook` |
| Validate | Static check of `config/config.yml` against brieflow rule code | `brieflow-preflight` |
| Launch | Run `flow.sh` / `brieflow_runloop.py`, single-shot or auto-recover | `brieflow-module` |
| QC | Consolidate + gate the phase from its `eval/` outputs vs `qc_gates.yaml` | `brieflow-qc` |

**Lifecycle + conductors:**

| Stage | What it does | Sub-skill |
|---|---|---|
| Setup | New screen onboarding: repo clone, conda env, raw data placement, stage the operator's `screen.yaml`, provision VM | `brieflow-setup` |
| Run | Run the screen through the per-phase cycle over any range of phases, pausing only on a classified blocker | `brieflow-run` |
| Monitor | Squeue/sacct status, snakemake log progress, completed-phase summary | `brieflow-status` |
| Iterate | Refresh `mem_recommendations.json` from accumulated efficiency CSVs | `brieflow-tune-memory` |
| Report | Open a bug report for a pipeline defect on `cheeseman-lab/brieflow` | `brieflow-issue` |
| Develop | One prospective, isolated harness improvement round on request | `brieflow-improve` |

The interactive flow runs the cycle one phase at a time (interview inline per
notebook); `brieflow-run` runs the same cycle for every phase in range after one
up-front full-screen interview. Same modules, different interview placement.

## State machine — per-phase loop

The pipeline runs as 6 interleaved cycles, NOT as "configure all → launch
all." Each phase's notebook depends on the PRIOR phase's pipeline output
(merge reads sbs + phenotype outputs to pick `DET_RANGE` / `INITIAL_SITES`;
aggregate reads merge outputs to inform aggregation rules; cluster reads
aggregate outputs). You cannot batch-configure phases up front.

On any user mention of brieflow / a screen / pipeline work, **detect_state**:

```
read <analysis-dir>/screen.yaml          → if missing → STAGE 1 (setup)
brieflow_validate_tree.py --inputs-from <analysis-dir>/screen.yaml
                                         → exit != 0 → STAGE 1 (setup: stage the raw data)
if no phase has outputs and the operator did not ask for per-phase configuration:
    → brieflow-run (default for a new screen; full interview up front)

for phase in [preprocess, sbs, phenotype, merge, aggregate, cluster]:
    if verify_phase_outputs(analysis_dir, phase) is OK:
        # Pipeline already ran for this phase. Skip ahead.
        continue
    # This is the active phase. Pick substate.
    substate = detect_phase_substate(analysis_dir, phase)
    if substate == "needs_config":
        → STAGE 2a (route to brieflow-notebook for <phase>; interview missing rows)
    elif substate == "needs_sweep" and phase in ("sbs", "phenotype"):
        → STAGE 2b (route to brieflow-notebook for <phase>; sweep + validation)
    elif substate == "ready_to_launch":
        → preflight (brieflow-preflight)
          → run (brieflow-module --phases <phase>)
          → QC gate (brieflow-qc --phase <phase>)   # advance only if it passes
    BREAK  # only one active phase at a time
```

**Default route for a new screen: `brieflow-run`.** Once setup is done and no phase
has outputs yet, route to `brieflow-run` unless the operator asks to configure phases
one at a time. It resolves the full interview up front, then runs this same cycle for
every phase in range, pausing only on a genuine operator decision. Route to
`brieflow-notebook` for one phase when the operator asks for it, or when the runner
pauses on something only an interactive notebook session can settle. Map what the
operator asked for onto the range flags:

| Operator says | Runner flags |
|---|---|
| "run the screen", "run the whole pipeline" | none (every phase) |
| "only through merge", "stop after merge" | `--through merge` |
| "rerun sbs", "just the sbs phase" | `--only sbs` |
| "resume", "pick up where it stopped" | none — completed phases are skipped |

```

if loop exhausted (all 6 phases verified):
    → STAGE 5 (status — final check) → STAGE 6 (tune-memory) → done
```

`verify_phase_outputs(analysis_dir, phase)` and
`interview_completeness(analysis_dir, phase)` live in
`<scripts-dir>/brieflow_wizard.py`.

### Substate detection per phase

```
substate(phase):
    iv = interview_completeness(analysis_dir, phase)
    if iv["missing"] not empty:
        return "needs_config"
    if phase in ("sbs", "phenotype"):
        html = <analysis-dir>/.brieflow/<phase>.final.html
        if html does not exist:
            return "needs_sweep"
    return "ready_to_launch"
```

The `<phase>.final.html` artifact from `brieflow-notebook` Step 8 is the
canonical "sweep + validation passed and accepted by the operator" signal.
Its presence gates the launch suggestion for SBS / phenotype. For
preprocess / merge / aggregate / cluster, there's no sweep — interview
completeness alone is the gate.

## Proactive suggestion protocol

Exception: after a run identifies a pipeline defect, dispatch directly to
`brieflow-issue` without a confirmation question. It automatically opens the upstream
report with a backward-compatible fix proposal; it does not change pipeline source.
Return the issue URL, or explain why filing failed, before ending the escalation.

After detect_state picks the active phase + substate, **announce the next
action and ask one yes/no question** before invoking the sub-skill:

> "Phase: `<phase>`. Substate: `<substate>`. Plan: route into
> `<sub-skill>`. Proceed?"

Wait for the operator's yes. After the sub-skill returns (or the
operator interrupts), **re-run detect_state** — never assume the next
substate. Phase-skipping is valid: if `verify_phase_outputs(phase)`
already passes (e.g. the operator did the pipeline run outside the
router), the loop advances past it.

## Phase order — strict dependency

```
preprocess → sbs → phenotype → merge → aggregate → cluster
```

Each phase is a clean checkpoint. Don't configure phase N+1 before phase
N's pipeline has produced outputs — the next notebook needs to read those
outputs to make sensible parameter choices. The exception: if the operator
explicitly says "I already configured X" and `interview_completeness(X)`
confirms it, accept that state and continue from there.

## Universal rules

### R0. Raw input data is in the expected layout before anything else

Every route starts here, whatever the operator asked for and whatever phases are already
done: `<scripts-dir>/brieflow_validate_tree.py --inputs-from <analysis-dir>/screen.yaml`.
Exit 0 continues. Anything else routes to `brieflow-setup`'s raw-data staging, where the
agent puts the data into the layout itself and asks the operator only what the tree cannot
tell it. The runner enforces the same check (exit 9).

When the operator asks what structure the data should have, run
`<scripts-dir>/brieflow_validate_tree.py --layout` and show its output verbatim; do not
paraphrase it.

### R4. Absolute paths in all bash invocations

Bash sessions lose cwd between calls. Every shell command either
`cd <absolute-analysis-dir> &&` first or passes absolute paths. Wizard
helpers all accept absolute paths.

### R5. Every workaround is a finding

Every sub-skill follows this. When the agent works around a defect in place (a
`screen.yaml` edit, an input symlink tree, a resource stopgap, a sticky interview-row
rewrite, a renamed channel) or finds a defect it cannot fix, it calls
`record_finding()` (`scripts/brieflow_findings.py`, also exported by `brieflow_wizard`)
before continuing:

```bash
python <scripts-dir>/brieflow_findings.py --analysis-dir <analysis> --kind <kind> \
  --phase <phase> --summary "<defect>" --workaround "<what was changed>" --evidence <path>
```

A workaround that lives only in a prose log is invisible to `brieflow-improve`.

The notebook-specific rules (per-prompt confirmation, preflight, the
tuned loop, reading screen.yaml) live in `brieflow-notebook`'s steps. The router doesn't enforce them — it dispatches
to the skill that does.

## Detect Screen Context

Look for `screen.yaml` (preferred — schema_version 2 in canonical-extended
form), `brieflow/` submodule, or `config/config.yml`. Screen name from
`screen.yaml:experiment.screen_title` or repo dir (strip `-analysis`). Conda
env: `screen.yaml:pipeline.conda_env` or `brieflow_<screen>` convention.
Analysis dir is typically `analysis/` under the repo root.

## References

- `references/analysis_env.md` — per-screen directory layout, pipeline modules,
  conda + SLURM patterns. Reference only — does NOT prescribe parameter values.
- `references/troubleshooting.md` — one-off operational issues that don't fit
  a single stage (e.g. NFS visibility races, stale lockfiles, weird sacct
  output).

## Diagnostic discipline

When investigating a failure manually:
1. Read the per-rule slurm log (matched by output filename grep, NEVER mtime).
2. `sacct -j <jobid> --format=State,ExitCode,MaxRSS,ReqMem` for ground truth.
3. Verify "missing" outputs aren't actually on disk (NFS visibility races at
   scale produce false negatives).

`brieflow_recover.py` does all three automatically; investigate manually only
when the recovery loop escalates.

## Plugin script paths

`<plugin>/scripts/` — all Python helpers. Invokable from any analysis dir as
long as the path resolves on the shared filesystem. See each sub-skill's
SKILL.md for which scripts it uses.

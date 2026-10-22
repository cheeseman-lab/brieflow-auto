---
name: brieflow-module
description: Use to launch a brieflow pipeline run on slurm or cloud, with optional autonomous OOM recovery. Single-shot (interactive tmux) or auto-recover (synchronous, detects OOM → bumps mem → resumes, capped at N attempts). Triggered by "run the pipeline", "launch brieflow", "run preprocess and sbs", "complete the pipeline overnight", etc.
---

# Brieflow — Launch a pipeline run

Run `flow.sh` (or `brieflow_runloop.py` for phased orchestration) against the
current screen, with optional autonomous OOM recovery.

## Modes

| Mode | When | What |
|---|---|---|
| **single-shot** (default for interactive) | Operator is at the keyboard and wants to watch | Launches one `flow.sh` invocation in tmux; operator monitors live |
| **auto-recover** (default for "complete the pipeline" / autonomous) | Operator says "run X to completion", "overnight", "all the way through" | Runs synchronously; on failure, classifies via `brieflow_recover.py`, bumps mem in `mem_recommendations.json`, resumes; capped at N attempts |

When in doubt: ask "Auto-recover on OOM (cap at 3 attempts), or single-shot in tmux?"

## Steps

Resolve `$SCRIPTS_DIR` once, before step 4 (printed as "Base directory for
this skill:" at invocation):
```bash
SCRIPTS_DIR=$(realpath "<skill-base-dir>/../../scripts")
```

1. **Detect screen repo** (as in brieflow-status step 1). Derive
   `<screen_id>`, `<conda_env>=brieflow_<screen_id>`, `<analysis_dir>`.
2. **Decide modules.** Use explicit if provided; else infer next phase from
   output-dir state.
3. **Decide mode** (above).
4. **Run preflight** (always, both modes):
   `$SCRIPTS_DIR/brieflow_preflight.py --analysis-dir <abs>`
   - HIGH severity exit 1 → abort with the report; suggest fix from issue.
   - LOW only → continue.
5. **Aggregate efficiency** (skip if no prior logs):
   `$SCRIPTS_DIR/brieflow_aggregate_efficiency.py --analysis-dir <abs> --quiet`
6. **Single-shot mode**: tmux session `<screen>_analysis`, confirm with
   operator before launching.
7. **Auto-recover mode**: synchronous loop, max-attempts=3 default:
   - Launch via `brieflow_run.py` or phased via `brieflow_runloop.py`
   - On non-zero: invoke `brieflow_recover.py` for classification
   - Action = success → break; resume → relaunch; escalate → notify + exit nonzero
   - See `references/recovery_runbook.md` for the full FSM.
8. **On success**: refresh memory recs + notify operator.

## Single-phase invocation (the default pattern)

Run ONE phase per invocation. Each phase's notebook configuration depends
on the PRIOR phase's pipeline outputs (merge reads sbs + phenotype outputs;
aggregate reads merge outputs; cluster reads aggregate outputs). Bundling
downstream phases — even small ones like merge + aggregate + cluster — is
wrong because the operator can't configure the next notebook until the
prior pipeline's outputs land.

The router (`brieflow-auto`) drives the interleaving: it walks
configure → launch → wait per phase, one at a time. This skill's job is
just to launch the requested single phase.

```bash
$SCRIPTS_DIR/brieflow_runloop.py \
  --analysis-dir <abs> \
  --phases <phase>                                # one of: preprocess, sbs,
                                                  # phenotype, merge,
                                                  # aggregate, cluster
  --config config/config.yml --backend slurm \
  --jobs 400 --max-attempts 3 \
  --tag <screen>_<phase>_<timestamp>
```

Latency wait defaults to 5 s; `brieflow_recover.py` caps bumps at 10 s
(`LATENCY_BUMP_LADDER`).

> **Backend is a flag, never a code edit.** `flow.sh` handles both backends
> itself: `--backend local` (one big VM/GCS — snakemake `--cores`/`--jobs` with
> an auto memory budget) and `--backend slurm` (cluster — `--executor slurm` +
> `--jobs` cap). Change concurrency with `--cores N` (local) or `--jobs N`
> (slurm); an explicit `--jobs` always wins and, on slurm, matches the profile's
> jobs cap. **Do NOT hand-edit `flow.sh` to adapt it to the backend at hand** —
> both paths already work, and patching the script per-run is exactly how it
> drifts. If a backend genuinely misbehaves, fix `flow.sh` upstream in brieflow-analysis
> (and `brieflow_run.py`) and release it — never patch the checked-out copy.

After completion, the router re-detects screen state and suggests the next
phase's notebook (via `brieflow-notebook`). Don't loop multi-phase
inside this skill; return control to the router.

## More than one phase

Anything beyond a single raw Snakemake launch — the notebook, preflight and QC steps, or
several phases in order — goes through the runner: `bin/brieflow-run --only <phase>` for
one phase with the full cycle, `bin/brieflow-run --from <a> --through <b>` for a range.
This command stays the escape hatch for launching one module's Snakemake run by hand.

## Pipeline order

```
preprocess → sbs → phenotype → merge → aggregate → cluster
```

Sequential per `flow.sh` (`all_<module>` markers gate phase transitions).
**`classify` is NOT a module** — it's a config section read by `aggregate`
rules. Valid modules: `preprocess sbs phenotype merge aggregate cluster
mozzarellm viz all`.

## Hard rules

- **NEVER skip preflight.** HIGH severity issue → guaranteed failure.
- **NEVER `--use-arrays` at well/full scale.** Array wildcard-collision bug in
  `snakemake-executor-plugin-slurm` 2.6.x.
- **Auto-recover resumes on `recoverable_oom` (mem bump), `nfs_latency` (latency
  bump, capped) and `recoverable_fd_limit` (raise nofile); it escalates on
  `config_mismatch` and `unknown`.** Numbers live in `scripts/brieflow_recover.py`,
  not here.
- **Cap at 3 attempts** by default; `--max-attempts N` to override.
- **Single-shot requires confirmation**; auto-recover does not (autonomous by
  design) but shows a summary at end.

## Workarounds are findings

Every workaround made in place (a `screen.yaml` edit, an input symlink tree, a resource
stopgap, a sticky interview-row rewrite) and every defect you cannot fix is a finding:
call `record_finding()` from `scripts/brieflow_findings.py` before continuing (router rule
R5).

## References

- `references/recovery_runbook.md` — full failure-classification FSM + the
  2026-04-30 well-scale recovery pattern this skill encodes.

## Plugin scripts

`<plugin>/scripts/brieflow_run.py`, `brieflow_runloop.py`, `brieflow_recover.py`,
`brieflow_preflight.py`, `brieflow_aggregate_efficiency.py`, `brieflow_notify.sh`.

## Exit

Return to router (`brieflow-auto`). If investigation identifies a pipeline defect,
invoke `brieflow-issue` automatically and return the issue URL or filing blocker.
The issue must propose a backward-compatible fix, not a screen-specific pipeline rewrite.

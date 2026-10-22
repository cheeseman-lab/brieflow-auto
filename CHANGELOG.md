# Changelog

## Unreleased

SBS QC reads the called-base mix of a combinatorial `frac` run (`brieflow_base_composition.py`,
wired into `brieflow_qc.extract_sbs_qc`). On a three-dye code with G blank, a cycle with all
three dyes on is equally near C and A, and without `combinatorial.ambiguous_call: N` brieflow
calls it C, so the plate reads C-rich with no error.

- **New SBS gates** (`data/qc_gates.yaml`): `all_on_unresolved_rate` (bright cycles with every
  code dye on, when `ambiguous_call` is not `N`; fail above 0.10), `base_excess_max` (largest
  called share over the share the barcode library expects at that cycle; warn above 0.15, fail
  above 0.30) and `ambiguous_n_inert` (fails when `ambiguous_call: N` is configured but no read
  carries an N — the installed brieflow is not the one the config was written for).
  `n_call_rate` and `all_on_rate` are reported ungated.
- **Measured** on one plate set before the brieflow N fix: 24-42% of bright cycles all-on, C
  +0.18 to +0.34 over the library share; after it, all-on cycles called N and the largest
  excess 0.14. Reads come from a sample of `reads.parquet`, the all-on rate from a sample of
  per-tile `bases.tsv` under the caller's own blank and fraction rules; reads with no signal
  are left out.
- **Interview rows** for the SBS notebook's `AMBIGUOUS_CALL` (lab default `"N"`, frac only),
  `N_WILDCARD` (1, frac only) and `RECOMB_MAX_N` (2, multi only), with their `lab_defaults.yaml`
  values and spec-CSV rows, so the notebook-parameter coverage check passes on the notebook that
  adds them (brieflow-analysis #130).
- **Chance-level floor** on `single_gene_map_rate` (fail below 0.02, warn below 0.05). The gated
  `single_map_rate` counts cells with one dominant read whether or not it is in the library, so a
  plate decoding at chance passed it (0.339 single-barcode, 0.001 gene). Ten combinatorial plates
  run 0.107-0.489, the screen behind the 2026-08-28 call 0.164; only the chance-level plate fails.

## 0.5.43 — 2026-10-22

First public release, under the MIT license (the same as brieflow). Site-specific paths and
unpublished screen names are gone from the code, tests and docs; screens are named by their
public names. A test fails if any of these come back. Plugin metadata names the author without
an email.

## 0.5.42 — 2026-10-19

The layout check's blocker header no longer says "manual fix required": blockers are not
auto-reshaped, but the agent resolves each one with the operator (brieflow-setup "Raw data
staging"). brieflow #333 merged, so brieflow's "Before You Screen" page now shows the same
layout.

## 0.5.41 — 2026-10-19

The raw-data layout is a stated contract, checked before anything runs, and staging data into
it is the agent's job.

- **One layout, printable.** `brieflow_validate_tree.py --layout` prints the structure brieflow
  reads: `input_sbs/plate_<N>/c<M>/` and `input_ph/plate_<N>/round_<R>/`, with the rules for
  one acquisition per directory and for multi-round phenotype data. The skills show this
  output when an operator asks what the data should look like. brieflow's "Before You
  Screen" page gets the same layout (brieflow #333); it showed a plate-major tree.
- **Multi-round checks.** New blockers: two acquisitions in one cycle or round (the same file
  names in two subfolders, e.g. two Phenix Measurements), an empty cycle or round directory,
  and plates with different phenotype rounds. Rounds holding different numbers of image files
  need the operator's confirmation (exit 3). `.ims` counts as an image file.
- **Enforced on every route.** The router checks the input directories screen.yaml names
  (`--inputs-from`) before routing anywhere and sends a failing tree to setup's raw-data
  staging. The runner checks them on every launch, and after the GCS pull when a lifecycle
  hook pulls them, and stops with exit 9; `--accept-layout` proceeds past confirmed warnings
  and is journaled.
- **Staging is the agent's job.** Setup's "Raw data staging" lists, per blocker, what the agent
  does and the one question it asks. The cloud walkthrough stated the old plate-major layout;
  fixed.
- The duplicate and unseparated checks read only what they need, so the check takes seconds
  on production trees.

## 0.5.40 — 2026-10-16

Matches brieflow `zarr3` #297, #288 and #332 and brieflow-analysis `marimo` #129.

- **`ZARR_MAX_LEVELS`** (preprocess notebook, `all.zarr_max_levels`): pyramid levels for each
  converted raw image in zarr mode, lab default `1` (no pyramid).
- **Notebook QC fails closed.** brieflow now prints the alignment and Segmentation QC blocks
  only when the notebook passes `verbose=True`. `parse_qc_block` used to count a run with no
  QC block as a pass; it now sets `qc_missing` and fails when no QC metric is printed or
  inherited. The info-only optics metric alone does not count.

## 0.5.39 — 2026-10-16

`scripts/install_plugin.sh --git --ref <tag>` now fetches and checks out the tag when the
marketplace clone already exists. It used to skip git entirely and install whatever the clone held,
so every upgrade after the first install silently stayed on the old version. A clone with local
changes stops the install instead of being overwritten.

## 0.5.38 — 2026-10-16

Follows brieflow `zarr3` #329, where the bootstrap runs one job per gene and writes no null
distributions: the rules `bootstrap_construct` and `construct_bootstrap_complete` no longer exist,
so they leave the dry-run preview, the efficiency aggregator and the memory baselines.
`bootstrap_gene` keeps its 8000 MB baseline until a production-size run is observed. Tested against
the `marimo` template at brieflow `e794f46`.

## 0.5.37 — 2026-10-16

- **Raw data must arrive one directory per cycle and per round.** `brieflow_validate_tree.py`
  stops with `cycle-unseparated` / `round-unseparated` when images are not split into
  `c<M>/` and `round_<M>/` directories (for example several cycles in one folder, told apart
  only by file name), and does so before any move, so a blocked tree is left untouched.
  Previously loose images from several cycles were wrapped together into `c1/` and reported as
  canonical. Setup on an existing host now runs the same check. The preprocess probe no longer
  guesses a path pattern for SBS data without cycle directories; it says how to stage the data.
- **Merge gate under partial phenotype coverage.** When the phenotype tiles cover only part of
  each SBS field, SBS recovery cannot exceed about `ph_cells / sbs_cells`, so a correct merge
  failed the 0.50 minimum (0.23 on the small test). The gate now reads `recovery_of_reachable`,
  matched cells over `min(ph_cells, sbs_cells)` per well from the merge summary;
  `sbs_recovery_rate` is still reported.

## 0.5.36 — 2026-10-16

Matches the template as merged on brieflow-analysis `marimo` (#125, #127): the watershed
thresholds (`THRESHOLD_DAPI`, `THRESHOLD_CELL`, `NUCLEUS_AREA`) depend on the camera, so they are
asked when watershed segmentation is chosen instead of filled from lab defaults. Tested against the
maintained `marimo` clone.

## 0.5.35 — 2026-10-16

Matches the release template (brieflow-analysis #127): notebooks numbered 0–7 with operator values
in `params_<topic>` cells. Tested end to end by setting up and running the small test screen from a
fresh clone of that template.

- **Parameter cells.** The marker scan treats a `params_*` cell's assignments like a
  comment-fenced block, so older screens keep working. `phase_notebook()` finds a phase's notebook
  by name whatever its number. Tuned values are read back from `config.yml` when no tuned export
  file exists. `tools/compare_notebooks.py` checks that a restructured notebook defines what the
  original did.
- **Fewer questions for a new operator.** Segmentation method is the lab default cellpose;
  `MIN_CELL_CUTOFFS` the lab default `{all: 0}`; `CHANNEL_COMBOS` one aggregation of every phenotype
  channel; the SBS DAPI cycle defaults to 1 and, on a DNA screen, the cell background to the first
  base channel. `ALIGN` comes from screen.yaml `phenotype.channels_imaged_together`; several
  phenotype rounds imply alignment, otherwise it is asked.
- **Interviews follow plugin upgrades.** An interview built by an older plugin adopts the current
  template's row definitions and keeps its answers.
- **Fixes found by the run:** a single phenotype round is probed as round order `None`, as the
  notebook expects; values resolved to `None` are written into the notebook; the test-tile picker
  handles wells too small for an interior ring; the `DET_RANGE` pixel size is the median over files
  spread across the data, so one tile with bad metadata can't set it; the standardized barcode
  library never shares a path with the raw design table.

## 0.5.34 — 2026-10-15

When screen.yaml names a per-well table (`cellular.well_annotations`), the table decides which wells
run and how they are grouped.

- **Wells:** the preprocess notebook's new `WELL_ANNOTATIONS_FP` is filled from
  `cellular.well_annotations`, so the sample tables, and every later module, keep only the listed
  wells (brieflow-analysis #125). Before, every imaged well was processed and the table trimmed
  wells only at aggregate.
- **Grouping:** new `cellular.group_by` sets `GROUP_COLS`, and `cellular.reference_group` sets
  `BOOTSTRAP_CONTROL_SCOPE` and `CONTROL_SCOPE` to `within_perturbation` with that value as both
  reference groups (each perturbation scored against its own vehicle arm). `cellular.split_by`
  keeps setting `SPLIT_COL`. A vehicle-referenced screen whose design was hand-set per screen now
  derives the same five values from screen.yaml.
- Preflight checks that `group_by` names a table column, that `reference_group` comes with
  `group_by` and is a value of that column, and that both need `cellular.well_annotations`.
- The aggregate copy of the table also drops rows with no `group_by` value.

## 0.5.33 — 2026-10-15

The operator writes `screen.yaml`; setup stages it and the runner checks it.

- `scripts/brieflow_screen_yaml.py` holds the screen.yaml contract: the keys the plugin reads, the
  descriptive keys, and the experimental facts an operator must supply (`REQUIRED_KEYS`, plus the
  barcode columns for the library type and the combinatorial code for two-color chemistry). Its
  `check`, `find` and `stage` commands report blank fields, find a filled-in screen.yaml the operator
  brought, and copy it into `analysis/` with the well-annotations and design tables it names.
- `/brieflow-setup` stages the operator's screen.yaml and stops, listing the blank fields, when only
  the template is there. It no longer interviews the operator to write it. It sets only the host
  facts it created (`pipeline.conda_env`, `data.input_sbs_dir`, `data.input_phenotype_dir`).
- The runner checks screen.yaml before anything else and exits 3 naming every blank required field.
- The interview re-derives the rows screen.yaml supplies whenever screen.yaml changes (it stores
  the file's sha), keeping values from the operator, probes and sweeps. Before, a screen.yaml
  replaced after the first run never reached `interview.json`.
- Preflight raises `sort-calls-not-peak-for-dna` (HIGH) when `library.detection_target` is DNA and
  `sbs.sort_calls` is not `peak`. `library.detection_target` is now a required fact, so it is never
  defaulted.
- Preflight's DAPI-first check reads the SBS DAPI cycle from `iss.background_channel_nucleus.cycle`,
  the field the notebook uses, instead of `iss.dapi_cycle`, which the template never had.
- `tests/test_screen_yaml_contract.py` holds the canonical template equal to the contract and checks
  that every key the plugin reads has a comment there (brieflow-analysis #125).
- `brieflow-run` documents reruns.

## 0.5.32 — 2026-10-06

A completed phase whose upstream phase finished later now reruns when Snakemake has nothing
pending. Brieflow reads some upstream outputs through `ancient()` (cluster's `clean_aggregate`,
aggregate's `split_datasets`), so a newer upstream never triggers them; the runner passed such a
phase with 0 jobs and gated QC on the old outputs. `phase_rerun_check` now returns those rules,
and the runner adds them to `--forcerun` for that phase only.

## 0.5.31 — 2026-10-06

`--forcerun` reaches only the phases whose `.smk` defines the rule. A run with `--from aggregate
--forcerun split_datasets` passed the rule to the cluster phase too, where Snakemake fails with
"No rule to produce split_datasets".

## 0.5.30 — 2026-10-06

A failed attempt is no longer reported as complete because an older Snakemake log still holds the
module's terminal marker. `brieflow_recover.py` takes `--since` (the attempt's start time, passed
by the runloop) and ignores marker logs written before it. Before this fix, a re-aggregation whose
every Snakemake call crashed while building the job graph was marked done and gated on the
previous run's outputs. The Stop hook (`brieflow_rollout_hook.py`) also runs on Python 3.8.

## 0.5.29 — 2026-10-06

Preflight fixes for two false HIGH findings that stopped an aggregate launch. The check for "_" in
phenotype channel names (`phenotype-channel-name-underscore`) now fires only when the screen's
brieflow checkout still splits channel combos on "_"; brieflow#314 fixed that on zarr3. The
`mode` argument guard in `lib/shared/cellpose_training.py` is a function argument, not a config
key, and is no longer matched against the mozzarellm `mode` setting.

## 0.5.28 — 2026-10-06

Sync with the brieflow merge and alignment-view changes. The merge interview drops the stitch
approach (`STITCH`, `MASK_TYPE`, `STITCHED_IMAGE`, `NUM_TILES_PHENO`, `NUM_TILES_SBS`) and adds
`MERGE_APPROACH` ("fast" | "positions", written to `merge.approach`). It is derived, not asked.
It is "positions" when the measured median phenotype cells per tile is below
`merge_approach.max_cells_per_tile` (60 in `lab_defaults.yaml`; fast failed at 18-29 cells per
tile and worked at about 130). The runner measures this at launch when phenotype outputs exist,
and again when merge starts. Before phenotype outputs exist, it is "positions" when the phenotype
objective in `screen.yaml` is at least `merge_approach.min_objective_x` (40x); with neither, it is
"fast". The launch review's derived-decisions section shows the source and the reason. An
operator value is never replaced; a change at merge time lab-fills the rows it activates and is
journaled as `merge-approach`.

`FLIPUD`, `FLIPLR` and `ROT90` apply when `MERGE_APPROACH` is "positions";
`INITIAL_SITES_APPROACH`, `DET_RANGE` and the fast-merge levers (`LOCAL_REFINEMENT`, `WARP_*`,
`SEED_*`, `THRESHOLD_TRIANGLE`) apply when it is "fast". On "positions" the runner neither
probes `INITIAL_SBS_TILES` nor pauses on its absence. Merge QC on "positions" reads the same
`eval_merge` summary and gates as on "fast"; the per-well positions tables are not gated.
`positions_merge` gets a baseline memory cap (2 GB per well without image QC), and
`config_tiers.yaml` lists `merge.approach` in {fast, positions} and the positions keys,
including `positions_image_qc` (off by default).

The SBS preview rows `VIZ_CYCLE`, `DAPI_REFERENCE_CYCLE` and `VIZ_CHANNELS` (and its computed
derivation) are removed; the notebooks' display selectors replace them and need no rows.
`/brieflow-notebook` Step 6a' and `/brieflow-run` require the agent to render every alignment
view (all SBS cycles and base channels, every merge tile-site pair, every positions tile
overlap and phenotype-in-SBS pair) and check that they agree. It records a finding for each
off cycle, channel or pair, and proposes `SKIP_CYCLES` (or the positions best orientation) in
the launch review without applying it.

## 0.5.27 — 2026-10-04

Sweeps no longer run on the host the runner orchestrates from: with `--backend slurm` (and
`sweep_tiles.run_in_allocation`), each sweep runs on a compute node through `srun`
(`scripts/brieflow_sweep_alloc.py`). A sweep stopped by a time limit raises `SweepTimeout`
instead of reporting "no finite mapping rate": the live-kernel helpers used to ignore an
unsettled kernel and record the value as missing. The baseline limit scales with the tile's
image size (15 s per Mpx on 4 cores, x3, floored at `kernel_timeout_s`; one 77 Mpx 4-color SBS
tile primes in about 1,000 s, over the old fixed 1,020 s limit under load). The headless
fallback (`scripts/brieflow_cell_runner.py`) primes each tile once and reruns only the cells
downstream of the changed marker that the metric needs: on 3 tiles of a 4-color SBS screen,
15-23 s per THRESHOLD_READS value and 26-33 s per PEAK_WIDTH value after one priming pass,
against about 980 s per value for the old headless path and 51-88 s for the live kernel, with
identical mapping rates. Refine points are fixed once from the first batch of tiles and probed
on every tile (single-parameter and joint sweeps), so refine rows compare across tiles. When no
tested Leiden resolution reaches the genes/cluster target (a small library), the runner journals
`sweep-target-missed` with the closest resolution and its median instead of a `sweep-winner`.

## 0.5.26 — 2026-10-04

QC acceptance without reruns. `brieflow-run --accept-qc <phase>` journals a `qc-accept` decision
(source operator) and marks a phase done without rerunning it; it needs the phase's finished run
and does not change the launch review, so the same `--approved` command continues. Each finished
phase run leaves `.brieflow/phase_complete/<phase>.json` (tag, time, config digest). Re-entering
such a phase with an unchanged config, and no upstream phase completed since, gates QC on the
existing outputs instead of resubmitting (one screen re-ran 7.4 h of phenotype after a QC pause);
otherwise the runner logs Snakemake's rerun reasons and list-changes counts and journals a
`phase-rerun`. The launch-review hash keeps runner-written rows (auto-probe, tuned-confirmed) by
source only, so a resumed run no longer needs a new review for its own test tiles and sweep
winners. Phenotype retention gates scale with expected tile-edge loss, ((T - d) / T)^2 for
measured cell diameter d and tile size T, relative to the HeLa reference in
`qc_gates.yaml` (`phenotype.edge_reference`); the factor never tightens a gate. Each phase's QC
outcome is kept in `.brieflow/qc_status.json` and the completion message lists warn, advisory
and accepted phases. Escalation drafts carry a `route` and the pause reason, and the runner's
message follows the route instead of always pointing at `/brieflow-issue`. The run manifest
records plugin install health (unreadable registry files, version skew) and the runner warns on
it. `scripts/brieflow_watch.py` is the shipped run watcher (state, new runner events, this
launch's failed jobs; exit codes for a monitor loop).

## 0.5.25 — 2026-10-04

Quick fixes from a large-cell screen (4-color SBS, 5-channel phenotype). Partition and
account reach Slurm as strings: Snakemake summed a numeric `slurm_partition` across a job
group (`-p 60`), so no SBS job was submitted. When every sbatch call is rejected, recovery
escalates at once as `submission_failure` and names the partition, and it classifies at most
20 errors per rule instead of searching logs for each of thousands. `flow.sh` receives
`BRIEFLOW_OPS_PLUGIN_DIR` from the runner instead of globbing the plugin cache. Memory: the
runner writes `.brieflow/geometry.json` at launch and before each phase (a partial probe never
replaces a complete one); per-tile rules written as a flat `mem_mb` now resolve instead of
falling to the 3000 MB default; an OOM bump never lands below the rule's formula estimate;
`bootstrap_gene` rises to 8000 MB and `annotate_cluster_anndata` gets a 64000 MB baseline.
**Illumination-correction caps now scale with `preprocess.sample_fraction`** relative to the
0.5 the formulas were fitted at (`sample_fraction_ref`). At 0.1 the `calculate_ic_*` formula
caps drop to a fifth of 0.5.24's (observed peaks at 0.1 are far below either); the post-OOM
floor at the formula estimate is the backstop. A sweep winner needs at least 50% finite trials
and a finite rate on at least 2 tiles, else the parameter stays unresolved and the phase
pauses. Preflight blocks (HIGH) on dye-named SBS base channels and on `_` in phenotype channel
names (brieflow#314); the preprocess probe routes numeric wells (`Well1_Point1`) to the
operator; DET_RANGE refuses pixel sizes outside 0.05-5 µm; classify runs only when
`RUN_CLASSIFY` is True. The efficiency aggregator counts one observation per Slurm job, not
per job step, and a run stopped by closing its console pipe is traced as `interrupted`.

## 0.5.24 — 2026-10-04

Evidence capture. `brieflow_improve.py collect` finds, next to each `--analysis-dir`, the
runner traces, captured transcripts (up to three parent directories) and `*_LOG.md` operator
logs (`--no-discover` turns this off). Sources over the 2 MB limit are split into chunks
(transcripts, ledgers, records) or excerpted (pipeline logs, keeping sbatch resource flags)
instead of aborting the bundle. Efficiency reports become per-rule summaries, the records list
covers issue drafts, the launch review, interview, tuned values, sweep tables and logs and
memory recommendations, identical contents are stored once, a transcript snapshot that is a
prefix of a later one is skipped, and every file not kept is listed with a reason. Traces keep
the exception message, a traceback tail and the agent session ID, and are linked to the
transcript that covers them. `/brieflow-setup` turns transcript capture on (one privacy
disclosure) and adds the ignore rules; `record_finding()` (`scripts/brieflow_findings.py`) is
required by every acting skill when the agent works around a defect. A later completed phase
clears a stale pause in `progress.json`; an advisory or warn QC pass is no longer logged as
"PASS (QC ok)". New screens route to `/brieflow-run` by default. The status skill queries sacct
from the launch time (`brieflow_manifest.py --sacct-start`) with every failure state and
`--endtime=now`.

## 0.5.23 — 2026-09-25

marimo 0.25.0. The dev pin moves from 0.23.6 to 0.25.0, matching brieflow `zarr3` and the
brieflow-analysis `marimo` notebook headers. The live-kernel contract is unchanged: the
kernel helpers, the reactive sweep (only cells downstream of the edited marker rerun),
`export_phase_html` and the CLI flags the plugin passes behave as on 0.23.6. The bridge
scripts under `scripts/marimo-pair/` are updated to marimo-pair v0.0.20: `execute-code.sh`
now takes `--url` (no `--port` or registry lookup), exits nonzero when the server ends the
stream without a result, and adds `--file`; `discover-servers.sh` checks each server's
`/health` and reports a `url`. The local change that never deletes registry entries is
kept. The brieflow-notebook skill and the live test call the bridge with `--url`, and the
skill notes that `ctx.edit_cell` raises `StaleCellError` on a cell the kernel has not read.

## 0.5.22 — 2026-09-25

Cleanup release; no change to what a run computes. The hidden `--autopilot` alias for
`--screen` is removed (it was kept one release past 0.5.15); pass `--screen`, or use
`bin/brieflow-run`. Unused helpers are deleted: the pre-adapter sacct and slurm-log
functions in `brieflow_failure_logs.py` (and their re-exports from `brieflow_recover.py`),
`fill_defaults_from_template` and its CSV-literal parser, `find_param_in_config`,
`resolve_to_group`, the `_VALID_SOURCES` alias, `scripts/brieflow_status.sh`, and the stale
`per_phase_walk.md` and `config_parameters.md` references. Skills, commands, `CLAUDE.md`,
`HARNESS.md` and `scripts/README.md` now match the code: the phase list is `PIPELINE_PHASES`
(including `classify` and `analyze`), the recovery classes include `recoverable_fd_limit`,
an OOM on `--backend local` escalates, the recovery docs drop `--resume` (a deprecated no-op),
per-plate SBS runs use `--only-plate`, and the runner's defaults are stated beside the
standalone runloop's. Personal paths and pointers to unshipped planning docs are gone from
docstrings and comments. `tests/test_preflight_combinatorial.py` reads its brieflow checkout from
`BRIEFLOW_CHECKOUT`. `docs/` is no longer tracked (development notes stay local); the
interview spec CSV under it stays tracked for `tests/test_template_consistency.py`.

## 0.5.21 — 2026-09-24

Interview and Slurm changes for four upstream changes. `PIPELINE_GPU` (phenotype,
lab default `None`, brieflow-analysis#111) sets `phenotype.gpu` for the pipeline's
segmentation jobs; `None` follows `GPU`. The `RANSAC_RANDOM_STATE` row is removed: brieflow seeds RANSAC
at 0 and retired `merge.ransac_random_state` (brieflow#305, brieflow-analysis#114). flow.sh now keeps Slurm arrays off
unless given `--arrays` (brieflow-analysis#110), so `--use-arrays` passes `--arrays` instead
of nothing. The same change ships `slurm/config.yaml` in the analysis template with the
partition commented out; a base profile with no `slurm_partition` under `default-resources`
no longer counts as established, so setup still probes and asks for the partition, and
`brieflow_run.py` refuses to submit with it.

## 0.5.20 — 2026-09-24

Aggregation and clustering can run per experimental condition. screen.yaml gains
`cellular.well_annotations`, a per-well table beside it (plate, well, one column per
variable), and `cellular.split_by`, the column whose values each get their own aggregated
dataset and clustering. On every aggregate pass the table is copied to
`config/well_annotations.tsv`, keeping rows with a split value whose well reached merge
(under `all.root_fp`), `WELL_ANNOTATIONS_FP` points at it, `SPLIT_COL` follows `split_by`,
and the test wells are chosen from the kept rows; rows the operator set are left alone,
and removing the table from screen.yaml clears both. A table that cannot be read, lacks a
column or keeps no well pauses the run as a config error, and preflight checks the named
table exists and has plate, well and the split column. At the cluster phase, cell classes
missing from `MIN_CELL_CUTOFFS` take its `all` cutoff. Pairs with brieflow-analysis
`marimo` (the notebook builds its classes and combos from the split) and brieflow `zarr3`
(the per-well parquet pool is read with one unified schema, and annotation values are read
as text).

## 0.5.19 — 2026-09-22

Interview rows for vehicle-referenced scoring (brieflow#293, brieflow-analysis#108).
`TVN_BATCH_CORRECTION` (aggregate, lab default `True`) chooses per-batch TVN or one TVN
on the pooled controls. `CLUSTER_CONTROL_KEY` (cluster, lab default `None`) sets the
cluster-space null separately from the aggregate control key. `BOOTSTRAP_CONTROL_SCOPE`
and `CONTROL_SCOPE` accept `within_perturbation`, which scores each perturbation against
its own arm in the reference group; the two reference-group rows now open for either
`reference_group` or `within_perturbation`.

## 0.5.18 — 2026-09-15

Remote setup now records an explicit connection handoff rather than deriving the Linux
username from the client username or cloud email. The connection helper resolves GCP OS
Login identity (or requires an explicit SSH username), pins project/account/zone/instance,
and verifies remote identity and analysis-directory access. It supports existing SSH hosts
without adding a remote execution backend. The handoff explains on-machine Claude Code
and tmux reconnection; failed access never triggers automatic user or ownership changes.

## 0.5.17 — 2026-09-15

Test runs disable external notifications before collection and in child processes, so
intentional watchdog deaths and migration failures cannot page the real Slack channel.
Failed webhook delivery no longer consumes the warning throttle interval.

Each end-to-end invocation retains local before/after state and runner output in a unique
trace directory. An opt-in Claude Code Stop hook saves content-addressed runtime transcript
snapshots in enabled workspaces; explicit exports from other runtimes can be imported.
No private records are automatically uploaded or published. `/brieflow-improve` stages
evidence-linked baseline/candidate copies and records regression comparisons for maintainer
review. It does not train model weights, promote changes, or establish efficacy on screens.

The analyze interview includes the merged annotation-source row and matches the notebook
defaults: `MOZZARELLM_SOURCE=affinage_then_uniprot`, `MOZZARELLM_N_FEATURES=20`.
Notebook HTML export uses the current interpreter; a live-kernel regression checks that
a direct parameter edit between an agent sweep and export is preserved.

Agent-driven escalation now opens Brieflow bug reports automatically, without per-issue
operator confirmation. The issue skill follows the upstream template and requires a
minimal backward-compatible fix proposal plus regression tests for existing and new
inputs. It checks public-disclosure safety and existing issues before filing; proposed
fixes are not applied to pipeline source during a run. The new `brieflow_issue.py` helper
records GitHub URLs, deduplicates identical reports, and prevents blind creation retries
after an uncertain response. GitHub access failures remain explicit filing blockers.
The standalone runner retains its local evidence files for an agent to investigate and
submit; it does not upload raw diagnostics itself.

## 0.5.16 — 2026-09-14

The `analyze` interview follows the reworked `12_analyze.py`, which now configures brieflow's
billable `mozzarellm` stage instead of one direct adapter call. `SCREEN_CONTEXT` and
`MOZZARELLM_TEMPERATURE` are gone (the screen context is derived upstream from `screen.yaml`;
the `screen_context` computed derivation goes with it). `COMPARTMENT_COMBO` completes the
clustering selection. `MOZZARELLM_ANNOTATE_SELECTED`, `MOZZARELLM_EXTRA_CLUSTERINGS` and
`MOZZARELLM_REWRITE_CONTEXTS` replace the whole-screen sweep with an explicit selection;
`MOZZARELLM_RUN_NAME`, `MOZZARELLM_MAX_WORKERS`, `MOZZARELLM_MAX_FAILED_CLUSTERS`,
`MOZZARELLM_RUNTIME`, `MOZZARELLM_MODE`, `MOZZARELLM_MCP`, `MOZZARELLM_INCLUDE_FEATURES`,
`MOZZARELLM_N_FEATURES`, `MOZZARELLM_FDR_THRESHOLD` and `MOZZARELLM_MAX_TOKENS` land as
lab-default rows; `MOZZARELLM_MODEL` defaults to `claude-sonnet-5`. `config_tiers.yaml`,
`lab_defaults.yaml` and the interview spec CSV move together. `analyze` stays in
`NOTEBOOK_ONLY_PHASES`: the stage it configures is launched on demand, never by the phase
chain.

## 0.5.15 — 2026-09-14

One runner. `/brieflow-run` (`bin/brieflow-run` → `brieflow_run.py --screen` → `run_screen` in
`scripts/brieflow_runner.py`) is the only end-to-end route; `--from/--through/--only` bound
the phase range; `--lifecycle-hooks {noop,gcs}` moves data around the run; the Path A
orchestrator and the GCS approval poll are deleted. A launch gate writes
`.brieflow/launch_review.html` and stops (exit 8) until `--approved`. Every launch writes
`.brieflow/run_manifest.json` (plugin, brieflow SHA, screen repo SHA, interpreter, flags).
Every escalating exit writes `.brieflow/issue_draft.md` for `/brieflow-issue`, which files
after operator confirmation. "autopilot" is retired everywhere (`progress.json` replaces
`autopilot_progress.json`, migrated on first read; `--autopilot` survives one release as a
hidden alias). Migrate is removed. Operator-specific values in `cloud/` and the setup skill
became `BRIEFLOW_*` variables. `tests/test_docs_match_code.py` keeps skills, commands and
root docs honest against argparse, the filesystem and the version strings.

- v0.1.0 — initial 4-command plugin (status / run / migrate / issue)
- v0.2.0 — preflight, tune-memory, auto-recover, Python helpers,
  manual-override preservation, recovery runbook
- v0.3.0 — `brieflow-setup`, `brieflow-notebook`, live marimo-pair parameter
  sweeps, screen.yaml-driven interview bootstrap
- v0.4.0 — interview rebuilt as the 212 uppercase notebook vars with a
  six-source resolution taxonomy (`interview_template.json` is the maintained
  source of truth); `lab_defaults.yaml` wired; preprocess + DET_RANGE + channel
  auto-probes; computed derivations (channels_metadata, viz_channels, segment_cells,
  cmaps, barcode positions); `additional_notes` → candidate overrides; pre-run
  `interview_review`; blank-screen.yaml → operator ask (never guessed). `brieflow_
  wizard.py` is a thin facade over the split modules; consistency/coverage tests
  lock the template ↔ resolvers ↔ probe ↔ spec surfaces.
- v0.5.10 (current) — **cleanup release; the version strings now agree.** `plugin.json` said
  0.5.9 while `pyproject.toml` and `marketplace.json` still said 0.4.33, a whole minor line
  behind, so nothing downstream could tell which number was real. All three are now 0.5.10.
  Also closes the last of the four flow.sh/plugin divergences found on a combinatorial SBS screen:
  `resolve_merge_det_range` kept `record_decision` and the probe's notes read inside
  `if det:`, so a probe that read no pixel size authored a note explaining why and then
  dropped it — nothing in `decisions.jsonl`, nothing on the interview row, and merge died
  several cells deep on an unset DET_RANGE with no record of the cause. The failure path now
  writes the reason to the row's `notes` and journals an `operator-gap` decision, leaving
  `value`/`value_source` unset so the gap scan still reports the row.
- v0.5.9 — **SBS QC gates on BARCODE-level mapping, not gene-level.** Measured across four
  completed plates of a combinatorial SBS screen: single-barcode 0.29 / 0.42 / 0.55 / 0.58, while the
  gene-level metric the gate read was 0.09-0.25 and reported impossible >100% per-well rates (8 wells on
  one plate). Gene assignment loses 55-68% of barcode-mapped cells even though `cell_barcode_0` is the 7-char
  MAP prefix and each of the 100 library prefixes maps to exactly one of 35 genes — the loss is in the
  barcode→`gene_symbol_0` join, upstream. `extract_sbs_qc` now reports `single_map_rate`/`any_map_rate`
  from `1_barcode_cells__count`/`1_or_more_barcodes__count` and keeps gene-level as ungated
  `single_gene_map_rate`/`any_gene_map_rate`. Gate min 0.35→0.25 (operator: "these rates are fine for full
  runs"). Threshold tuning was investigated and ruled out — mapping declines monotonically with
  THRESHOLD_READS, so the sweep's choice of 50 is genuinely optimal.
- v0.5.8 — reverted the unverified throughput work of 2026-08-27 (per-job thread pins +
  CPU-affinity sitecustomize; tag `scale-experiments-20260827`). What was MEASURED and stands: a real
  1080² tile segments in ~27 s whether the job may use 1, 8 or 32 cores (cellpose CPU work is
  single-threaded); each job spawns ~366 threads on a 360-core VM regardless of OMP/MKL/TORCH env vars
  (torch's pthreadpool sizes by ALLOWED cpus); 352 concurrent jobs left the machine at 9% user / 51%
  idle / ~45% system with median job age 48 min. What did NOT hold up: the claim that core-assignment
  collisions explained it, and the sitecustomize remedy built on that claim. Scale is now a MEASUREMENT
  question — establish steps/min at --cores 32 / 64 / 128 / 150 before changing anything.
- v0.5.7 — two overnight-SBS fixes from a combinatorial SBS screen's first full run. (1) **Cross-tile joint
  winner requires majority tile coverage**: a per-tile refine point one tile probed (12/800 → 0.605 on ONE
  tile) beat the 8-tile coarse optimum (15/800 → 0.542 ± 0.046) and was written to config. (2) **OOM
  cascade → recover hand-off**: the runloop's cascade monitor terminates Snakemake as soon as N jobs of a
  rule go OUT_OF_MEMORY — before any `Error in rule` block lands in the master log — so `brieflow_recover`
  found "no rule errors" and ESCALATED instead of bumping (`combine_sbs_info` @ 1600 MB from a single-well
  baseline). The runloop now passes `--oom-rule <rule>` and recover treats it as a peak-at-cap OOM at the
  rule's current recommendation (4× bump, manual_override-flagged), returning `resume`. Also: WS session
  readiness is polled (`/api/sessions`) instead of a 2 s grace (3 parallel kernels raced it), the bridge
  retries a transient no-session, and `library.skip_cycles` is one knob for alignment skips + library
  standardization + MAP/RECOMB regions. (3) **Environment-independent launch**: `brieflow_run` /
  `brieflow_dryrun_preview` prepend the interpreter's bin dir to PATH (the VM orchestrator launches the
  runner by the env's absolute python without `conda activate`; bare `snakemake` was not found), and the
  upstream-rerun guard aborts only on a real `upstream_rerun` classification (a crashed dry-run used to read
  as one). (4) Probe infers `sequencing_order` from MAP+RECOMB window scans (one screen was recomb-first
  under a shared map_recomb assumption; staging/preprocess were fine) and scans MAP cycle windows.
  (5) Preflight channel conventions: phenotype must have a channel named `DAPI` (notebook hardcodes
  `CHANNEL_NAMES.index("DAPI")`; a channel named "Hoechst" paused a run after a full SBS), SBS DAPI-first when
  DAPI_CYCLE ≠ CYTO_CYCLE.
- v0.5.6 — sweep-tile abort root-caused + parallel tiles. (1) Tiles aborted mid-sweep with
  "No running marimo instances found": the marimo-pair bridge discovered the kernel through the server
  registry in `$HOME` (shared NFS on a cluster) using a host-local `kill -0` that DELETED the json when
  the pid wasn't visible, so any bridge run on another node dropped a live kernel's registration.
  `_execute_in_kernel` now addresses the kernel by `--url http://127.0.0.1:PORT` (no registry), and
  `discover-servers.sh`/`execute-code.sh` never delete a registration they can't see from this host.
  (1b) A sweep value that leaves zero reads makes the notebook's `call_cells` cell raise
  (`KeyError: 'gene_symbol_1'`); the bridge then reports `success:false` for the whole execution even
  though the injected band completed — `_execute_in_kernel` now accepts a non-zero bridge exit when the
  band's OK marker is in stdout, and `run_marimo_sweep` treats a per-value cell error as "no rate"
  (data), so the band and tile survive. (Verified on r02c04 t1: the kernel stays healthy afterwards.)
  (2) Diagnosability: marimo server stderr → `.brieflow/_marimo_server.<port>.log` (was /dev/null);
  tile/band exceptions → `<slug>.error.log`; the joint sweep isolates failures per BAND so one bad band
  no longer discards the tile. (3) `adaptive_marimo_sweep_multitile` + `joint_marimo_sweep_multitile`
  run tiles concurrently (`lab_defaults.sweep_tiles.max_parallel_kernels`, default 3; bring-up
  serialized). (4) Kernel readiness timeout 30→120 s (compute nodes miss 30 s cold). Surfaced on a combinatorial SBS screen.
  (5) **Base-order probe** (`brieflow_base_order_probe.py`): the channel↔base assignment is hand-
  transcribed into screen.yaml and a swap is invisible until mapping collapses (one screen's C/T were
  swapped for weeks; a screen without a reference pipeline cannot catch it). The probe runs the SBS read path
  once on the test tile with no segmentation (every peak is a read) and scores read-level LIBRARY
  MATCH under every relabelling of the base channels × cycle direction; argmax vs configured →
  `.brieflow/base_order_probe.json` (`base-order-probe/v1`, verdict ok/mismatch/ambiguous/
  insufficient). The runner runs it after the SBS notebook writes config; preflight gates HIGH on
  `mismatch` (suggested bases order in the issue), MEDIUM on ambiguous. Its `configured.match` is
  also the cleanest read-level diagnostic for a low mapping rate: high → loss is downstream
  (segmentation / THRESHOLD_READS / cell calling); low → alignment or cycle set. Validated on
  one screen's test tile: configured [T,C] 0.668 vs swap 0.019 (chance 0.08).
  (6) **One skip-cycle knob**: `screen.yaml library.skip_cycles: [6]` → the interview derives
  `SKIP_CYCLES` (alignment drops the cycle), `SKIP_CYCLES_MAP` / `SKIP_CYCLES_RECOMB` (skipped positions
  relative to each region, for library standardization) AND the shrunken MAP/RECOMB positions, mirroring
  brieflow's `compute_barcode_cycle_positions`. Setting these separately is how one screen ended up with a 7-wide
  MAP region against a 6-char library (`library-region-mismatch`, caught by the probe).
- v0.5.5 — joint sweep + channel-reorder tool, from debugging a combinatorial SBS screen against its reference
  pipeline. (1) `joint_marimo_sweep_multitile`: PEAK_WIDTH and THRESHOLD_READS INTERACT (the peak
  score is max−min inside a PEAK_WIDTH window, so width rescales what the threshold gates), yet they
  were swept as two independent 1-D passes. Now `lab_defaults.sweep_joint` lists params swept as a
  PAIR on one live kernel per tile: exhaustive product of the coarse bands + a scale-free local refine
  (threshold ×2^(±1/4,±1/2), width ±1), argmax of cross-tile mean SINGLE_MAP_RATE; sequential
  per-param sweeps remain the fallback. (2) THRESHOLD_READS band is log-spaced 50–3200 (screen-
  independent; peak scores span ~2 decades across screens), PEAK_WIDTH 3–15 step 2. (3)
  `brieflow_reorder_channels.py`: permute a converted store's channel axis in place (idempotent
  marker, self-verifying) so a wrong channel order (DAPI must be channel 0) is fixed without
  re-running preprocess. Surfaced: with DAPI last and DAPI_CYCLE≠CYTO_CYCLE the segmentation composite
  indices go wrong and cellpose segments the spot channel; preflight enforcement of DAPI-first TODO.
- v0.5.4 — DAG-driven reactive SBS sweep (the THRESHOLD_READS/PEAK_WIDTH optimization
  was intractable on large plates). (1) Sweep tiles bounded to a well-spread sample
  (`_bounded_sweep_tiles`, `sweep_tiles.max_tiles=10`) so cost is O(max_tiles × values), not
  O(n_wells × values) — the one-tile-per-well fan-out made a 60-well plate ~480 runs/param.
  (2) `adaptive_marimo_sweep_multitile` runs the sweep reactively: each tile is segmented ONCE in
  a live marimo kernel (`watch=False`), then the marker cell is mutated so only downstream cells
  recompute (coarse then fine band on the same kernel), replacing a fresh notebook subprocess per
  (value,tile) that re-ran alignment+CellPose every trial (~589 s each). All context-bound work
  (tees, baseline, `run_marimo_sweep`) is injected INTO the kernel through the marimo-pair
  `execute-code.sh` bridge (`_execute_in_kernel`), because `marimo._code_mode.get_context()` only
  attaches in-kernel — and the kernel boots STALE, so `_kernel_baseline` runs every cell once (the
  one segmentation) before the bands sweep. The resolver prefers it and falls back to the headless
  subprocess path when the live-kernel route yields no usable result (raises on no finite rate, so
  the fallback actually fires). Per-tile coarse-winner semantics to be confirmed on a real kernel.
  Surfaced across two combinatorial SBS screens.
- v0.5.3 — interviewer fix: `CALL_READS_METHOD` is now a chemistry-conditional
  computed derivation (`_compute_call_reads_method`: combinatorial→`frac`, four_color→`median`,
  operator-overridable to `merfish`), replacing a static `lab-default: "median"` that broke every
  combinatorial screen at `call_reads` (`Method 'median' is not supported for chemistry
  'combinatorial'`). Surfaced running combinatorial SBS screens.
- v0.5.2 — Phenix `rNNcNN` well-split port + slurm-policy hardening. (1)
  `brieflow_tiles.py` splits well ids through a convention-aware `_split_well`
  (alpha `A1` + Phenix `r03c05`) at all three metadata-path sites, so the test/
  validation/anchor tile selectors resolve HCS-nested paths instead of 404ing —
  mirrors brieflow's own `split_well_to_cols` (lib fix in brieflow PR #249). (2)
  `provision_vm.sh` zone-fallback matches the newer `ZONE_RESOURCE_POOL_EXHAUSTED`
  capacity messages, not only the legacy `reason: stockout`, so fallback fires. (3)
  `--latency-wait` defaults to 5s with a hard cap of 10s (`brieflow_recover`
  ladder now `[10]`, was `[60, 120, 240]`): a longer wait only delays surfacing a
  genuinely-missing output, and the real fix is mem/retry. (4) `brieflow_compute`
  writes `runtime: "1d"` into the base slurm profile (was `400` minutes), so long
  SBS/phenotype rules aren't killed mid-run.
- v0.5.1 — bug fixes surfaced running Phenix screens on slurm. (1) Coarse
  (per-cycle) external metadata now extracts all FOVs: `get_well_from_wildcards` returns
  None on a well-less job instead of crashing, and the `extract_metadata_tiff` FOV filter
  was removed (the dispatcher passes `tile=0`, which zeroed the table) — brieflow-side, PR
  #248. (2) `brieflow_compute` no longer emits `slurm_extra: --output=…`, which
  plugin-slurm ≥2.6.0 forbids and which silently deadlocked submission; failure-log readers
  now follow the executor's `.snakemake/slurm_logs` tree. (3) `build_vm_image.sh` SSHes via
  IAP, unblocking image builds on IAP-only GCP projects.
- v0.5.0 — Opera Phenix support + backend-invariance. Generic split-tiff
  probe inference (prefix-hint role classifier, distinct-z mode) for Phenix/per-
  channel tiff conventions; `Index.xml` stage-metadata auto-config (probe emits the
  `*_METADATA_*` vars, paired with brieflow #246's file-type metadata reader —
  notebooks stay stock). Preflight mem-cap made backend-invariant: consults the same
  layers `resolve_resources` applies at run time (mem_recommendations baseline +
  `scaling_formulas`), so a fresh screen passes on slurm and local alike with no
  hand-authored caps — and it doubles as the brieflow-drift detector. Compute identity
  is probe-established (`brieflow_compute`): backend from `sbatch` presence, partition
  operator-picked from `sinfo` (never guessed), account from `sacctmgr` — no hardcoded
  WIBR slurm profile. Runner conductor codified as the one run entry point.

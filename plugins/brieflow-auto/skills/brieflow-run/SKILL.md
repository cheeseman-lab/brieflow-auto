---
name: brieflow-run
description: Use to run a brieflow screen through the per-phase cycle (notebook → preflight → run → QC) over any range of phases — the whole screen, one phase, or a --from/--through range — with the interview resolved up front and the loop pausing only on a genuine operator decision. Triggered by "run the screen", "run the whole pipeline", "rerun sbs", "run only through merge", "resume the run", etc.
---

# Brieflow — Run (one runner, any range of phases)

The composition over the five modules. Where the interactive flow asks the
operator per phase, the runner resolves the **whole** interview up front and
converts every "operator-tunes-after-output" decision into an agent step
(parameter sweeps, eval-table selections). Then it runs the identical per-phase
cycle for every phase in range — the whole screen by default, `--only <phase>`
for one, `--from`/`--through` for a range.

```
RUN:          interview(FULL SCREEN, once)  →  for phase in range: cycle(phase)
              cycle(phase) = notebook → preflight → run → QC
```

The only difference from interactive is interview placement (full-screen-upfront
vs inline-per-notebook); the per-phase cycle is the same modules.

## Run records and optional transcripts

The runner automatically retains one execution trace per invocation, including pauses;
`trace.json` keeps the exception message and traceback tail, and the agent session ID when
the runner starts inside a Claude Code session. Transcript capture is turned on at setup
(`brieflow-setup` step 6; details in `../../scripts/TRACE_RECORDS.md`). If
`.brieflow/record_rollouts.json` is missing from the session workspace and no
`rollout-capture` decline is journaled, explain once that captures are local and may hold
private conversation and tool output, then enable that workspace. Do not enable unrelated
sessions or publish raw captures. A transcript is separate from the runner's decision journal;
never claim that a journal alone is a saved rollout.

## Preconditions (the runner refuses / pauses otherwise)

- `screen.yaml` filled in by the operator. The runner checks it first
  (`scripts/brieflow_screen_yaml.py check`) and stops with exit 3, naming every blank
  REQUIRED field, before resolving anything else. Never fill those fields for the operator.
- Interview resolves with **no operator gaps** — every `user`/`user_tuned_seed`/
  `override` row has a value. If gaps remain, the runner stops and tells you which;
  fill them with `/brieflow-notebook`, then relaunch.
- Raw data staged in the layout brieflow reads (`scripts/brieflow_validate_tree.py
  --layout`). The runner checks the inputs screen.yaml names on every launch (after the
  pull, when a lifecycle hook pulls them) and stops with exit 9 otherwise.
- **Segmentation-model deps installed if non-default.** The runner does NOT install
  the model-specific deps. If the interview lands on `CELLPOSE_MODEL=cpsam` (e.g.
  RPE1) or `SEGMENTATION_METHOD=stardist`, install them into the screen env before
  launching (brieflow-notebook Step 3b: `uv pip install cellpose==4.0.4 torch==2.7.0
  torchvision==0.22.0` for cpsam). Default cyto3 needs nothing.
  - **Which env path depends on `detection_target` (DNA vs RNA):**
    - **DNA screens** (nuclear barcode spots → SBS segments nuclei only,
      `SEGMENT_CELLS=False`): SBS uses cyto3/nuclei, which exist ONLY on cellpose
      3.1.0 (cpsam is 4.x-only). So when phenotype wants cpsam, run **cpsam for
      phenotype only**: finish + cache ALL SBS on 3.1.0 first, THEN upgrade the env
      in place (`uv pip install cellpose==4.0.4 …`) and run phenotype. After the
      upgrade, never `--forcerun` an SBS rule — SBS TIFF intermediates are `temp()`,
      so a forced rerun re-triggers SBS segmentation under 4.x and fails.
      Order: SBS → upgrade → phenotype → merge/aggregate/cluster.
    - **mRNA / RNA screens** (`detection_target: RNA` — cytoplasmic spots → SBS
      segments whole CELL bodies, `SEGMENT_CELLS=True`): SBS uses **cpsam (4.x)**
      too, same as phenotype. Install cpsam up front and run the WHOLE pipeline on
      cellpose 4.x — no 3.1.0 stage, no env-upgrade dance, no SBS-first ordering.
      The "SBS can't run on 4.x" caveat above is specific to the cyto3/nuclei/
      SizeModel models DNA-screen SBS relies on; cpsam-based SBS runs fine on 4.x.
- **Phases split by hardware (CPU → GPU → CPU) — hand the VM off twice.** preprocess is
  CPU-bound; SBS + phenotype segmentation (cpsam) want a GPU box; merge/aggregate/cluster
  are CPU/RAM-bound and OOM on a GPU box's memory. So run preprocess on a highmem CPU VM,
  hand off to a GPU box for the segmentation window, then hand back to the CPU box for
  merge onward — don't burn GPU cost on the CPU-bound phases. Do it by **same-zone disk
  reattach, never a copy** — an rsync scrambles mtimes and makes `--rerun-triggers mtime`
  drag completed phases back into the DAG. Full procedure + gotchas (stockout downsize, fstab, kernel-pin):
  [`brieflow-setup/references/machine_handoff.md`](../brieflow-setup/references/machine_handoff.md).
- **Multi-plate with divergent SBS config** (e.g. jannu: plate_1 skips its failed
  cycle 1, plate_2 skips nothing) is driven natively by the runner via
  **branch-and-converge** — no manual SBS pass. Declare the per-plate divergence in
  `screen.yaml`:
  ```yaml
  iss:
    plate_overrides:
      1: { skip_cycles: [1] }       # cycle NUMBERS to drop for this plate
      2: { }                         # no override (omittable)
      # optional per-plate: manual_background_cycle: <cycle number>
  ```
  When `iss.plate_overrides` is present, the SBS phase generates a
  `config/config_p<N>.yml` per plate (base config + that plate's
  `skip_cycles_indices` / `manual_background_cycle_index`, indices derived from the
  plate's own cycle list in `sbs_samples.tsv`) and runs `flow.sh sbs --only-plate N
  --configfile config/config_p<N>.yml` once per plate. SBS outputs are `{plate}`-keyed so
  the plates coexist; phenotype/merge/aggregate/cluster then converge over the union
  (base config). Divergent cycle *counts* alone need no override — preprocess globs
  each plate's cycles into the sample table and `n_cycles` is per-tile derived.
  - **A skipped cycle MUST also be applied to that plate's barcode library — not
    just its image config.** This is the easy half to miss: `skip_cycles` only
    enters `config_p<N>.yml` for image processing, but the barcode library the
    notebook builds is plate-agnostic (one `barcode_library.tsv`, full-length
    prefix). When a plate drops a cycle, its called barcodes are one base shorter
    and shifted, so they will **not** match the full-length library — the result is
    **0% mapping**, which surfaces only at the end as `eval_mapping` crashing on an
    empty summary (`matplotlib ... Number of rows must be a positive integer, not
    -1`). `call_cells` uses the library `prefix` column **as-is** (no re-truncation),
    so the library prefix length must equal that plate's `n_cycles`, aligned to the
    cycles actually sequenced:
    - plate skips cycle 1 (sequences cycles 2..N) ⇒ library prefix = drop the FIRST
      base (`prefix[1:]`), i.e. `standardize_barcode_design(map_prefix_length=N,
      skip_cycles_map=[1])`.
    - plate sequences cycles 1..N-1 normally (just fewer cycles) ⇒ library prefix =
      first N-1 bases (`prefix[:N-1]`), i.e. `map_prefix_length=N-1, skip_cycles_map=None`.
    Two plates with different windows need **different** libraries; emit a
    `config/barcode_library_p<N>.tsv` per plate and point each `config_p<N>.yml`'s
    `sbs.df_barcode_library_fp` at its own. The plates still converge downstream
    because both map into the same `gene_symbol` space. (Verify the trim is lossless
    first: confirm prefixes stay unique per gene at the shortened length.)
    **Automated:** `generate_plate_configs` (via `_write_plate_barcode_library`) now
    emits a per-plate `config/barcode_library_p<N>.tsv` and points each
    `config_p<N>.yml`'s `df_barcode_library_fp` at it, selecting the design-prefix
    positions `[c-1 for c in sequenced_cycles]` (sequenced = plate cycles minus
    `skip_cycles`). It no-ops when a plate sequences the full design in order, so
    single-plate / non-divergent screens are untouched. Idempotent — it reads the
    untrimmed design from the base config every time, so it's safe to regenerate on
    every resume, which is what makes resuming across the branch→merge
    boundary safe.
  - **The merge reference cycle auto-avoids skipped cycles too.** `SBS_METADATA_CYCLE`
    (merge) selects the SBS cycle whose stage coordinates seed `fast_alignment`'s
    tile pairing. Its lab-default is `1` — exactly the cycle most often skipped — and
    a dropped cycle carries unreliable stage coords that mis-pair whole wells (jannu
    2026-07-06: plate-1 well B2's cycle-1 coords were offset ~1130 px vs cycles 2-12,
    so `fast_alignment` failed). So the merge resolver (`brieflow_resolvers` C0c)
    unions the global `SKIP_CYCLES` with **every** `plate_overrides.*.skip_cycles` and
    bumps `SBS_METADATA_CYCLE` to the lowest non-skipped cycle — a per-plate skip
    drives the merge reference cycle with no manual interview pin. Operator-set values
    that aren't themselves skipped are preserved.

## Steps

1. **Detect screen repo**; resolve the absolute analysis dir.
2. **Review before launch (operator double-check).** Because the runner resolves
   the whole interview at once (~200 params), present the review so the operator
   can sanity-check what was auto-set before committing to an unattended run:
   ```bash
   # SCRIPTS_DIR = the plugin's scripts/ dir; resolve from this skill's base dir
   # (printed as "Base directory for this skill:" at invocation):
   SCRIPTS_DIR=$(realpath "<skill-base-dir>/../../scripts")
   PYTHONPATH="$SCRIPTS_DIR" python -c "import brieflow_wizard as bw; \
     r=bw.resolve_all_phases('<abs-analysis-dir>'); \
     iv=bw.load_interview('<abs-analysis-dir>/.brieflow/interview.json'); \
     rev=bw.interview_review(iv, note_overrides=r['note_overrides']); \
     print(bw.render_interview_review(rev)); \
     open('<abs-analysis-dir>/.brieflow/interview_summary.html','w').write(bw.render_interview_review_html(rev))"
   ```
   This also writes a single consolidated **`<abs-analysis-dir>/.brieflow/interview_summary.html`**
   — the one-glance "everything that was auto-decided" artifact (badge counts +
   attention sections + every param by phase). Surface the `⛔ Needs your answer`
   and `📝 From your notes` sections first, point the operator at the HTML summary,
   apply any confirmed note-derived overrides (`set_value`, source `operator`)
   before launching.
3. **Slack paging gate (operator double-check, before launching the unattended
   loop).** Paging is the only channel to reach the operator on a blocker — never
   start a headless run silently unpageable.
   - Check configuration:
     ```bash
     PYTHONPATH="$SCRIPTS_DIR" python -c "import brieflow_runner as a; \
       print('configured' if a._webhook_configured() else 'unset')"
     ```
   - If `unset`, ask via `AskUserQuestion`: *"Slack paging isn't set up —
     configure it now, or run without paging?"* Options: `[Configure]` `[Skip]`.
     - **Configure:** collect the webhook URL, then
       ```bash
       PYTHONPATH="$SCRIPTS_DIR" python -c "import brieflow_runner as a; \
         a.set_slack_webhook('<url>')"
       ```
       then record the gate decision — the gate records that paging is
       *configured*; the launch-time `notify_selftest` (step 4, already
       part of startup) is what confirms it's actually *delivered*:
       ```bash
       PYTHONPATH="$SCRIPTS_DIR" python -c "import brieflow_decisions as d; \
         d.record_decision('<abs-analysis-dir>', kind='slack-gate', phase='pre-launch', \
         choice='paging ON (configured)', why='operator configured webhook at run start', \
         source='agent')"
       ```
     - **Skip:** record `choice='paging OFF (operator declined)'`, `source='agent'`,
       and proceed.
   - If `configured`, record `choice='paging ON (configured)'` — same
     distinction: delivery is confirmed by the launch-time `notify_selftest`
     in step 4, not here.
   The `slack-gate` entry is the decision journal's first line
   (**`<abs-analysis-dir>/.brieflow/decisions.md`**). Then launch.
4. **Launch through the gate.** The runner is the one entry point for any range of
   phases. First invocation writes the review and stops:

   ```bash
   SCRIPTS_DIR=$(realpath "<skill-base-dir>/../../scripts")
   pgrep -fa "brieflow_run.py --screen .*<abs-analysis-dir>" && { echo "already running — do not double-launch"; }
   "$SCRIPTS_DIR/../bin/brieflow-run" --analysis-dir <abs-analysis-dir> --backend <slurm|local> \
       [--only <phase> | --from <phase> --through <phase>] [--lifecycle-hooks gcs --screen-id <screen>]
   # exit 8: .brieflow/launch_review.html written
   ```

   Print `.brieflow/launch_review.json` sections 1, 2, 4 and 6 to the terminal and give the
   operator the HTML path to scroll. Ask ONE `AskUserQuestion`: "Launch as reviewed / Change
   something / Stop". On "Change something", route to `/brieflow-notebook` for the phase they
   name and come back here. On "Launch as reviewed", re-run the same command with `--approved`
   (in tmux or a systemd unit for a long run; the runner journals `launch-approved`).
   Never pass `--approved` on the first invocation.
5. **It pauses + notifies (non-zero exit) on a real blocker:**
   - template gap (operator input needed) — fill via `/brieflow-notebook`.
   - raw input data not in the expected layout (exit 9) — stage it with `/brieflow-setup`
     ("Raw data staging"). When only a confirmation is left (phenotype rounds of different
     sizes, uneven cycle counts), ask the operator once; on a yes, relaunch with
     `--accept-layout` (journaled as `layout-accept`).
   - sweep below the evidence floor (`SWEEP_MIN_FINITE_FRAC` / `SWEEP_MIN_FINITE_TILES` in
     `scripts/brieflow_sweep.py`) — the parameter is left unresolved, not tuned-confirmed;
     rerun the sweep where it can finish (inside a Slurm allocation) or set the value by hand.
   - submission failure (recovery found no job reached Slurm) — fix the profile it names,
     typically a numeric `slurm_partition` summed across a job group.
   - preflight failure — fix the config key it names, then relaunch.
   - QC-gate failure — the operator decides: retune the offending phase (`/brieflow-notebook`
     + `/brieflow-qc`), or accept the result with `--accept-qc <phase>`. Before accepting, or
     recommending acceptance of, an sbs, phenotype or merge phase, render all of its alignment
     views and check that they agree (`/brieflow-notebook` Step 6a': every cycle and base
     channel, every merge tile-site pair, every positions tile overlap and phenotype-in-SBS
     pair). Record a finding for each off cycle, channel or pair; propose `SKIP_CYCLES` (or
     the positions best orientation) in the launch review, never apply it. Accepting journals
     a `qc-accept` decision (source operator) and marks the phase done without a rerun; it needs
     the phase's finished run (`.brieflow/phase_complete/<phase>.json`) and leaves the launch
     review unchanged, so the same `--approved` command continues with the next phase:
     ```bash
     "$SCRIPTS_DIR/../bin/brieflow-run" --analysis-dir <abs-analysis-dir> --backend <slurm|local> \
         --accept-qc <phase> --approved
     ```
   - run failure after recovery (recovery exhausted) — inspect with `/brieflow-status`.
   - upstream-rerun guard — a completed upstream phase would re-run; confirm before forcing.
   - disk shortfall — free space / change target.
   - notebook failure — the phase's config was never written; read the log the notify names.
   Each pause records why in the progress state, and the ones the phase loop returns also
   leave `.brieflow/issue_draft.md` and `.json` as local evidence. For an identified
   pipeline defect, immediately invoke `/brieflow-issue`: it opens an actual issue
   automatically, with a backward-compatible fix proposal and regression-test plan.
   Do not stop at a draft or ask for per-issue confirmation. Keep routine config,
   resource and QC problems out of the upstream tracker. Report the issue URL or
   explicit filing blocker, and keep the analysis paused. Do not edit Brieflow's
   source to fit the screen. Resume only after the underlying problem is resolved
   (completed phases are skipped).
6. **Exit 0** = all phases complete. The final message lists any phase whose QC was a warn,
   an advisory fail or an accepted fail (`.brieflow/qc_status.json`). Review results.

A phase whose run finished (its phase-complete marker) is not resubmitted on re-entry when the
config is unchanged in content and no upstream phase completed after it: the runner gates QC on
the existing outputs. Otherwise it logs why Snakemake would rerun (dry-run reasons and
Snakemake's list-changes counts for params, code and input) and journals a `phase-rerun`
decision before it runs. Escalation drafts carry a `route` (`operator-decision`, `config`,
`investigate`, `upstream-issue`); only `upstream-issue` goes to `/brieflow-issue`.

## Rerunning a screen

A rerun in the same analysis repository starts from what the last run recorded, so the operator
is not asked again:

- `.brieflow/interview.json` keeps every resolved value with its source. Values that came from
  `screen.yaml` are re-derived whenever `screen.yaml` changes (the interview stores its sha);
  values from the operator, a probe or a sweep are kept.
- The launch review is written again and must be approved again (`--approved`) when anything it
  lists has changed.
- To rerun from a fresh clone, copy `screen.yaml`, `config/` and `.brieflow/interview.json` into
  it; the run then starts from the same recorded values. `.brieflow/run_manifest.json` records the plugin
  and pipeline versions of the earlier run for comparison.

## Where sweeps run

With `--backend slurm`, the runner's parameter sweeps run on a compute node through `srun`
(`scripts/brieflow_sweep_alloc.py`; resources and the on/off switch are `sweep_tiles.*` in
`data/lab_defaults.yaml`; partition and account come from `slurm/config.yaml`), never on the
host the runner orchestrates from. A live-kernel sweep pays each tile's baseline (load, align,
segment) once; its limit scales with the tile's image size. A sweep that stops on a time limit
is reported as a timeout (`SweepTimeout`), not as "no finite mapping rate". The headless
fallback (`scripts/brieflow_cell_runner.py`) also primes each tile once and then reruns only
the cells downstream of the changed marker that the metric needs. Refine points are fixed once
from the first batch of tiles and probed on every tile, so refine rows compare across tiles.

## Watching a run

Use the shipped watcher, not a hand-written loop:
```bash
python "$SCRIPTS_DIR/brieflow_watch.py" --analysis-dir <abs-analysis-dir>
```
Run it on an interval (Monitor or `/loop`). It prints the runner state, the runner events new
since the previous call, and this launch's failed Slurm jobs by rule and state (all failure
states, counted from the launch, across midnight). Exit 0 running, 1 needs attention, 2
finished, 3 stopped. Classify runs only when
   the interview's `RUN_CLASSIFY` is True; otherwise the runner journals a `skip-phase`
   and aggregate runs without a classifier.

## Judgment-call logging

Whenever you make a non-mechanical decision during the run — interpreting a
blocker as real vs. transient, confirming an `additional_notes` candidate
override, accepting a borderline QC result and proceeding — record it:
```bash
PYTHONPATH="$SCRIPTS_DIR" python -c "import brieflow_decisions as d; \
  d.record_decision('<abs-analysis-dir>', kind='<short-kind>', phase='<phase>', \
  choice='<what you chose>', why='<one line>', source='agent')"
```
The mechanical decisions (sweep winners, blocker pauses, recovery actions) are
logged automatically; this is for the reasoning only you have. The journal is
**`<abs-analysis-dir>/.brieflow/decisions.md`**.

## Workarounds are findings

Every workaround made in place (a `screen.yaml` edit, an input symlink tree, a resource
stopgap, a sticky interview-row rewrite) and every defect you cannot fix is a finding:
call `record_finding()` from `scripts/brieflow_findings.py` before continuing (router rule
R5).

## Relationship to the other commands

`/brieflow-run` is the composition; the same building blocks are individually
operator-invokable: `/brieflow-notebook` (configure one phase), `/brieflow-preflight`
(validate config), `/brieflow-module` (run one phase), `/brieflow-qc` (gate one
phase), `/brieflow-status` (monitor).

## Plugin script

`<plugin>/scripts/brieflow_run.py --screen` — the conductor lives in
`brieflow_runner.py:run_screen`.

## Exit

Return to the router skill (`brieflow-auto`). On a pause, surface the blocker
and the exact command to resolve it.

---
name: brieflow-notebook
description: Configure + run a single brieflow phase notebook (preprocess / sbs / phenotype / merge / classify / aggregate / cluster / analyze). Pre-fills marker-block parameters from the interview, runs the notebook in a live marimo kernel, varies parameters via the marimo reactive API (`ctx.edit_cell` + `ctx.run_cell` through the scratchpad) to beat QC + maximize the phase's optimization target (DAG-aware — upstream cells stay cached), then validates across additional tiles and exports HTML. Triggers on "configure preprocess", "configure sbs", "run the phenotype interview", "fill in the merge notebook", "set up the cluster step", etc.
---

# Brieflow — Run a phase notebook end-to-end

You are running one phase of a brieflow OPS pipeline. This skill walks the
sequence in order: fill the interview if it's not yet filled, push the values
into the notebook, start the kernel, run baseline, vary parameters until QC
passes and the target metric is good, validate on additional tiles, export
HTML.

The plugin gives you small primitives. The orchestration lives in this skill
as a linear sequence of steps. Read top to bottom, execute step by step.

## Prerequisites

Before this skill is invoked, the following must already be in place
(`brieflow-auto:brieflow-setup` or the router covers this):

- `<analysis-dir>/screen.yaml` exists and is populated.
- `<analysis-dir>/.brieflow/interview.json` exists (even if some rows are
  empty — Step 1 walks the missing ones).
- The screen's conda env (`screen.yaml.pipeline.conda_env`) is installed
  and contains `brieflow` plus `nest_asyncio`.

## What this skill does NOT do

- Bootstrap a new screen → use `brieflow-auto:brieflow-setup`.
- Run the full snakemake pipeline on slurm/cloud → use `brieflow-auto:brieflow-module`.

## Inputs

- `<analysis-dir>` — the screen's analysis directory (contains `screen.yaml`,
  `.brieflow/`, the phase notebooks, `config/`).
- `<phase>` — one of the phases in `PIPELINE_PHASES` (`scripts/brieflow_phases.py`),
  which also maps each to its notebook (`0_preprocess.py`, `1_sbs.py`, … `7_analyze.py`).

## Derived state

- `<conda_env>` — read at Step 4 from `<analysis-dir>/screen.yaml`
  `pipeline.conda_env`. Not a separate input.
- `<scripts-dir>` and `<execute-code-sh>` — resolved at Step 0 below, relative
  to this SKILL's base directory.

## Path variables used throughout this SKILL

| Variable | What it points to |
|---|---|
| `<analysis-dir>` | The screen's analysis directory (input). Holds `screen.yaml`, `.brieflow/`, phase notebooks, `config/`. |
| `<screen-root>` | Parent of `<analysis-dir>`. Holds the screen-repo's canonical notebook sources, `<canonical-notebook-dir>/<phase>.py`. |
| `<scripts-dir>` / `$SCRIPTS_DIR` | The plugin's `scripts/` directory; resolved in Step 0. Holds `brieflow_wizard.py` and `marimo-pair/`. |
| `<plugin>` (e.g. in `<plugin>/data/...`) | The plugin root, parent of `<scripts-dir>`. Holds `data/interview_template.json` and skill / command sources. |
| `<phase>` | One of the phases in `PIPELINE_PHASES`. |

## Step 0 — Resolve plugin-local paths

Claude Code prints `Base directory for this skill: <skill-dir>` at the top
of the skill-invocation response. This SKILL lives at
`<plugin-root>/skills/brieflow-notebook/SKILL.md`, so:

```bash
SKILL_DIR=<skill-base-dir-from-invocation-message>
SCRIPTS_DIR=$(realpath "$SKILL_DIR/../../scripts")
EXECUTE_CODE_SH="$SCRIPTS_DIR/marimo-pair/execute-code.sh"
```

These two values are referenced throughout the SKILL — capture them once at
the start of the run. They are NOT hardcoded absolute paths because the
plugin can be installed at any prefix.

Every kernel-side Python invocation begins with this preamble — paste it
into every `execute-code.sh` heredoc:

```python
import sys, asyncio, nest_asyncio
nest_asyncio.apply()    # marimo's scratchpad runs inside a running loop
sys.path.insert(0, '<scripts-dir>')   # substitute the value resolved in Step 0
```

If you forget `nest_asyncio.apply()`, every `asyncio.run(...)` raises
`RuntimeError: cannot be called from a running event loop`.

### Where these helpers live

This SKILL invokes plugin helpers that ship with the plugin — they are
NOT in brieflow's conda env and are not pip-installed anywhere. See
CLAUDE.md "Plugin helpers vs the brieflow library" for the full import
model. Quick reference for this SKILL's surface:

- `$SCRIPTS_DIR/brieflow_wizard.py` — `init_screen_interview`,
  `interview_completeness`, `patch_marker_block_value`,
  `start_marimo_kernel`, `setup_kernel_tees`, `run_marimo_sweep`,
  `validate_across_tiles`, `export_phase_html`, `parse_qc_block`, …
- `$SCRIPTS_DIR/marimo-pair/execute-code.sh` — the scratchpad bash tool.
- `$SCRIPTS_DIR/_ws_holder.py` — WebSocket holder; written automatically.

Inside scratchpad heredocs: the preamble above
(`sys.path.insert(0, '<scripts-dir>')`) makes `import brieflow_wizard`
work. Outside (bash sanity checks, ad-hoc `python -c`):
`PYTHONPATH="$SCRIPTS_DIR"` is the equivalent.

```bash
PYTHONPATH="$SCRIPTS_DIR" python -c "import brieflow_wizard; print(brieflow_wizard.__file__)"
# expect a path under ~/.claude/plugins/cache/brieflow-auto/brieflow-auto/<version>/scripts/
```

`from lib.sbs.* import ...` inside the scratchpad reaches real brieflow
library modules from the conda env — those ARE pip-installed and are a
separate codebase from the plugin helpers above.

## Step 1 — Conduct the interview

The interview is a row-per-parameter JSON file at
`<analysis-dir>/.brieflow/interview.json`. It captures every value the
phase notebooks need that isn't a true library default — combining
operator answers, screen.yaml pre-fills, and auto-probed values.

### How the interview gets filled

**screen.yaml is the source of truth for everything the operator knows
about the screen** (paths, channels, library design, optics, conda env,
etc.). The interview-template at `<plugin>/data/interview_template.json`
maps each interview row to its source — usually a `derivation` clause
(e.g. `{type: screen_yaml, path: library.raw_design_table_fp}`), sometimes an
`auto-probe` helper, occasionally a `bucket=user` prompt.

When you encounter a row with `bucket=auto + value=null`, your job is to
reason about what fills it. Walk screen.yaml section by section and check:

- **collection / experiment / cellular** — archival metadata; not
  consumed by the run-notebook flow (the BIA uploader reads them).
- **library** — barcode design table, sgRNA / gene column names, prefix
  length, detection target. These map directly to SBS marker-block rows
  (`DF_DESIGN_FP`, `GENE_SYMBOL_COL`, `GENE_ID_COL`, `PREFIX_LENGTH`,
  `SEGMENT_CELLS` + `SORT_CALLS` derived from `detection_target`).
  - **Simple mode** (`library.type == "simple"`): `library.barcode_col` →
    `BARCODE_COL` (raw column name in df_design). Downstream uses
    `PREFIX_COL = "prefix_col"` (canonical post-standardization name).
  - **Multi mode** (`library.type == "multi"`): two pairs of vars,
    distinct purposes:
    - `library.barcode_map_col` / `library.barcode_recomb_col` →
      `BARCODE_MAP` / `BARCODE_RECOMB` — **raw** column names in
      df_design (e.g., `"iBAR_2"` / `"iBAR_1"`). Passed to
      `standardize_barcode_design` as its `prefix_map` / `prefix_recomb`
      args.
    - `PREFIX_MAP = "prefix_map"` / `PREFIX_RECOMB = "prefix_recomb"` —
      **canonical** post-standardization column names. Always literal;
      not derived from screen.yaml. Used downstream by `prep_multi_reads`
      / `call_cells`.
  - The two pairs are NOT interchangeable. Collapsing them into one var
    (as some older notebooks did) produces a `KeyError` at
    `prep_multi_reads` time when the raw name doesn't exist in `df_reads`.
- **iss** — SBS-side optics + channel layout + cycle order. The 5 channel
  names feed `channels_metadata`; `background_channel.{channel,cycle}`
  fills `cyto_channel` + `cyto_cycle`; binning + objective drive
  `merge.det_range`.
- **phenotype** — same shape for the phenotype notebook. `channels[]`
  feeds `phenotype.channels_metadata`; `background_channel_cell` fills
  `phenotype.cyto_channel`; binning + objective contribute to
  `merge.det_range`.
- **pipeline** — `conda_env` is the bash env for every shell invocation.
- **data** — `storage_format` + `input_sbs_dir` + `input_phenotype_dir`
  go into `preprocess` rows.

**Preprocess shortcut — one comprehensive data probe.** For the `preprocess`
phase, prefill all the data-derivable rows in one shot instead of probing
piecemeal:

```python
import yaml
from brieflow_wizard import (
    probe_preprocess, apply_preprocess_probe, load_interview, save_interview,
)
screen = yaml.safe_load(open(f"{analysis_dir}/screen.yaml"))
iv = load_interview(f"{analysis_dir}/.brieflow/interview.json")
report = probe_preprocess(analysis_dir, screen)   # inspects both sides' raw data
apply_preprocess_probe(iv, report)                # fills rows, value_source=auto-probe
save_interview(iv, f"{analysis_dir}/.brieflow/interview.json")
# report["operator_needed"] + report["notes"]: what couldn't be inferred
# (tiff channels, non-uniform channels, no data) — ASK the operator for these.
```

It composes `detect_data_format` / `detect_organization` / `detect_z_planes` /
`infer_path_pattern` + an ND2-metadata channel read (uniformity +
screen.yaml reconciliation). It prefills only what the *files* report and never
fabricates — TIFF is forced per-tile with z detected, but TIFF channels and
unreadable/non-uniform data route to the operator. The prefilled rows then drop
out of the interview's gap list automatically.

Rows that **don't** come from screen.yaml:

- `SBS_CHANNEL_ORDER` / `PHENOTYPE_CHANNEL_ORDER` — prefilled by
  `probe_preprocess` (above) from the raw files; for ND2 multichannel this is
  `null` (channels embedded). The biological names in screen.yaml are for
  feature labeling, not file lookup.
- `TEST_PLATE` / `TEST_WELL` / `TEST_TILE` — `auto-probe` from the
  preprocess metadata. Use
  `select_test_tile_from_metadata(analysis_dir, phase, n_tiles=1)` which
  reads
  `brieflow_output/preprocess/metadata/<phase>/<plate>/<well_row>/<well_col>/combined_metadata.parquet`
  and picks an interior tile in a random well. The helper returns
  `{plate, well, tiles, well_center_um, well_radius_um, rationale}`.
  Pass `well=<...>` to pin a specific well or `seed=<int>` for
  reproducible random selection. Use the raw-ND2 `select_test_tile(nd2_path)`
  only when preprocess output doesn't exist yet (e.g. brieflow-setup
  walks).

If a row needs operator judgment that screen.yaml can't supply (e.g.
"are these channel-name regex matches correct?"), the template marks it
`bucket=user` — that's the row you ask via `AskUserQuestion`.

### Two paths

**A) Interview file doesn't exist — bootstrap it.**

```python
from brieflow_wizard import init_screen_interview
import yaml
screen = yaml.safe_load(open(f"{analysis_dir}/screen.yaml"))
screen_id = screen.get("collection", {}).get("screen_id") or Path(analysis_dir).parent.name

init_screen_interview(
    template_path=f"{plugin_dir}/data/interview_template.json",
    dest_path=f"{analysis_dir}/.brieflow/interview.json",
    screen_id=screen_id,
    # screen_yaml_path defaults to `<dest_dir>/../screen.yaml` — pass explicitly only if non-standard
)
```

`init_screen_interview` reads the template + the screen's screen.yaml,
applies every row's `derivation` block to pre-fill from screen.yaml,
and writes `<analysis-dir>/.brieflow/interview.json`. Returns a dict
summarizing the bootstrap (`{"dest": Path, "applied": [...], "missing":
[...]}`). The `missing` entries are template-referenced screen.yaml
paths that are absent/null in this screen's file (catches "field got
renamed" silent breaks) — surface them as `wizard-findings/v1
kind=screen-yaml-path-missing`.

**B) Interview file exists — check completeness.**

```python
from brieflow_wizard import interview_completeness, load_interview, find_param
state = interview_completeness(analysis_dir, phase)
# state = {
#   "filled":   list[str],   # param names that have a non-null value AND no bucket drift
#   "missing":  list[str],   # required (user / user_tuned_seed) and value is null
#   "stale":    list[str],   # value is set but template schema drifted since
#   "orphaned": list[str],   # interview row exists but template no longer has it
# }
# Lists hold bare param-name strings (NOT row dicts). To inspect a row's full
# metadata (bucket, derivation, notes, current value), look it up explicitly:
iv = load_interview(f"{analysis_dir}/.brieflow/interview.json")
for name in state["missing"]:
    row = find_param(iv, name, notebook=phase)
    # row has keys: notebook, param_name, bucket, default, derivation_source, notes, value, ...
```

### After either path

**Walk every row in `missing` + `stale`** for `<phase>` via
`AskUserQuestion`, one prompt per row (no batch-fill — operators miss
bad inferences in walls of text). Then `save_interview`.

If a row STILL has `bucket=auto + value=null` after the bootstrap +
walk AND the notebook's marker block defaults to `None`, that's a
**canonical-template-gap**: the interview template is missing a
`derivation` for it (or the screen.yaml is missing the
field the mapping points to, or the operator answer was skipped). Log
`wizard-findings/v1 kind=canonical-template-gap` with the row name + the
expected screen.yaml field (if you can infer it from context) and STOP.
Do NOT silently hardcode the value in the notebook — the next screen
will hit the same gap and the operator won't see why.

### Auto-mode warning, gated

If any `AskUserQuestion` will fire in this step OR Step 6 (QC-failure
variation confirmations), first print:

> "This phase uses AskUserQuestion to confirm parameter values. Auto mode
> auto-resolves these to the Recommended option without showing them to
> you. Please disable auto mode (Esc, toggle off) before we proceed."

This prompt is **mandatory even under auto mode** — it's the operator's
chance to redirect a sweep before it heads down a bad axis or to catch
an inferred value that's wrong for this screen. Wait for explicit
operator confirmation before proceeding. Re-enable auto mode after the
phase completes.

## Step 1b — Review the resolved parameters with the operator

Before patching anything in, show the operator how **this phase's** parameters
resolved — a lot get set at once, so let them double-check provenance and catch a
wrong screen.yaml derivation or lab default early:

```python
from brieflow_wizard import load_interview, interview_review, render_interview_review
iv = load_interview(f"{analysis_dir}/.brieflow/interview.json")
print(render_interview_review(interview_review(iv, notebook="<phase>")))
```

Lead with the `⛔ Needs your answer` and `📝 From your notes` sections (confirm any
prose-derived overrides → `set_value(..., source="operator")`). The operator
eyeballs the rest (from screen.yaml / lab default) and flags anything off before
you proceed. Skip only if the operator explicitly opts out.

## Step 2 — Push interview values into the notebook's marker blocks

```python
from brieflow_wizard import load_interview, patch_marker_block_value

interview = load_interview(f"{analysis_dir}/.brieflow/interview.json")
nb_path = f"{analysis_dir}/<N>_<phase>.py"

for p in interview["params"]:
    if p["notebook"] != phase: continue
    if p["bucket"] in ("archival", "output", "tuned-output", "nonexistent"): continue
    if p["value"] is None or p["value_source"] is None: continue
    try:
        patch_marker_block_value(nb_path, p["param_name"], repr(p["value"]))
    except (ValueError, KeyError):
        pass  # bucket=auto rows with no marker block — that's fine
```

`patch_marker_block_value` is case-insensitive (interview's lowercase
`threshold_reads` matches the notebook's uppercase `THRESHOLD_READS`) and
preserves the file's case convention in the rewrite.

**Ordering precondition for Step 4**: all `patch_marker_block_value`
calls must complete before `start_marimo_kernel` opens the file. The
kernel reads the `.py` file exactly once at startup; any patch issued
after Step 4 writes values the live kernel will never see. For
in-session mutation use `ctx.edit_cell` (Step 6).

**The screen.yaml → interview mapping is not in this SKILL** — it lives
in `<plugin>/data/interview_template.json` as `derivation`
metadata on each row, and `init_screen_interview` applies it at Step 1.
Derivation types are listed in `<plugin>/scripts/README.md` ("Where values
come from"). If you encounter a `bucket=auto +
value=null` row at Step 1 with no `derivation` and no operator
answer captured, that's a template gap to log + escalate (per Step 1's
canonical-template-gap rule), not a value to hardcode in the notebook.

## Step 3 — Preflight the notebook (static check, fast)

```python
from brieflow_wizard import preflight_notebook, expected_operator_params
expected = expected_operator_params(analysis_dir, phase)
report = preflight_notebook(nb_path, expected, conda_env=conda_env)
```

If `report["ok"] is False`, STOP — the canonical notebook itself has a
defect (marker gap, cross-cell collision, missing markers). Log a
`wizard-findings/v1 kind=canonical-template-gap` finding and exit. The
fix lives at `<screen-root>/<canonical-notebook-dir>/<phase>.py`, not in
this analysis notebook.

### Step 3b — Segmentation-model deps (phenotype only, gated)

The base env (setup S6) deliberately omits the heavy, model-specific
segmentation deps. Install them **only when the interview lands on that model**,
**once**, into the screen's conda env, before starting the kernel:

```python
seg = find_param(interview, "SEGMENTATION_METHOD", notebook=phase)
model = find_param(interview, "CELLPOSE_MODEL", notebook=phase)
# cpsam (cellpose-SAM) — e.g. the RPE1 screen's irregular cells
if model and str(model.get("value")).lower() == "cpsam":
    run: uv pip install cellpose==4.0.4 torch==2.7.0 torchvision==0.22.0
# stardist path (same pattern)
elif seg and str(seg.get("value")).lower() == "stardist":
    run: uv pip install "numpy<2.0" stardist==0.9.1 csbdeep==0.8.1
```

Run the `uv pip install` in the screen's conda env (idempotent — a no-op if
already satisfied). Default cyto3 needs nothing extra (brieflow base deps cover
it). Skip this step for non-phenotype phases.

## Step 4 — Start the kernel

```python
from brieflow_wizard import start_marimo_kernel
pid, port, session_id = start_marimo_kernel(
    nb_path, conda_env=conda_env, watch=False,
)
```

The file-on-disk and the live kernel state are kept in sync at exactly
two checkpoints, and never in between:

1. **At kernel startup** — Step 2 has already patched the marker blocks
   on disk; the kernel reads the patched file once when it boots.
2. **At HTML export (Step 8)** — `export_phase_html` calls `ctx.save()`
   to flush the in-kernel winners back to disk before the export.

Between those two checkpoints, all parameter mutation is API-driven:
`ctx.edit_cell` + `ctx.run_cell` via the scratchpad (Step 6). This is
what makes the loop fast (only downstream cells re-run) and portable
across filesystems.

**Hard stop**: do not edit the `.py` file while the kernel is running.
Use `ctx.edit_cell` for in-session changes; the disk file is the
operator-visible artifact at start and export, not a control surface
during the sweep.

Verify the session bound (the WS holder must be alive):

```bash
curl -s http://127.0.0.1:<port>/api/sessions
# expect: {"<session_id>": {"filename": "...", "path": "..."}}
```

If empty, the holder died — check the holder log at
`<analysis-dir>/.brieflow/_ws_holder.<port>.log`.

### Marimo context model — `ctx.cells` vs `ctx.graph.cells`

Inside an `async with cm.get_context() as ctx:` block, two distinct cell
collections exist. Use the right one:

| Attribute | What it holds | When to use |
|---|---|---|
| `ctx.cells` | `_CellsView` of all NOTEBOOK cells (keyed by 4-char cell IDs like `Hbol`, `MJUe`) | Enumerate notebook cells, look up a cell by ID for `run_cell` / `edit_cell` |
| `ctx.globals` | Notebook global namespace; populated after cells run | Check that a variable like `df_reads` exists post-run |
| `ctx.graph.cells` | Just the currently-executing SCRATCHPAD cell — typically a single ID like `lgaB` | Scratchpad introspection only; never use to enumerate notebook |

Mistaking `ctx.graph.cells` for the notebook cells produces "only 1 cell"
debugging confusion (looks like the notebook never loaded). Always
enumerate via `ctx.cells.keys()`.

Cell-state operations (`ctx.run_cell(target)`, `ctx.edit_cell(target,
code=...)`) require the `async with` form:

```python
async with cm.get_context() as ctx:
    ctx.run_cell(cell_id)
```

Bare `ctx.run_cell(...)` outside an `async with` block silently no-ops.

`ctx.edit_cell` raises `StaleCellError` on a cell whose `.code` this kernel
has not read since it last changed, so read `ctx.cells[cell_id].code` before
editing it (`run_marimo_sweep` and `find_marker_cell` already do).

## Step 5 — Install tees + capture baseline

Library functions in `lib.sbs.*` / `lib.phenotype.*` emit QC + mapping
metrics via `print(...)`. To capture them, install per-module tees on
the kernel side:

```python
# In execute-code.sh against the running kernel:
import asyncio, nest_asyncio
nest_asyncio.apply()
import sys; sys.path.insert(0, '<scripts-dir>')   # value from Step 0's $SCRIPTS_DIR
from brieflow_wizard import setup_kernel_tees
import marimo._code_mode as cm

log_path = f"{analysis_dir}/.brieflow/{phase}.tee.log"
open(log_path, 'w').close()  # truncate per session
asyncio.run(setup_kernel_tees({"ctx_factory": cm.get_context}, log_path))
```

`setup_kernel_tees` is idempotent (sentinel-based de-dup) — safe to call
multiple times across a session.

Marimo runs the DAG automatically on initial kernel start, but you may
need to trigger any stale cells explicitly:

```python
async def _trigger_all_stale():
    async with cm.get_context() as ctx:
        stale = [c.id for c in ctx.cells if str(getattr(c, 'status', '')).rsplit('.', 1)[-1] == 'stale']
        for cid in stale: ctx.run_cell(cid)
asyncio.run(_trigger_all_stale())
```

**Wait for baseline to fully settle** — the tee log will grow as cells
print. The reliable settle signal is the count of `SINGLE_MAP_RATE` (sbs
/ phenotype) or analogous target-metric lines in the tee log. Don't rely
solely on cell-status polling — there's a race window between "upstream
cell finishes" and "downstream cell marked stale" where all cells
briefly look idle.

## Step 6 — Triage QC sequentially, then optimize mapping (SBS only)

QC failures have a natural ordering: alignment is upstream of
segmentation, segmentation is upstream of mapping. Fix them in that
order — don't try to tune `THRESHOLD_READS` when the alignment is
broken; you'll be optimizing on garbage.

The library prints two QC blocks per cell run:

```
Alignment QC:
  cycle_dapi_shift_residual_max_px:           X.XXXX  (pass < 1.0)
  intra_cycle_channel_shift_residual_max_px:  X.XXXX  (pass < 1.0)
Segmentation QC:
  cells_with_1_nucleus_frac:                  0.XXXX  (pass > 0.95)
  cell_area_cv:                               0.XXXX  (pass < 0.6)
  mean_nuclear_solidity:                      0.XXXX  (pass > 0.9)
SINGLE_MAP_RATE: 0.XXXX  (n_total=N)   # SBS only
ANY_MAP_RATE:    0.XXXX  (n_total=N)   # SBS only
```

The `(pass <op> threshold)` annotation IS the threshold — never hardcode.
Use `parse_qc_block(stdout, baseline=<baseline_dict>)` which handles
inheritance under reactive recompute (absent metrics carry forward from
baseline as PASS; only label FAIL when a metric is PRESENT and below its
printed threshold). With no QC metric printed or inherited it fails closed
(`qc_missing`): the library prints these blocks only when the notebook passes
`verbose=True`, so restore that argument rather than accepting the tile.

### Step 6a — Alignment QC

Failing keys: `cycle_dapi_shift_residual_max_px`,
`intra_cycle_channel_shift_residual_max_px` (SBS),
`phenotype_channel_shift_residual_max_px` (phenotype).

If all alignment metrics PASS → go to 6b.

If any fail:

- SBS metrics → sweep `WINDOW` ∈ {1, 2, 3, 4}, then `UPSAMPLE_FACTOR` ∈
  {1, 2, 4}. One axis at a time (see "How to vary" below).
- Phenotype `phenotype_channel_shift_residual_max_px` → first confirm
  `PHENOTYPE_TARGET_CHANNEL` / `PHENOTYPE_SOURCE_CHANNEL` are correct
  (chromatic aberration is usually optics-level — wrong channel
  selection is the recoverable case). If channels are right and the
  metric still fails, the dataset has an alignment problem that
  parameter tuning won't fix.
- Last resort for SBS: `MANUAL_BACKGROUND_CYCLE_INDEX`,
  `MANUAL_CHANNEL_MAPPING`.

If after sweeping you can't get alignment to pass → `AskUserQuestion`:

> "Alignment QC still failing after sweep — metric X = Y, threshold Z.
> Best parameters tried: ... . How would you like to proceed?"

Options: `[Accept with warning]` `[Open marimo to inspect interactively]`
`[Abort phase]`. Don't push forward into segmentation/mapping on
unreliable alignment.

### Step 6a' — Render every alignment view before accepting a phase

The printed residuals summarize; the overlays show which cycle, channel or tile pair is
off. The notebook's display selectors (lowercase marimo UI, no interview rows) draw a
default subset. When the agent drives a notebook, it renders **all** of them from the
scratchpad, writes each figure under `<analysis_dir>/.brieflow/alignment_views/<phase>/`,
and opens every PNG before it accepts the phase (sbs, phenotype or merge):

```bash
bash $EXECUTE_CODE_SH --url http://127.0.0.1:$PORT --session $SESSION_ID <<'EOF'
from pathlib import Path
out = Path(analysis_dir) / ".brieflow" / "alignment_views" / "sbs"
out.mkdir(parents=True, exist_ok=True)
fig = plot_cycle_alignment_overlay(
    aligned, CHANNEL_NAMES, cycles="all", cycle_labels=kept_cycle_numbers
)
if fig is not None:
    fig.savefig(out / "cycles_all.png", dpi=110)
fig = plot_flagged_channel_overlays(
    aligned, CHANNEL_NAMES, cycles="all", channels="all", cycle_labels=kept_cycle_numbers
)
if fig is not None:
    fig.savefig(out / "channels_all.png", dpi=110)
EOF
```

- **SBS**: the two calls above. `plot_cycle_alignment_overlay` draws one row per cycle
  (DAPI of cycle k on cycle 1, and cycle k's spots on the other cycles' spots);
  `plot_flagged_channel_overlays` with `channels="all"` adds a panel for every base
  channel of every cycle to the flagged ones.
- **Phenotype**: when `ALIGN` is on, call `plot_phenotype_alignment_overlay` with the
  arguments of the notebook's overlay cell (its image and channel-name dict are
  cell-local, so rebuild them in the scratchpad); otherwise `plot_phenotype_channel_overlay`.
- **Merge, `MERGE_APPROACH = "fast"`**: every tile-site pair, not only the selector's first. Select every
  option of `merge_overlay_pairs`, or loop `final_pairs` and call
  `plot_merge_alignment_overlay(sbs_dapi, ph_dapi, initial_alignment_df, ph_tile, sbs_site)`
  with the images from `load_merge_dapi_pair`, as the notebook's overlay cell does.
- **Merge, `MERGE_APPROACH = "positions"`**: select `all` in both selectors of "Check the positions
  placement on the images" (`positions_overlap_pairs`, `positions_in_sbs_pairs`), or draw
  every candidate from the scratchpad: `overlay_candidates(positions_placement, counts)`
  gives `"tile_overlaps"` and `"phenotype_in_sbs"`; `overlay_image_paths` gives the label
  and image paths for their tiles; pass all of them to `plot_tile_overlaps` and
  `plot_phenotype_in_sbs`, as the notebook's overlay cell does. Read the printed
  `positions_qc` as well: `*_overlap_agreement`, `*_best_orientation` and
  `*_orientation_warning`. A set orientation warning is a finding, and the best orientation
  is proposed for `FLIPUD`/`FLIPLR`/`ROT90` in the launch review, never applied. In a
  pipeline run the fit status is only in the `positions_merge` rule log (its status line
  and `WARNING:` lines).

Read the panels: white or grey means aligned; magenta and green fringes mean a shift
(the titles give the measured shift in pixels and mark off cycles and channels). A
nucleus or spot found in one image only stays fully magenta or green; that is not a
shift. Check that the views agree with each other and with the printed QC: a cycle the
residual table passes but whose overlay fringes is a finding, and so is the reverse.

For every off cycle, channel or tile pair, call `record_finding()` with
`kind="alignment-view"`, the phase, the cycle/channel/pair and its shift, and the PNG
path as evidence. A persistently off SBS cycle is a candidate for `SKIP_CYCLES`:
**propose it through the launch review** (name the cycle, the shift and the PNG, and let
the operator change it via "Change something"); never apply `SKIP_CYCLES` yourself.

### Step 6b — Segmentation QC

Failing keys: `cells_with_1_nucleus_frac`, `cell_area_cv`,
`mean_nuclear_solidity`.

If all pass → 6c (or, for non-SBS phases, skip to Step 7).

If any fail, sweep one of these (one axis at a time):

- `NUCLEI_FLOW_THRESHOLD`, `CELL_FLOW_THRESHOLD`
- `NUCLEI_CELLPROB_THRESHOLD`, `CELL_CELLPROB_THRESHOLD`
- `NUCLEI_DIAMETER_INPUT`, `CELL_DIAMETER_INPUT` (set to int to override
  cellpose's auto-estimate)

If after sweeping segmentation still fails → same operator escalation
pattern as 6a: `AskUserQuestion` with accept/inspect/abort.

### Step 6c — Mapping rate optimization (SBS only)

Skip this step for phenotype / merge / aggregate / cluster — they have
no mapping-rate metric. Confirm baseline QC passes, then go to Step 7.

For SBS: once alignment + segmentation both pass, optimize
`SINGLE_MAP_RATE` by sweeping:

- `THRESHOLD_READS` ∈ {50, 100, 200, 400, 800, 1600}
- `PEAK_WIDTH` ∈ {3, 5, 7, 9}

Sweep one axis at a time (typically `THRESHOLD_READS` first, then
`PEAK_WIDTH` at the best `THRESHOLD_READS`). `SINGLE_MAP_RATE` is the
target to maximize; `ANY_MAP_RATE` is reporting-only.

**Budget expansion when confident**: the values above are starting
palettes. If a sweep shows the target metric is monotonically improving
toward one edge of the range, extend further in that direction (e.g.
`THRESHOLD_READS=800` was the best of {50, 100, 200, 400, 800} → try
{1600, 3200, 6400}). If the metric is concave, refine around the peak
(e.g. `PW=7` best of {3, 5, 7} → try {7, 9, 11}). Stop when the metric
plateaus or starts to drop.

**Don't vary upstream-of-mapping parameters here.** If alignment +
segmentation passed at baseline, leave their markers alone. Touching
`UPSAMPLE_FACTOR` or cellpose flow thresholds invalidates the cellpose
cache and re-runs ~3 minutes of GPU work for no mapping-rate gain.

### How to vary, mechanically (applies to 6a, 6b, 6c)

Use `run_marimo_sweep`. It locates the marker cell via `find_marker_cell`,
rewrites the assignment line via `ctx.edit_cell` inside `async with`,
calls `ctx.run_cell` to trigger reactive recompute, and waits for the
DAG to settle — once per candidate value. Returns a pandas DataFrame
with `(threshold, single_rate, any_rate, qc_pass, elapsed_s)` columns
and (if `out_log` is given) appends each row to a TSV as it goes.

The whole sweep runs in-kernel via the scratchpad — pipe the code in
through `execute-code.sh`:

```bash
bash $EXECUTE_CODE_SH --url http://127.0.0.1:$PORT --session $SESSION_ID <<'EOF'
import asyncio, nest_asyncio
nest_asyncio.apply()
import sys; sys.path.insert(0, '<scripts-dir>')   # value from Step 0
from brieflow_wizard import run_marimo_sweep
import marimo._code_mode as cm

df = asyncio.run(run_marimo_sweep(
    session={"ctx_factory": cm.get_context},
    param_name="THRESHOLD_READS",
    values=[50, 100, 200, 400, 800, 1600],
    out_log=f"{analysis_dir}/.brieflow/sweep_THRESHOLD_READS.tsv",
    prints_log=f"{analysis_dir}/.brieflow/sbs.tee.log",
    timeout_s=600.0,
))
print(df.to_string())
EOF
```

This is the marimo-pair pattern — scratchpad-driven `ctx.edit_cell` +
`ctx.run_cell` against the live reactive kernel. The same call shape
works for alignment, segmentation, and mapping sweeps; only `param_name`
and `values` change.

### Detecting sweep failures

Before acting on the returned DataFrame, validate its shape:

- **Every `single_rate` (or the phase's target metric) is `None`** — the
  marker cell was found but the target metric never reached
  `prints_log`. Re-check Step 5: tees installed against the right
  modules, `prints_log` points at `<phase>.tee.log`.
- **Every `qc_pass` is `False` with the *same* failing key** — upstream
  is broken (alignment or segmentation), not the swept parameter. Return
  to 6a/6b before tuning further.
- **Every `elapsed_s` is implausibly short (< ~5s) AND values match
  baseline** — `ctx.run_cell` didn't invalidate downstream cells.
  Verify with `find_marker_cell(ctx, param_name)` that the marker cell
  ID exists, and confirm the swept variable is actually referenced by a
  downstream cell.
- **`elapsed_s` keeps growing past `timeout_s` on every iter** — the
  per-iter budget is too small. Raise `timeout_s` and re-run only the
  affected values.

**Budget expansion**: same rules as before. If the metric trends to one
edge, extend `values` further; if concave, refine around the peak. Just
re-call `run_marimo_sweep` with the new value list — the previous TSV
rows stay.

### Notebook-design rule (canonical-author guidance)

Operator-parameter cells should expose ONE tunable variable per
`@app.cell` — never bundle a tunable param with a high-reach derivation
in the same cell. Marimo's reactive granularity is the cell, not the
variable: when a cell's source changes (even just a comment), marimo
invalidates ALL the cell's outputs. If the cell also defines a variable
upstream of segmentation, varying the operator param invalidates
cellpose's cache.

Example fix (in the canonical at `<screen-root>/<canonical-notebook-dir>/<phase>.py`):

```python
# BAD — bundles PEAK_WIDTH with EXTRA_CHANNEL_INDICES (upstream of seg):
@app.cell
def _(CHANNEL_NAMES, EXTRA_CHANNELS):
    PEAK_WIDTH = 5
    EXTRA_CHANNEL_INDICES = [CHANNEL_NAMES.index(c) for c in EXTRA_CHANNELS]
    return (EXTRA_CHANNEL_INDICES, PEAK_WIDTH, ...)

# GOOD — operator-param cell isolated; derivation in its own cell:
@app.cell
def _():
    PEAK_WIDTH = 5
    return (PEAK_WIDTH, ...)

@app.cell
def _(CHANNEL_NAMES, EXTRA_CHANNELS):
    EXTRA_CHANNEL_INDICES = [CHANNEL_NAMES.index(c) for c in EXTRA_CHANNELS]
    return (EXTRA_CHANNEL_INDICES,)
```

If you spot a violation during a phase run, log
`wizard-findings/v1 kind=canonical-cell-bundling` with the cell line
range — surfacing to the canonical author is the right escalation.

## Step 7 — Multi-tile validation

After the winning marker set is identified, validate across additional
tiles. Each tile change invalidates everything upstream (TEST_TILE is
upstream of alignment / cellpose / etc.), so each validation iter does
the full ~3-5 min DAG. That's the expected cost of cross-tile
validation.

**Default path — parallel headless subprocesses.** Use
`validate_tiles_headless`, which runs N tile-copies of the notebook as
isolated `marimo.App.run()` script subprocesses with the winning markers
baked in. The live marimo kernel stays untouched on the operator's
`TEST_TILE`, so failure isolation is automatic and wall-clock is
~max-tile-time, not sum-of-tile-times. Pair with
`select_validation_tiles_from_metadata` for cross-well /
cross-plate coverage (different sampling target than Step 1's
`select_test_tile_from_metadata`, which intentionally samples within one
well for the operator's test tile).

```bash
bash $EXECUTE_CODE_SH --url http://127.0.0.1:$PORT --session $SESSION_ID <<'EOF'
import asyncio, nest_asyncio
nest_asyncio.apply()
import sys; sys.path.insert(0, '<scripts-dir>')
from brieflow_wizard import (
    validate_tiles_headless,
    select_validation_tiles_from_metadata,
)

# Operator-pinned in screen.yaml takes precedence; else cross-well auto-sample.
tiles = screen.get("validation_tile_pool") or select_validation_tiles_from_metadata(
    analysis_dir, phase, n_tiles=5, min_plates=1, min_wells=3,
)["tiles"]

df = asyncio.run(validate_tiles_headless(
    nb_path=f"{analysis_dir}/{N}_{phase}.py",
    winners={"THRESHOLD_READS": 800, "PEAK_WIDTH": 7},  # the winners
    tiles=tiles,
    work_dir=f"{analysis_dir}/.brieflow/parallel",
    conda_env=conda_env,
    omp_threads=4,
    max_parallel=4,
    timeout_s=900.0,
    keep_artifacts=True,  # per-tile .py + .log preserved for post-hoc inspection
    # export_html=True,   # also produce per-tile .html for viz inspection
))
print(df.to_string())
EOF
```

The returned DataFrame has columns
`(plate, well, tile, single_rate, any_rate, qc_pass, qc_failed_keys, elapsed_s, exit_code, error, log_fp)`.
Subprocess crashes / timeouts surface as rows with `error` set — the
helper never raises on per-tile failures, so the Step-7 acceptance
logic below can classify cleanly.

**Per-tile HTML (opt-in).** Set `export_html=True` to swap the
subprocess command from `python <copy>.py` to
`marimo export html <copy>.py -o <copy>.html` — same DAG runs, HTML is
produced as a side effect (~150 KB per tile). Use this when a
validation tile flags a borderline or anomalous QC value (e.g.
alignment residual near threshold) and you want to inspect the overlay
viz cells without re-running the DAG by hand. PASS/FAIL acceptance
still comes from the DataFrame; the HTML is for diagnosis.

**Fallback — live-kernel serial.** When the operator wants to
validate-debug a single tile interactively (e.g. step through a
failure inside the marimo UI), use `validate_across_tiles` instead:

```bash
bash $EXECUTE_CODE_SH --url http://127.0.0.1:$PORT --session $SESSION_ID <<'EOF'
import asyncio, nest_asyncio
nest_asyncio.apply()
import sys; sys.path.insert(0, '<scripts-dir>')
from brieflow_wizard import validate_across_tiles, select_test_tile_from_metadata
import marimo._code_mode as cm

tiles = screen.get("validation_tile_pool") or select_test_tile_from_metadata(
    analysis_dir, phase, n_tiles=3, well=TEST_WELL
)["tiles"]

df = asyncio.run(validate_across_tiles(
    session={"ctx_factory": cm.get_context},
    param_dict={"THRESHOLD_READS": 800, "PEAK_WIDTH": 7},
    tiles=tiles,
    prints_log=f"{analysis_dir}/.brieflow/{phase}.tee.log",
    timeout_s=600.0,
))
print(df.to_string())
EOF
```

The serial path mutates the live kernel (`ctx.edit_cell` on `TEST_TILE`),
so it inherits any stale-cell state from the sweep, runs serially, and
requires a `TEST_TILE` restore at Step 8. Prefer the headless path
unless interactively debugging.

**Acceptance**: each tile must have `qc_pass == True`. The **±5% target-metric**
clause applies *only when the phase emits a quantitative scalar target*:

- `sbs` → `SINGLE_MAP_RATE` (mapping rate). Acceptance = `qc_pass AND
  abs(single_rate − test_tile_single_rate)/test_tile_single_rate ≤ 0.05`.
- `phenotype` / `merge` / `aggregate` / `cluster` → no scalar target.
  Acceptance is `qc_pass == True` only; `single_rate` will be `None`
  by design and is not an error.

`validate_tiles_headless` reflects this: on non-SBS phases (notebook
filename not ending in `_sbs.py`) the helper does NOT mark
`"SINGLE_MAP_RATE not found"` as an error.

Branch on the number of failing tiles:

- **0 failing tiles** — accept the winners, proceed to Step 8.
- **1 failing tile in a 3-tile sample** — surface
  `{tile, metric, qc_failed_keys}` via `AskUserQuestion` (accept
  variability / re-tune / abort). The operator decides.
- **≥ 50% failing tiles** — do NOT prompt per tile. The test-tile result
  isn't representative of the well. Log
  `wizard-findings/v1 kind=test-tile-not-representative` with the tile
  list and metrics, and STOP. The operator picks a different
  `TEST_TILE` and re-runs from Step 5.

Don't auto-revert the winner — the operator decides whether to accept
the cross-tile variability or re-tune.

## Step 8 — Export HTML + stop the kernel

```python
import asyncio, nest_asyncio
nest_asyncio.apply()
from brieflow_wizard import export_phase_html, stop_marimo_kernel
try:
    asyncio.run(export_phase_html(
        nb_path,
        f"{analysis_dir}/.brieflow/{phase}.final.html",
        conda_env=conda_env,
        session={"ctx_factory": cm.get_context},
    ))
finally:
    stop_marimo_kernel(pid)
```

Always stop the kernel in a `finally` block. If the export raises, the
in-kernel state is lost regardless; leaving the WS holder alive only
leaks the port (blocking the next phase run) and the holder PID.

**Test-tile state at export time.** If Step 7 used the default headless
path (`validate_tiles_headless`), the live kernel never moved off the
operator's `TEST_TILE` and the HTML naturally anchors to that tile —
nothing to restore. If Step 7 used the fallback `validate_across_tiles`,
patch `TEST_TILE` back to the operator's original choice via
`ctx.edit_cell` and re-trigger the downstream cells (one full ~3-5 min
DAG) before calling `export_phase_html`.

Point the operator at the HTML file.

### Hand-off back to the router

The presence of `<analysis-dir>/.brieflow/<phase>.final.html` is the
router's signal that this phase is configured AND swept/validated AND
accepted by the operator. After Step 8 completes:

1. Return to `brieflow-auto` (the router).
2. The router will re-run state-detect and **proactively suggest the
   pipeline launch for THIS phase** — `brieflow-module --phases <phase>`.
3. **Do NOT proceed to the next phase's notebook** until this phase's
   pipeline run completes. The next notebook needs THIS phase's
   pipeline outputs to be configured properly:
   - sbs / phenotype notebooks read preprocess outputs
   - merge notebook reads sbs + phenotype outputs
   - aggregate notebook reads merge outputs
   - cluster notebook reads aggregate outputs
   Skipping ahead leaves the next notebook with no data to inspect for
   its operator-input rows (`DET_RANGE`, `INITIAL_SITES`, aggregation
   keys, cluster parameters).

## Wizard helper API (Python)

All in `<plugin>/scripts/brieflow_wizard.py`:

**Interview I/O**: `init_screen_interview`, `load_interview`,
`save_interview`, `filter_params`, `find_param`, `set_value`,
`is_active`, `interview_completeness`

`set_value(interview, param_name, value, source, notebook=None)` —
`source` must be one of the literal strings
`{'operator', 'default', 'auto-probe', 'gen', 'tuned-confirmed', 'screen-yaml'}`.
For values the operator just confirmed via `AskUserQuestion`, use
`'operator'` (NOT `'operator-confirmed'` — that's not a valid source).
Use `'tuned-confirmed'` only for values pulled from
`.brieflow/tuned_<phase>.json` after a sweep, then re-confirmed by the
operator. `find_param` and `set_value` are case-sensitive on
`param_name`; pass the exact form from the template (case mismatches now
surface a "did you mean" hint in the error message).

**Pre-edit scanners**: `preflight_notebook`, `scan_marker_blocks`,
`verify_marker_coverage`, `scan_variable_collisions`,
`dry_export_notebook`, `expected_operator_params`

**Auto-probe**: `detect_data_format`, `detect_organization`,
`detect_z_planes`, `detect_round_order`, `infer_path_pattern`,
`derive_metadata_order_type`. Tile-selection helpers — distinct
sampling targets:
- `select_test_tile_from_metadata` — within-well sampling for the
  operator's `TEST_TILE` (Step 1 auto-probe).
- `select_validation_tiles_from_metadata` — cross-well /
  cross-plate sampling for multi-tile validation (Step 7).
- `select_test_tile` — raw ND2 fallback for pre-preprocess use cases.

**Marker patching (pre-kernel / post-stop only)**: `patch_marker_block_value`
writes to the `.py` file on disk; valid before Step 4 (`start_marimo_kernel`)
and after Step 8 (`stop_marimo_kernel`) on the live notebook. For in-session
mutation on the LIVE kernel use `run_marimo_sweep` /
`validate_across_tiles`. The headless validator (`validate_tiles_headless`)
patches notebook COPIES under `work_dir` — those COPIES are not opened in
any kernel, so `patch_marker_block_value` is unrestricted there. Companion
helpers: `tuned_export_path`, `pull_tuned_values`, `record_tuned_confirmed`.

**Marimo kernel + headless runners**:
- `start_marimo_kernel(notebook_path, watch=False)` — `watch=False` is
  the contract for this SKILL (see Step 4's two-checkpoint sync).
- `stop_marimo_kernel`, `setup_kernel_tees` (idempotent),
  `parse_qc_block`, `export_phase_html`.
- `run_marimo_sweep` — in-kernel parameter sweep (Step 6).
- `validate_across_tiles` — in-kernel serial validation (Step 7 fallback).
- `validate_tiles_headless` — parallel subprocess validation (Step 7
  default). Pairs with `select_validation_tiles_from_metadata`.

**Phase orchestration**: `audit_canonical_notebooks`,
`confirm_channel_mapping`, `record_channel_mapping`,
`confirm_tuned_loop`, `verify_phase_outputs`, `reset_phase`

## Scope guards

- **Only edit assignments inside `params_*` cells** (older screens: inside the
  `# === OPERATOR PARAMETERS ===` fences). Never touch derivation cells, viz cells, or
  config-write cells.
- **Never read another screen's `config.yml` or `interview.json`** —
  that's migration, which is out of scope for the wizard.
- **Do not commit.** The router handles commits after the full pipeline pass.
- **Don't write `.py` / `.sh` iteration drivers.** Use the primitives
  above. If a helper you need is missing, log
  `wizard-findings/v1 kind=plugin-helper-missing` and STOP — do not
  improvise.
- **Log findings with `record_finding()`** (`scripts/brieflow_findings.py`,
  exported by `brieflow_wizard`). Every `wizard-findings/v1` row above goes
  through it, and so does every workaround made in place (a `screen.yaml`
  edit, a sticky interview-row rewrite, a renamed channel): router rule R5.

## References (load on demand)

- `references/wizard_workflow.md` — original protocol with examples + edge cases

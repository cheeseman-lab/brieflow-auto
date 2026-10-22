# Wizard workflow (edit-in-place against marimo notebooks)

How a Claude Code session sets up a brieflow screen. The wizard is not a CLI
tool — **you** (the agent) are the wizard. Two artifacts per screen:

```
<analysis-dir>/.brieflow/interview.json    # operator's answers + audit trail
<analysis-dir>/0_preprocess.py             # marimo notebooks, edited in-place
<analysis-dir>/1_sbs.py                    # by you inside params_* cells
<analysis-dir>/2_phenotype.py              # when running headless
<analysis-dir>/3_merge.py
<analysis-dir>/5_aggregate.py
<analysis-dir>/6_cluster.py
```

Each notebook keeps its operator-editable values in cells named `params_<topic>` (for example
`params_segmentation`) that hold only plain `NAME = value` assignments. **You only ever edit the
values inside those cells.** Everything else (derivation cells, viz cells, config-write cell) is
owned by the notebook. Screens created before the 0–7 renumbering use comment fences
(`# === OPERATOR PARAMETERS ===` … `# === END OPERATOR PARAMETERS ===`) instead; the helpers
treat both the same way.

Helpers live at `plugins/brieflow-auto/scripts/brieflow_wizard.py` for the
interview I/O. The notebook owns its own derivation and config writing.

## When to activate

The operator says any of:

- "set up brieflow for screen X" / "init screen X" / "drive the wizard for X"
- "/brieflow-setup X" (screen.yaml staged, interview not yet bootstrapped)
- "run the screen" / "launch the run for X" *and* `<analysis-dir>/.brieflow/interview.json`
  does not yet exist

## Bootstrap: copy the interview template + verify notebooks present

The plugin ships a stable JSON template at:

```
plugins/brieflow-auto/data/interview_template.json
```

On first activation, copy it into the screen's `.brieflow/` dir and stamp it
with the screen_id:

```python
from brieflow_wizard import init_screen_interview
init_screen_interview(
    template_path="<plugin>/data/interview_template.json",
    dest_path="<analysis-dir>/.brieflow/interview.json",
    screen_id="<screen-id>",
)
```

`init_screen_interview` refuses to overwrite an existing interview.json
unless `force=True` — protects in-flight wizard work.

Verify the marimo notebooks are present in the analysis dir
(`0_preprocess.py`, `1_sbs.py`, etc.). If they're missing, the operator
needs to copy them in from the brieflow-analysis checkout (or wherever the
canonical converted notebooks live).

## The per-phase flow

Walk pipeline phases in dependency order, one at a time:

```
preprocess → sbs → phenotype → merge → classify → aggregate → cluster → analyze
```

For each phase:

1. **Check interview completeness; collect only the unfilled prompts**

   Per SKILL.md Step 1, the skill never re-solicits answers that are already
   on disk. Compute the partial-fill state first:

   ```python
   from brieflow_wizard import (
       interview_completeness, load_interview, filter_params,
       is_active, set_value, save_interview,
   )
   state = interview_completeness(analysis_dir, phase)
   # → {"filled": [...], "missing": [...], "stale": [...]}
   ```

   - If `state["missing"]` and `state["stale"]` are both empty → this
     phase is fully filled. **Skip the interview entirely** and jump
     to step 3 (preflight); the agent is on a re-run.
   - Otherwise, walk **only** `state["missing"]` and `state["stale"]`
     in the next step. Do NOT touch rows in `state["filled"]`.

   `user` and `user_tuned_seed` are both operator-supplied at wizard
   time. The difference is downstream: `user` is the final value;
   `user_tuned_seed` is a starting value the notebook will refine.
   `filter_params` still gives you the full taxonomy if you need it
   for context:

   ```python
   i = load_interview("<analysis-dir>/.brieflow/interview.json")
   prompts = filter_params(i, notebook="preprocess", bucket=("user", "user_tuned_seed"))
   ```

2. **For each prompt in `state["missing"]` + `state["stale"]`, in CSV order** (skip the entire step if both lists are empty):
   - For `stale` rows, surface `(old_value, new_default)` side-by-side via `AskUserQuestion` with default-accept on the existing value; only overwrite when the operator explicitly picks the new value.
   - Check `is_active(prompt, i)` — if gating isn't satisfied yet, skip; revisit after the gating answer comes in.
   - **Auto-probe** what you can derive from the operator's already-given answers + filesystem state (see Auto-probe protocol below). When auto-probed, **STOP and surface the value inline** via `AskUserQuestion` (one accept/override question per prompt — see SKILL.md Step 1). Only after the operator confirms do you `set_value(..., source="auto-probe")`. **Do not batch-fill auto-probes silently and dump a final review table** — the operator can't catch a wrong inference in a wall of values, and the first reference pass W-001 demonstrates this fails in practice.
   - **Otherwise ask the operator.** Show the prompt's `notes` if non-trivial. `set_value(..., source="operator")` with the answer.
   - For `user_tuned_seed` prompts, frame the question as "starting value" — make
     it clear the notebook will refine via plot/grid-search and the operator
     will confirm the refined value at notebook-run time.
   - **Save after every value** (or at least every 3-5): `save_interview(i, path)`. The write is atomic.

2.5. **Channel reconciliation** (Required for preprocess phase; informational for others).

   After running `infer_path_pattern` against the actual files, you have
   `distinct_values["channel"]` — the literal channel tokens that appear in
   the filenames. These are the **ground truth** for `SBS_CHANNEL_ORDER` /
   `PHENOTYPE_CHANNEL_ORDER` (the dict keys brieflow uses for file lookup).

   Reconcile them against screen.yaml's biological labels:

   ```python
   regex_tokens = top_candidate["distinct_values"]["channel"]
   # screen.yaml side
   sbs_bio = [c["name"] for c in screen["iss"]["channels"]]                # e.g. ['DAPI', 'G', 'T', 'A', 'C']
   ph_bio  = [c["biological_name"] for c in screen["phenotype"]["channels"]]  # e.g. ['DAPI', 'COXIV', 'CENPA', 'WGA']
   ```

   Compare token count `N` against biological-label count `M`:

   - **`N == M` (size agrees)**: surface the positional mapping via
     AskUserQuestion. Example for an SBS phase:
     > "Regex found 5 channel tokens in SBS files:
     >   1. `Dapi_1p-CY3_30p_545-A594_30p-CY5_30p-CY7_30p`
     >   2. `CY3_30p_545`
     >   3. `A594_30p`
     >   4. `CY5_30p`
     >   5. `CY7_30p`
     >
     > screen.yaml lists 5 biological labels: `['DAPI', 'G', 'T', 'A', 'C']`.
     > Map positionally? token 1 → DAPI (c1 composite), token 2 → G (cycle bases), etc."

     On accept: `SBS_CHANNEL_ORDER = regex_tokens` (verbatim);
     `sbs_channels_metadata[].name = sbs_bio` (biological).

   - **`N != M` (size disagrees)**: STOP. Surface the discrepancy verbatim
     to the operator — DO NOT auto-resolve. Common causes:
     - **Composite c1 + split c2-c9** (counts coincidentally agree but
       composition is unusual): operator confirms the composite is the
       first entry.
     - **Some files missing channel-axis**: e.g. one file has DAPI only
       (1 token) while others have all 5. Operator picks reconciliation.
     - **Token includes laser/exposure suffix** operator didn't anticipate:
       operator decides verbatim or strip.

   **Hard rule:** `SBS_CHANNEL_ORDER` / `PHENOTYPE_CHANNEL_ORDER` come
   verbatim from `regex_tokens`. Never substitute biological labels
   (screen.yaml) or cleaned ND2 C-axis tokens. The first reference pass got
   this right; the second pass broke because cleaned ND2 metadata was
   silently substituted for the regex output — empty dict-lookup → crash.

3. **Preflight the phase notebook BEFORE editing.** (Per SKILL.md Step 3.)

   ```python
   from brieflow_wizard import preflight_notebook
   expected = [r["param_name"] for r in filter_params(i, notebook=phase, bucket=("user", "user_tuned_seed"))]
   report = preflight_notebook(
       notebook_path=<absolute-path-to-phase.py>,
       expected_operator_params=expected,
       conda_env="brieflow_<screen_id>",
   )
   ```

   If `report["ok"]` is False — STOP and surface the report. Common cases:

   - `marker_coverage.outside` non-empty: operator-bucket params are assigned outside marker blocks → expand the markers in the **canonical** notebook (`<screen-root>/<canonical-notebook-dir>/<phase>.py`), then re-copy/regenerate the per-screen notebook before editing. Do not silently edit outside markers.
   - `collisions` non-empty: same variable assigned in two cells (marker block AND derivation/auto cell). Rename the auto-bucket constant (e.g., `PREFIX_MAP` → `PREFIX_MAP_OUT`) in the canonical notebook before editing.
   - `dry_export.ok` is False: import drift, syntax, or marimo cell-graph error. Fix in canonical, then proceed.

   Log every preflight failure into `<analysis-dir>/.brieflow/findings.json` (schema `wizard-findings/v1`) so the canonical-template defect is tracked for upstream fix.

4. **Edit the phase's marimo notebook in place** (only after preflight passes)

   Locate the `params_*` cells in the corresponding `.py` file (`0_preprocess.py` for preprocess,
`1_sbs.py` for sbs, etc.; `phase_notebook()` in `brieflow_phases.py` finds it in older screens
too). There are **several** per notebook, one per group of parameters (paths, channels,
segmentation, etc.).

   Write each value with `patch_marker_block_value` (or, by hand, the **Edit tool** on that line
only) so it matches the interview. For example, an interview value:

   ```json
   {"param_name": "SBS_IMAGES_DIR_FP", "value": "/path/to/raw/<screen>/input_sbs/"}
   ```

   becomes, inside `params_paths` in `0_preprocess.py`:

   ```python
   @app.cell
   def params_paths():
       SBS_IMAGES_DIR_FP = "/path/to/raw/<screen>/input_sbs/"  # was: None
       ...
   ```

   **Scope discipline (hard rule):**
   - **Only edit assignments inside `params_*` cells** (or, in older screens, between the
     OPERATOR PARAMETERS fences). Never touch derivation cells, viz cells, config-write cells, or
     anything else.
   - **Do not delete or rename variables** in the block — only assign new values to the existing variable names. Variable names match the canonical `.ipynb`'s convention; the rest of the notebook depends on them.
   - **Variable names in the notebook match interview `param_name` values** (usually case-insensitive; e.g., `SBS_IMAGES_DIR_FP` in both, or `sbs_data_format`/`SBS_DATA_FORMAT` cross-case). Match by name.

5. **Run the notebook** (operator's choice):

   - **Interactive review**: operator runs `brieflow-marimo <phase>.py` to open marimo in a browser, sees the derivation cells render (segmentation viz, alignment plots, etc.), confirms or tweaks tuned params. Reactive cells re-run automatically.
   - **Headless run**: `python -m marimo run <phase>.py` (or operator equivalent) executes the whole notebook including its config-write cell, producing `config.yml` / TSVs / etc.

6. **Gate on printed QC metrics (per SKILL.md Step 6).** Parse stdout for the `Alignment QC:` and `Segmentation QC:` blocks the brieflow library functions emit. If every line passes its printed `(pass …)` threshold, proceed. If any fail, block and route the operator to the relevant params (alignment vs cellpose/stardist). Thresholds are **experimental** — they'll be retuned across the first ~10 screens; never silently widen one per-screen.

7. **Pull tuned-export JSON and run the confirm loop** (per SKILL.md Step 6).

   Each phase notebook contains (or should contain) a `# === TUNED EXPORT ===` cell that writes `.brieflow/tuned_<phase>.json` after derivation cells run. Schema: `{<param_name>: {"derived": <value>, "src": <cell-tag>}, ...}`.

   ```python
   from brieflow_wizard import pull_tuned_values, record_tuned_confirmed, patch_marker_block_value
   tuned = pull_tuned_values(analysis_dir, phase)
   if not tuned:
       # Canonical notebook lacks the TUNED EXPORT cell — log to findings.json, add the cell, re-run.
       ...
   for param_name, info in tuned.items():
       # AskUserQuestion: derived=info["derived"] via info["src"] — accept/override?
       confirmed = ...  # operator's answer (often == derived)
       record_tuned_confirmed(interview, param_name=param_name, confirmed_value=confirmed, notebook=phase)
       patch_marker_block_value(notebook_path, param_name, repr(confirmed))
   save_interview(interview, ".brieflow/interview.json")
   ```

   **A phase is not complete until every `user_tuned_seed` param has either an operator-confirmed value (source=tuned-confirmed) or an explicit operator-acknowledged stand-in.** Headlessly defaulting tuned params to docstring seeds is the W-002 failure mode this loop exists to prevent.

8. **Confirmation pauses for `tuned` and `user_tuned_seed` params** (legacy section, superseded by step 7 above for the headless/wizard-driven path; still relevant for marimo-interactive path):

   Both buckets require operator confirmation, but the *source* of the
   starting value differs:

   - **`tuned` (estimate-suggested):** the notebook computes the value in a
     derivation cell (e.g., `estimate_diameters` → `NUCLEI_DIAMETER`,
     `CELL_DIAMETER`; M×B formula → `det_range`). The operator opens the
     notebook in marimo, reviews the derived value via the segmentation /
     alignment viz, and confirms or corrects. The wizard does *not* write
     these into the interview before the notebook runs — they originate in
     the notebook.
   - **`user_tuned_seed`:** the operator's seed value (collected in step 2
     and edited into the marker block in step 3) is the *starting point*.
     The notebook may grid-search (e.g., mapping rate vs. peak threshold
     plot for `threshold_peaks`) or render a comparison plot. The operator
     picks the refined value and updates the marker block — either by hand
     in marimo, or by re-running the wizard with the confirmed value.

   In either case, when the operator confirms or overrides a value, record
   it in the interview:

   ```python
   set_value(i, param_name="cell_diameter", notebook="sbs",
             value=42, source="tuned-confirmed")
   ```

   and re-edit the marker block in the notebook so the confirmed value
   sticks for the run record.

9. **Move to the next phase.**

## Auto-probe protocol

You have file system + shell access; use them. Specifically:

| Prompt | How to auto-probe |
|---|---|
| `SBS_IMAGES_DIR_FP`, `PHENOTYPE_IMAGES_DIR_FP` | Ask operator (no way to infer the path) |
| `SBS_PATH_PATTERN`, `PHENOTYPE_PATH_PATTERN` | After paths given, `find <dir> -name '*.nd2' -o -name '*.tiff' \| head -10`; infer regex from filenames; **show operator with concrete capture examples**, ask to confirm |
| `SBS_PATH_METADATA`, `PHENOTYPE_PATH_METADATA` | Derived from regex capture groups |
| `SBS_METADATA_ORDER_TYPE`, `PHENOTYPE_METADATA_ORDER_TYPE` | Derived from PATH_METADATA + type inference (`plate`→int, `well`→str, `tile`/`cycle`→int, `channel`→str). **Always emit as a dict** `{column: type_name}`, not a list — the notebooks expect dict form. |
| `sbs_data_format`, `phenotype_data_format` | Look at file extensions in `images_dir` |
| `sbs_data_organization`, `phenotype_data_organization` | Per-tile filenames (e.g. `Points-0001`) → `tile`; per-well multi-FOV → `well` |
| Channel names (operator labels) | List unique values of the regex's channel capture group; ask operator to confirm/rename to biological labels |
| `phenotype_round_order` | Look at `round_N` subdirs; if only `round_1`, default to `[1]` |

For everything else (channel→role mapping, library_type, classifier
enabled, segmentation method choice, etc.), ask the operator. Indices like
`cyto_index`, `foci_index`, `dapi_index`, `target`, `source`, `riders` are
derived inside the notebook's derivation cells via
`CHANNEL_NAMES.index(name)` — **don't ask for them**.

## Where raw screen data lives (and doesn't)

You do not know in advance where any screen's raw image data lives. The
operator tells you, full stop. Archive mounts and other site-specific paths are
common, but **never assume**.

What raw data is **not**:

- **Other screens' `*-analysis/` directories** — these are
  analysis directories from prior screen runs (config files + pipeline
  outputs), not raw data sources. Probing these is the migration anti-pattern
  forbidden in the next section.
- **The current screen's own `analysis/brieflow_output/`** — that's prior
  pipeline output, not raw input.
- **Any path you guessed because it contained the screen name** — guesses
  introduce silent errors. Ask the operator.

When you ask for `SBS_IMAGES_DIR_FP` and `PHENOTYPE_IMAGES_DIR_FP`, ask
plainly: "Where is the raw SBS image data?" / "Where is the raw phenotype
image data?" Do not preface with a fishing expedition.

## Scope: what NOT to read

The wizard derives values from three sources only:

1. **Operator answers** (what they tell you in chat)
2. **Raw data probing** (file scans of paths the operator gave you, regex
   inference from filenames, channel name extraction from filename tokens)
3. **brieflow library defaults** (constants from `lib.*` modules that the
   marimo notebooks consume internally)

**You must NOT read from**, even though the filesystem will let you:

- **Any `*-analysis/` directory other than the one you are currently working in.**
  That includes every other `*-analysis/` checkout on shared storage, or any
  other location with the same shape. These are prior screen runs; mining them is migration.
- Any other screen's `config.yml` or `interview.json`
- Any other screen's marimo notebook files
- Notebook output of prior runs from any other screen
- Any path that the global CLAUDE.md or a user-level skill happens to mention
  in passing — those are general orientation for the user's other workflows,
  NOT permission for the wizard to fish

**Why this matters:** copying values from a prior run is *migration*, which is
out of scope for the wizard. If you mine a prior config to populate the
current screen, the wizard run isn't a real test of whether the wizard
works — it's a test of whether prior values look plausible.

**One narrow exception:** the *current* screen's own preserved
`<analysis-dir>/brieflow_output/` is fair game when the operator has
explicitly told you "this run is re-deriving config against existing
pipeline output". The
mere presence of `brieflow_output/` is **not** permission — wait for the
operator to confirm the re-derive intent.

If you catch yourself reaching for a path containing `*-analysis/` or
`*_archive/` outside the current screen repo, **stop and ask the operator
first**. The right question is usually "should I look at this, or are you
giving me the value?" — not a silent file read.

## Conditional gating

A row's structured `gating` block (`{"param": ..., "equals": ...}`, or an `op` of
`not_equals`, `is_set`, `not_null` or `in`) decides whether it is active;
`is_active(param, interview)` evaluates it against the currently filled values in the
interview JSON. Rows without a block fall back to a legacy `only when X==Y` clause in
their `notes`. Examples from the template:

- `{"param": "MERGE_APPROACH", "equals": "positions"}` on `FLIPUD`/`FLIPLR`/`ROT90`, and
  `"equals": "fast"` on `INITIAL_SITES_APPROACH`, `DET_RANGE` and the fast-merge levers
- `{"param": "BARCODE_TYPE", "equals": "multi"}` on the MAP/RECOMB rows: skipped on `simple`
- `{"param": "ALIGN", "equals": true}` on `TARGET`/`SOURCE`/`RIDERS`: skipped without alignment

A gating param that is not filled yet leaves the row inactive until it is. If you discover
a gating relationship not encoded in the template, add a `gating` block to
`data/interview_template.json` (the hand-maintained source of truth) rather than working
around it.

## After all phases

Once cluster (the final phase) completes:

- `<analysis-dir>/config/config.yml` — full screen config, written by the
  notebooks themselves (each phase's notebook writes its section into the
  same file)
- `<analysis-dir>/.brieflow/interview.json` — complete record of every
  operator decision with provenance (operator / auto-probe / gen /
  tuned-confirmed) and timestamps
- `<analysis-dir>/{0_preprocess,1_sbs,2_phenotype,3_merge,5_aggregate,6_cluster}.py`
  — the marimo notebooks themselves, with the operator's values baked into
  the `OPERATOR PARAMETERS` blocks. **These are also the run record** — open
  any of them and you can see exactly what params produced the run.

Then the operator runs the pipeline phases via:

```
bin/brieflow-run
```

## Notes on the artifact split

- **The notebook itself is the parametrized record.** Opening `0_preprocess.py`
  shows exactly what was set for this screen — no hidden indirection.
- **The interview JSON is the audit trail.** It records *who* set each value
  (operator / auto-probe / tuned-confirmed) and *when*. Useful for review
  and debugging, but the notebook is the canonical source of "what ran."
- **`data/interview_template.json` is the hand-maintained source of truth.**
  It is edited directly — there is no CSV or generator step upstream of it.
- **`auto` and `tuned` values are notebook concerns**, not wizard concerns.
  Derivation cells run inside the notebook (`estimate_diameters` for cellpose
  diameters, M×B formula for `det_range`, regex parsing for sample DFs,
  threshold computation, etc.) and produce these config values directly.
  The wizard doesn't try to "fill in" `auto` or `tuned` params — the notebook does.
- **`user_tuned_seed` values are wizard concerns at seed time, notebook concerns at refine time.**
  The wizard collects the operator's starting value and writes it to the
  marker block. The notebook may then grid-search or render a refinement
  plot; the operator confirms the final value and the wizard re-writes the
  marker block (and updates `interview.json` with `source="tuned-confirmed"`).

## Dual-mode UX

The marimo notebooks support both:

- **Default user (interactive)** — operator runs `brieflow-marimo <phase>.py`,
  edits the OPERATOR PARAMETERS blocks by hand in marimo's browser UI, hits
  run, sees derivation cells reactively re-run. Same UX as the canonical
  `.ipynb` workflow.
- **Wizard-driven (Claude)** — Claude reads interview.json, edits the
  OPERATOR PARAMETERS blocks via the Edit tool, runs the notebook headlessly
  (or hands off to operator for interactive review). The interview JSON
  serves as Claude's working notes + the audit trail.

Either path produces the same artifact: a parametrized `.py` notebook that
ran a config-shaping pipeline. The difference is who typed the literal
values into the OPERATOR PARAMETERS blocks.

## Edge cases & gotchas

- **Regex matches 0 files** — propose alternative patterns, or ask operator
  for sample filenames manually
- **No DAPI channel found in scan** — ask which channel is the nucleus stain
  (Hoechst, etc.; doesn't have to be literal "DAPI")
- **Mixed nd2 + tiff in same dir** — ask operator to pick one; mixed isn't
  supported
- **Multi-plate screens** — single-plate handled gracefully; multi-plate may
  require manual edits to the notebooks (then re-running the affected phase)
- **Operator wants to re-init** — they delete
  `<analysis-dir>/.brieflow/interview.json`; next activation starts fresh
  from the template. Notebooks keep their current OPERATOR PARAMETERS values
  (manual cleanup if they want a fresh slate there too).
- **CSV evolved during a screen run** — the `_csv_sha` field in interview.json
  pins the version the screen was started with. If you see a mismatch,
  surface it; usually safe to continue (taxonomy adds don't break
  partially-filled interviews) but operator should know.
- **Notebook missing OPERATOR PARAMETERS markers** — the auto-conversion or
  cleanup pass may have missed a section. Surface it; do **not** silently
  edit cells without markers. Markers are the safety guarantee.

# brieflow-auto scripts — module map + resolution taxonomy

Contributor index for the plugin's Python helpers. The runtime contract is
unchanged from when everything lived in one file: **`brieflow_wizard.py` is a
thin facade that re-exports every public symbol** from the focused modules
below, so `from brieflow_wizard import X` (scratchpad / runner / bash /
spec-loaded tests) keeps working. Import boundary, install model, and the
plugin-vs-brieflow-library split are documented in the repo-root `CLAUDE.md`.

## Execution records and prospective development

`brieflow_connection.py` plans and verifies remote login handoffs, with explicit Linux
identity and workspace access. It prints connection and on-machine tmux/Claude Code
instructions; it is not a remote pipeline runner. The setup skill's connection reference
documents GCP OS Login, explicit SSH accounts, and use from multiple clients.

`brieflow_trace.py` preserves per-invocation state/output and explicitly imported runtime
transcripts. `brieflow_rollout_hook.py` implements opt-in Claude Code turn-boundary capture.
[TRACE_RECORDS.md](TRACE_RECORDS.md) documents capture scope, setup, and private-data handling.
`brieflow_improve.py` freezes selected evidence, stages baseline/candidate copies and compares
their regression suites. The agent supplies the proposal; the helper never promotes a change.

## Module map — four subsystems

`scripts/` is a flat module namespace on purpose: modules are invoked **by path**
from outside this repo (each screen repo's `flow.sh`, the installed plugin cache)
and imported by bare name, so the directory is a stable public surface rather than
a place to grow subpackages. The architecture is expressed here instead — every
module not described above belongs to one of four subsystems.

**A. The run loop** — the transplantable harness core (repo-root `CLAUDE.md` →
"The run loop"): one per-phase primitive applied in a loop, made survivable and
unattended.

| Module | Role |
|---|---|
| `brieflow_runloop.py` | The engine for one run: submit → monitor → classify failure → resume; drives the monitor thread + notifications |
| `brieflow_monitor.py` | Notify-free survivability signals + adaptive cadence (liveness / CPU / disk / OOM-cascade) — the generic, reusable layer |
| `brieflow_recover.py` | Recovery *policy* + orchestration (OOM mem-bump, FD-limit, requeue) — consumes classify + failure_logs |
| `brieflow_classify.py` | Failure taxonomy (OOM / config / NFS-latency / FD-limit / unknown) — "classify, don't blind-retry" |
| `brieflow_failure_logs.py` | snakemake master-log discovery + per-rule error parsing — the stack-specific layer to re-implement on export |
| `brieflow_backend.py` | slurm / local failure-log + job-state adapters (rule logs, sacct/queue queries, OOM counts) |
| `brieflow_driver_watchdog.py` | Liveness watchdog for the driver process |
| `brieflow_run.py` | Run launcher — invokes flow.sh (`run_flow`); `--screen` dispatches to the runner |
| `brieflow_runner.py` | Autonomous conductor — resolve the whole interview up front, then run the loop unattended |
| `brieflow_hooks.py` | Lifecycle hooks (e.g. GCS push) around phases |
| `brieflow_phases.py` | Canonical stage list — the single source `PHASES` / `PIPELINE_PHASES` derive from |
| `brieflow_manifest.py` | run_manifest.json version stamp for each launch |
| `brieflow_issue_draft.py` | issue_draft.{md,json}: what the runner leaves behind when it gives up |
| `brieflow_issue.py` | Open complete upstream bug reports with compatibility proposals; deduplicate and record GitHub URLs |
| `brieflow_decisions.py` | decisions.{jsonl,md}: the append-only decision journal (`auto` / `agent` / `operator`) |
| `brieflow_launch_review.py` | launch_review.{html,json}: what the run will do, gated behind `--approved` |

**B. Interview & setup** — the config-resolution layer that turns `screen.yaml` +
operator answers into parametrized notebooks. Internally a facade over 10 modules,
detailed in **"The wizard, split"** below: `brieflow_wizard` (facade),
`brieflow_interview`, `brieflow_buckets`, `brieflow_probe`, `brieflow_tiles`,
`brieflow_markers`, `brieflow_channels`, `brieflow_marimo`, `brieflow_sweep`,
`brieflow_resolvers`, `brieflow_notebook_ops`.

**C. Guards & QC** — gates that fail fast before/after a run.

| Module | Role |
|---|---|
| `brieflow_preflight.py` | Static config validation against the brieflow rule code (pre-submit) |
| `brieflow_qc.py` | Post-phase QC consolidation + gate from `eval/` outputs |
| `brieflow_dryrun_preview.py` | Dry-run DAG preview + authoritative rule→phase mapping |
| `brieflow_validate_tree.py` | Raw-data tree validation + layout normalization before onboarding |
| `brieflow_base_order_probe.py` | Infers the SBS channel↔base assignment from the data and checks it against config |
| `brieflow_dye_balance_probe.py` | Calibrates the combinatorial `frac` caller from per-dye brightness on the test tile |

**D. Sizing, diagnostics & infra** — supporting helpers.

| Module | Role |
|---|---|
| `brieflow_disk.py` | Per-phase disk requirement computation |
| `brieflow_screen_geometry.py` | Screen geometry (plates × wells × tiles × cycles × channels) |
| `brieflow_resolve_resources.py` | Resource / scaling formulas (mem, cpus) |
| `brieflow_aggregate_efficiency.py` | Refresh mem recommendations from efficiency CSVs |
| `brieflow_diag.py` | Diagnostics bundle for issue reports |
| `brieflow_compute.py` | Probes the execution backend + slurm identity; writes the base slurm profile |
| `brieflow_reorder_channels.py` | Permutes the channel axis of an already-converted preprocess store in place |
| `brieflow_notify.sh` | Slack notifier with priority tiers |
| `brieflow_smoketest.py` | Fast import / contract smoke test |
| `_ws_holder.py` | WebSocket holder spawned by the marimo kernel |

## The wizard, split (move-not-rewrite)

`brieflow_wizard.py` was a 4137-line monolith; it is now a thin facade over
10 modules. Dependency arrows point one way (a module only imports from modules
above it); there are no cycles.

| Module | Role | Depends on |
|---|---|---|
| `brieflow_buckets.py` | Resolution **taxonomy**: bucket families + `value_source` enum + `validate_interview_buckets` | stdlib |
| `brieflow_probe.py` | Data-inspection probes (`detect_*`, `infer_path_pattern`) | stdlib (+lazy nd2) |
| `brieflow_tiles.py` | Tile selection (`select_test_tile*`, `select_validation_tiles_from_metadata`, `select_tiles_one_per_well` (one tile/well diagnostic fan-out), `select_initial_sbs_tiles`) | stdlib (+lazy nd2/pandas) |
| `brieflow_markers.py` ⚠️ | OPERATOR PARAMETERS marker scan/patch (**contract-frozen**) | stdlib |
| `brieflow_channels.py` | Per-channel colormap helpers (`default_channel_cmaps`) | stdlib |
| `brieflow_marimo.py` ⚠️ | marimo kernel lifecycle (**contract-frozen**) | stdlib |
| `brieflow_interview.py` | Interview engine: init/IO, gating (`is_active`), `_COMPUTED_DERIVATIONS`, defaults, `resolve_all_phases`, channel-mapping confirm/record | buckets + channels |
| `brieflow_sweep.py` | Sweep + tile-validation orchestration (`run_marimo_sweep`, `sweep_headless`, `adaptive_sweep_headless`, `export_phase_html`, `parse_qc_block`, …) | markers + marimo + tiles |
| `brieflow_resolvers.py` | Resolver **registry**: `RESOLVER_MAP`, `KNOWN_UNIMPLEMENTED_PROBES`, `validate_resolver_coverage` | (resolved against the facade) |
| `brieflow_notebook_ops.py` | Pre-edit static checks + notebook ops (`preflight_notebook`, `dry_export_notebook`, `reset_phase`, `verify_phase_outputs`, `audit_canonical_notebooks`, `confirm_tuned_loop`) | interview + markers + buckets |
| `brieflow_wizard.py` | **facade** — re-exports all of the above | all of the above |

⚠️ **contract-frozen**: `brieflow_markers` and `brieflow_marimo` implement the
live marimo-run contract (marker matching rules, kernel lifecycle, the
pre/post-kernel-only patching rule). Don't change their behavior or signatures —
`tests/test_marimo_live.py` exercises the marimo path against a **real** kernel.

## Resolution taxonomy (`brieflow_buckets.py`)

Every interview row carries two orthogonal labels:

- **`bucket`** — *how* it resolves. Families:
  - **operator** `{user, user_tuned_seed, override}` — asked up front (the only
    buckets `resolve_all_phases` collects as gaps).
  - **system-derived** `{auto, auto-probe, tuned-output}` — notebook/data-derived.
  - **agent-resolved** `{agent-resolved}` — run-time sweep winner / eval-table pick.
  - **archival** `{archival}` — documentation-only; no marker, never asked.
- **`value_source`** — *where* the current value came from (`VALID_VALUE_SOURCES`:
  `operator` / `default` / `auto-probe` / `gen` / `tuned-confirmed` /
  `screen-yaml` / `lab-default`).

`validate_interview_buckets(rows)` is run against the real
`data/interview_template.json` by `tests/test_bucket_taxonomy.py` — bucket
vocabulary + derivation-clause shapes can't drift silently.

## Resolver registry (`brieflow_resolvers.py`)

Every `auto-probe` / `agent-resolved` row resolves via something — a live python
helper, the row's derivation clause, the agent during the interview, or a tracked
gap. `RESOLVER_MAP` is that single discoverable map; `KNOWN_UNIMPLEMENTED_PROBES`
lists promised-but-unbuilt probes (currently **empty** — `DET_RANGE` is now
built) so any future gap stays visible, not silent.
`validate_resolver_coverage(rows, namespace)` (run by
`tests/test_resolver_registry.py`) asserts no orphan promises and no dead
resolvers. `tests/test_template_consistency.py` additionally locks every
`RESOLVER_MAP` key + probe prefill key to a real (case-exact) template param.

## Where values come from

- `data/lab_defaults.yaml` — `sweep_bands` (swept), `notebook_defaults` (fixed
  values patched into the exact notebook var), `cluster_selection` (QC selection).
- `data/interview_template.json` — **the maintained source of truth** (one row
  per uppercase notebook var per notebook, integrity-locked by
  `tests/test_template_consistency.py`; the spec CSV that bootstrapped it,
  `docs/superpowers/plans/2026-06-07-interview-spec.csv`, is a historical index
  that test keeps equal to the template's row set). Per-row
  `bucket` + `derivation`. Derivation types: `screen_yaml` /
  `screen_yaml_list_field` (read screen.yaml; `null_ok` = explicit null is valid,
  not a gap), `library_default`, `screen_yaml_computed` (a key in
  `_COMPUTED_DERIVATIONS` in `brieflow_interview.py`).
- **Rule: the operator writes screen.yaml.** A required fact left blank (`REQUIRED_KEYS` in
  `brieflow_screen_yaml.py`) stops setup and the runner; any other blank is asked of the
  operator, never guessed.
- The all-null `screen.yaml` scaffold operators copy + fill lives in **brieflow
  itself** (not this plugin); it carries the channel-mapping schema the channel
  auto-probe maps against.

## Dev / test

The canonical runner is the committed `brieflow_auto_dev` conda env
(`requirements-dev.txt`, marimo pinned). From `plugins/brieflow-auto/`:

```
make test-env     # one-time: conda + uv create brieflow_auto_dev
make test         # full unit suite + the live-kernel marimo test
make marimo-test  # just the live marimo integration test
make lint         # ruff
```

`tests/test_marimo_live.py` is `importorskip`-guarded, so envs without marimo
(e.g. a bare CI runner) skip it and the rest of the suite still passes.

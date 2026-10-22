# Brieflow OPS Plugin

A Claude Code plugin that drives [brieflow](https://github.com/cheeseman-lab/brieflow)
— the Cheeseman lab's OPS pipeline — from raw imaging data to a publishable result with
minimal operator intervention, on shared HPC (Slurm) and cloud (GCS-backed VM) targets.

## What this plugin is for

A screen lifecycle, from the operator's seat, should look like:

1. **Setup**: provision the analysis repo + conda env + raw-data staging, and stage the
   `screen.yaml` the operator wrote once. (`brieflow-setup`)
2. **Per-phase notebook configuration**: walk one phase at a time (the order in
   `PIPELINE_PHASES`, `scripts/brieflow_phases.py`), let the plugin pre-fill what it
   can derive, tune what needs tuning, export an HTML record. (`brieflow-notebook`)
3. **Full pipeline launch**: read the launch review the runner writes, re-run with
   `--approved`, and let it drive every phase in range — preflight, submit, recover
   from OOM, gate. (`brieflow-run`)
4. **Archive + publish**: notebook HTML, the run manifest, the decision journal and
   the findings log travel with the screen for reproducibility; an escalating
   pipeline defect leads to an automatically opened upstream issue with a backward-compatible
   fix proposal and a recorded issue URL.

Every step is designed so the operator answers the smallest possible set of
questions, and every answer is captured in a structured artifact — `screen.yaml`
plus, under `.brieflow/`, `interview.json`, `launch_review.html`,
`run_manifest.json`, `<phase>.final.html`, `decisions.jsonl`, `findings.json` and
`issue_draft.md` (local evidence) and `issues/` (public reports and submission receipts) —
that another operator, or a reviewer, can read back later.

## Screen repo detection

A directory is a brieflow screen repo if `screen.yaml` sits in it or a parent, a
`brieflow/` submodule directory is present, or `config/config.yml` carries brieflow
keys. The screen name is derived from `screen.yaml` or from the repo directory name
(strip the `-analysis` suffix); the conda env convention is `brieflow_SCREENNAME`
and the analysis dir is typically `analysis/` under the repo root.

## Available commands

| Command | Purpose |
|---|---|
| `/brieflow-setup` | First-time screen onboarding — provision repo + env + raw data, stage the operator's `screen.yaml` |
| `/brieflow-notebook` | Configure + run one phase notebook end-to-end via the marimo reactive API; exports HTML |
| `/brieflow-preflight` | Static config validation against brieflow rule code (catches `cp_method`-style mismatches in seconds) |
| `/brieflow-module` | Launch a pipeline run; supports auto-recover on OOM |
| `/brieflow-qc` | Consolidate + gate one phase from its `eval/` outputs against `qc_gates.yaml` |
| `/brieflow-run` | Run a screen through the per-phase cycle over any range of phases — whole screen, `--only` one, or `--from`/`--through` |
| `/brieflow-status` | Read-only status (squeue, sacct, output dirs, snakemake logs) |
| `/brieflow-tune-memory` | Refresh `mem_recommendations.json` from accumulated efficiency CSVs |
| `/brieflow-issue` | Automatically open a Brieflow bug report for a pipeline defect, with a backward-compatible fix proposal |
| `/brieflow-improve` | Prospective evidence-linked harness development in separate baseline/candidate snapshots |

## Design principles

Commitments every command honors, so a screen run's output is publishable as-is.

### Self-describing artifacts

Every value lands in `screen.yaml` or `.brieflow/interview.json` with a `value_source`
field (`screen-yaml` / `operator` / `auto-probe` / `default` / `gen` /
`tuned-confirmed` / `lab-default`) recording its true provenance — never a guess
mislabeled as operator input, so any number traces back to its source. The final HTML
export reflects the parameters that produced the QC and mapping numbers shown; the
launch review groups every active parameter by provenance, with what needs an answer.

### One question per operator decision, asked only when necessary

`screen.yaml` is the source of truth for everything the operator knows about the screen.
The interview = the UPPERCASE notebook OPERATOR-PARAMETER vars in
`data/interview_template.json`, each resolved through exactly one of five sources:
`screen.yaml` derivation (incl. `screen_yaml_computed`), `lab-default`
(`lab_defaults.yaml`), `auto-probe` (data inspection), `sweep-seed` (agent tunes),
`key-decision` (asked). **A required fact left blank in screen.yaml stops setup and the run
(`REQUIRED_KEYS` in `scripts/brieflow_screen_yaml.py`); any other blank is asked, never guessed**;
free-text `additional_notes` prose is scanned (`scan_additional_notes`) into candidate
overrides the agent confirms. Each ask is one `AskUserQuestion`, not a batch form — ~5
genuine decisions on the reference screens, down from ~89.

### Live-kernel parameter sweeps via the marimo reactive API

Phase notebooks run in a live marimo kernel; in-session parameter mutation uses the
reactive API (`ctx.edit_cell` + `ctx.run_cell` via the scratchpad — see
`brieflow-notebook`), which invalidates only downstream cells, so a sweep iteration
costs the work below the changed marker, not the full DAG. Disk and kernel
synchronize at exactly two points: kernel startup (it reads the pre-filled file) and
HTML export (`ctx.save()` flushes the in-kernel winners back).

### Failure has a classifier, not a blind retry

`scripts/brieflow_classify.py` names the classes and `scripts/brieflow_recover.py`
owns the policy and its constants: bump `mem_mb` and resume on OOM (slurm; on
`--backend local` an OOM escalates), raise the FD
limit and resume on FD exhaustion, wait and resume on NFS latency, escalate
config-mismatch and unknown immediately so cluster time isn't wasted.

### Runs are self-identifying

Every launch writes `.brieflow/run_manifest.json` (schema
`brieflow-run-manifest/v1`, `scripts/brieflow_manifest.py`): plugin version and git
SHA, screen repo and brieflow submodule SHAs, interpreter and library versions, and
this invocation's flags. It is a **version stamp for a project still in evolution,
not a rerun guarantee** — enough to explain a result and tell two runs apart, never
a promise that replaying those versions reproduces the numbers.

### Escalation opens an upstream issue

When the loop stops on a blocker — every nonzero code the phase loop returns, plus a
failing pre-phase hook; never a pre-launch return (gap, missing env, bad range) and never
8, the launch gate — `run_screen` calls `write_issue_draft` (`scripts/brieflow_issue_draft.py`):
`.brieflow/issue_draft.md` + `.json`, built from the recovery action, the diagnostic
bundle and the manifest, with `<operator: fill in>` placeholders where only a person can
speak. These legacy draft files are local evidence, not the escalation endpoint.
The agent invokes `/brieflow-issue` to investigate the defect, select shareable evidence,
and automatically open a report using `scripts/brieflow_issue.py`. Reports follow Brieflow's
bug template and require a minimal backward-compatible fix proposal and regression-test plan.
The helper records the issue URL under `.brieflow/issues/` and journals `issue-filed`.
Without an active agent, the standalone runner leaves the evidence for the next agent session;
it cannot infer a safe fix proposal or upload raw logs by itself.

### Findings are first-class

When the agent detects a defect it can't fix in place — a notebook marker gap, an
interview-template row missing a derivation, a degraded auto-probe — or works around one in
place — a `screen.yaml` edit, an input symlink tree, a resource stopgap, a sticky
interview-row rewrite — it calls `record_finding()` (`scripts/brieflow_findings.py`), which
appends a row to `.brieflow/findings.json` (`schema: wizard-findings/v1`). Every skill follows
this (router rule R5). Findings travel with the screen and are what `/brieflow-improve`
collects; a workaround recorded only in prose is invisible to it.

## Hard stops

These are absolute rules. The plugin's commands enforce them; contributors
maintaining the plugin honor them too.

- **All temp files go in the current working directory**, never `/tmp` or any
  system temp dir. Shared HPC.
- **Never commit on behalf of the operator** unless the operator says so
  explicitly. The commands produce diffs; commits are an operator decision.
- **Don't edit the `.py` notebook file while a marimo kernel is running on it.**
  `patch_marker_block_value` writes to the `.py` file and is only valid before
  `start_marimo_kernel` and after `stop_marimo_kernel`; in-session mutation uses
  `ctx.edit_cell` + `ctx.run_cell` via the scratchpad. The disk file is the
  operator-visible artifact at start and export, not a control surface mid-sweep.
- **`ctx.save()` is reserved for `export_phase_html`.** Never call it from a
  scratchpad cell during a sweep — it overwrites the operator-pinned values the
  pre-kernel marker-patching step wrote into the file and persists transient sweep
  values as if they were the chosen set.
- **Don't silently hardcode interview values in the canonical notebook.** If a row
  is `bucket=auto + value=null` after bootstrap, that's a template gap — log
  `wizard-findings/v1 kind=canonical-template-gap` and STOP. The next screen hits
  the same gap and the operator won't see why if it's hardcoded.
- **`select_test_tile` does not fall back to tile 0** on read failure. Tile 0 is an
  edge tile and silent fallback picks bad data; the function returns `tiles: []`
  and the caller routes to operator selection.
- **File pipeline defects automatically, without changing pipeline source.**
  `/brieflow-issue` opens a report with a backward-compatible fix proposal, preserving
  existing defaults, interfaces, and supported screens. Do not reshape the pipeline
  around one dataset, relax QC, or insert screen-specific exceptions. Distinguish tested
  compatibility from proposed regression tests. Keep private data and raw diagnostic
  bundles local, check for duplicates, and report filing failures without claiming success.
- **Docs name flags and paths that exist.** `tests/test_docs_match_code.py` is
  the gate — run it after editing any skill, command, `README.md`, `CLAUDE.md`
  or `HARNESS.md`. Numbers live in code or policy files and the prose points at
  them; a restated value drifts silently and the agent believes the stale copy.
- **No skipped hooks or signing** on commits unless the operator asks for it.

## Architecture

The plugin combines four kinds of file:

- **Slash commands** (`commands/*.md`) — user-facing entry points; markdown
  protocols Claude follows, each calling one or more helpers with the right args.
- **Python helpers** (`scripts/*.py`) — deterministic workflow logic: interview I/O,
  marker-block scanning, failure classification, config preflight, memory aggregation,
  marimo kernel lifecycle, sweep orchestration. Stdlib plus a small set of third-party
  deps (`yaml`, `nd2`, `pandas`, `marimo`).
- **Bash helpers** (`scripts/*.sh`, `cloud/*.sh`) — squeue, sacct, mail, VM
  provisioning. Intrinsically shell tasks stay in shell.
- **Skills** (`skills/*/SKILL.md`) — long-form protocols that span multiple
  commands (e.g. `brieflow-notebook` walks the full per-phase sequence). Skills
  load reference docs on demand.

## The run loop

`bin/brieflow-run` execs `scripts/brieflow_run.py --screen`, which dispatches to
`run_screen()` in `scripts/brieflow_runner.py`. The phase list is data
(`PIPELINE_PHASES` in `scripts/brieflow_phases.py`); `--from`/`--through`/`--only`
bound the range, and `--backend`, `--lifecycle-hooks` and `--screen-id` pick
compute and data movement.

`run_screen` resolves the whole interview once, writes the launch review, and
returns 8 until the operator re-runs with `--approved`. `_run_phase_range` then
applies one primitive per phase — parametrize the notebook → `run_preflight` the
config it wrote → run under `scripts/brieflow_runloop.py` (submit, monitor,
classify, recover) → gate with `scripts/brieflow_qc.py` — pausing only on a
classified blocker, with a distinct exit code per blocker. The generic principles, state
files, agent boundary and porting checklist live in [HARNESS.md](./HARNESS.md).

## Plugin helpers vs the brieflow library

Two distinct codebases run side-by-side during any skill invocation:

1. **The `brieflow` library** — pip-installed into the screen's conda env
   (`brieflow_<screen>` by convention). Provides the image-processing logic the
   phase notebooks call (`lib.sbs.align_cycles`, `lib.sbs.call_reads`,
   `lib.phenotype.align_channels`, …), versioned by the brieflow submodule under
   each screen repo. Independent from this plugin.

2. **Plugin helpers** — ship with this plugin, NOT in brieflow's conda env,
   under `plugins/brieflow-auto/scripts/`: `brieflow_wizard.py` (a facade over
   the interview / marker / marimo / sweep / QC modules), the live-kernel bridge
   (`marimo-pair/{execute-code,discover-servers}.sh` and `_ws_holder.py`), and
   the `brieflow_*.py` / `brieflow_*.sh` helpers the runner and the commands
   call (run, preflight, recover, aggregate, status).

### Import boundary

| Where you import from | Resolved by |
|---|---|
| `from lib.sbs.eval_mapping import ...` (kernel scratchpad or screen-env shell) | conda env / `brieflow` package |
| `from brieflow_wizard import run_marimo_sweep` (scratchpad) | `sys.path.insert(0, '<scripts-dir>')` in the scratchpad preamble |
| `from brieflow_wizard import ...` (bash `python -c`) | `PYTHONPATH="$SCRIPTS_DIR"` before the python command |

`<scripts-dir>` / `$SCRIPTS_DIR` resolves to
`~/.claude/plugins/cache/brieflow-auto/brieflow-auto/<version>/scripts/` — a symlink
to the dev repo in dev mode, a copy in release mode.

The two release cycles are independent: rebuilding the screen's conda env
against a newer brieflow submodule does not change plugin helper behavior, and
reinstalling the plugin does not change brieflow library behavior. `plugin.json`
versions the plugin; the screen's `brieflow/` submodule SHA versions the library.

## Preflight gate

`run_preflight` runs before every phase submit and aborts on a HIGH-severity finding.
`scripts/brieflow_preflight.py` derives what to check from brieflow's own rule and script
sources, catching two classes that would otherwise fail every job: **accepted-value
violations** (a config value the script's `if/elif/raise ValueError("Unknown ...")` block
rejects) and **required-key absences** (a script reading `config[K]` would hit `KeyError`).

## Manual override preservation

`mem_recommendations.json` entries written by `scripts/brieflow_recover.py` carry
`manual_override` in `obs_source`, and `scripts/brieflow_aggregate_efficiency.py`
preserves them on refresh — a hard-won post-OOM bump is never overwritten by an
aggregator run that lacks data for that rule.

## Repository layout

`plugins/brieflow-auto/` holds the plugin runtime — `commands/`, `skills/`,
`scripts/`, `data/`, `bin/` and the `cloud/` helpers — alongside developer-only
material: `tests/`, `conftest.py`, `_test_screens/`, `tools/`, `Makefile`,
`pyproject.toml` and `requirements-dev.txt`. The repo root holds the docs and the
installer:

| Path | Purpose |
|---|---|
| `README.md` | What the plugin is, install, the command list |
| `HARNESS.md` | The pipeline-agnostic harness design, brieflow-auto as the worked example |
| `CHANGELOG.md` | Release history |
| `scripts/install_plugin.sh` | Populates the plugin cache — dev symlink or release copy |
| `docs/` | Development notes, specs, plans and history. **Local only, git-ignored.** |

New planning docs go in `docs/superpowers/specs/` or `docs/superpowers/plans/` with a
`YYYY-MM-DD-` prefix — never inside the plugin. `docs/` is git-ignored, so those notes stay on the
machine that wrote them; anything that must travel belongs in `HARNESS.md` or this file.
One file under it stays tracked: `docs/superpowers/plans/2026-06-07-interview-spec.csv`, the
index `tests/test_template_consistency.py` holds equal to the interview template. Git keeps
tracking a tracked file inside an ignored directory, so edits to it still show in `git status`;
commit them with the template change they belong to.

## Changing the plugin

Four recipes. Each names every file the change touches; `make test` from
`plugins/brieflow-auto/` is the last step of all of them.

- **Add a runner flag**: `add_argument` in `scripts/brieflow_run.py` — argparse's
  own `default=` covers every real command line, so add it to the `--screen`
  dispatch defaults list in the same file only for callers that build `args`
  without the parser (tests, programmatic entry); read it in `run_screen`
  (`scripts/brieflow_runner.py`); record it in the manifest's `run` block
  (`scripts/brieflow_manifest.py`) so it travels with the result; document it in
  `skills/brieflow-run/SKILL.md`; run `pytest tests/test_docs_match_code.py`.
- **Add a phase**: append the `(phase, notebook)` pair to `PIPELINE_PHASES` in
  `scripts/brieflow_phases.py` (and to `NOTEBOOK_ONLY_PHASES` if it writes config
  but submits no module); ship the notebook; add its rows to
  `data/interview_template.json`; add a QC extractor in `scripts/brieflow_qc.py`
  and register it in `_EXTRACTORS` (unregistered, the phase gets an empty,
  always-passing gate); add a gate block in `data/qc_gates.yaml`. The loop,
  dry-run order, launch review and QC all derive from the list — no control flow
  changes.
- **Add a skill or command**: `commands/<name>.md` (frontmatter `description:`, one
  paragraph, a pointer to the skill) and `skills/<name>/SKILL.md` (frontmatter `name` +
  trigger-phrase `description`, then the ordered protocol; long reference material to
  `skills/<name>/references/`); add a row to the router table
  (`skills/brieflow-auto/SKILL.md`) and to `README.md`, and recount the skills/commands
  lines there.
- **Release**: bump `plugins/brieflow-auto/.claude-plugin/plugin.json`,
  `plugins/brieflow-auto/pyproject.toml` and `.claude-plugin/marketplace.json`
  together — `test_versions_agree_and_claude_md_has_no_current_marker` fails if
  they disagree; add the `CHANGELOG.md` entry; `make test`; tag.

## Install model

Claude Code reads plugin code from
`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`, not from the source
repo. Two install modes populate that cache:

### Dev mode — cache is a symlink to the source repo

```bash
scripts/install_plugin.sh --dir <path-to-this-repo> --dev
```

This symlinks `~/.claude/plugins/cache/brieflow-auto/brieflow-auto/<version>/`
to `plugins/brieflow-auto/` in this repo, so source edits reach the runner with
no reinstall:

- Body-of-file content (Python helpers, SKILL bodies, docstrings) updates with no
  reload; frontmatter, `commands/*.md` and `plugin.json` need `/reload-plugins`.
- `--dev` also sets `autoUpdate: false` for this marketplace entry so a
  background sync can't clobber the symlink with a copy.
- `git push` in dev mode is for versioning and sharing with other operators — the
  runner sees an edit as soon as the file is saved, pushed or not.

### Release mode — cache is a real copy

```bash
scripts/install_plugin.sh --git --ref v0.3.0                  # pinned GitHub ref
scripts/install_plugin.sh --tarball brieflow-auto-plugin.tgz  # tgz with brieflow-auto/ at top
scripts/install_plugin.sh --dir /path/to/brieflow-auto        # extracted checkout
```

`cp -a` populates the cache, so source edits do NOT propagate: the cache is a
snapshot of `plugin.json`'s declared `version` at install time. To update,
re-run `install_plugin.sh`.

### Registry files

`~/.claude/plugins/known_marketplaces.json`, keyed by `brieflow-auto`, records the
marketplace source, install location and `autoUpdate` flag;
`~/.claude/plugins/installed_plugins.json`, keyed by `brieflow-auto@brieflow-auto`,
records the cache path and version.

## Versioning

See [CHANGELOG.md](./CHANGELOG.md).

# HARNESS.md — an LLM-driven harness around a long-running pipeline

A harness is the layer between an operator and a compute-heavy pipeline that runs for days:
it sets the pipeline's parameters, validates them, launches the work, watches it, recovers
what is recoverable, gates the output, and records why each number is what it is. An agent
can drive that layer but cannot be trusted to improvise it, so the harness is the set of
files that constrain what the agent may decide and record what it decided.

Execution records and coding-agent transcripts are distinct. Each runner invocation retains
before/after state and Python output. A runtime hook, turned on at setup after one privacy
disclosure, also retains actual conversation exports locally, with explicit session identity;
it does not recover missing historical traces.
See [capture and privacy](plugins/brieflow-auto/scripts/TRACE_RECORDS.md).
The prospective `brieflow-improve` workflow separates evidence, proposed changes, frozen
baseline/candidate tests, and maintainer review. Passing tests are not proof of improved screen
results, and the workflow never promotes a candidate on its own.

Each principle below names the file implementing it in brieflow-auto, the worked example in
this repo, which drives an optical pooled screening pipeline on a slurm cluster or a cloud VM.
The design is *science*-agnostic but *stack*-specific: it assumes reactive (marimo) notebooks,
a Snakemake DAG and a slurm-or-local executor. Porting means re-implementing the stack-bound
pieces and keeping the rest (§9).

## 1. One runner, stages as data

There is exactly one end-to-end entry point. `bin/brieflow-run` execs
`scripts/brieflow_run.py --screen`, which dispatches to `run_screen()` in
`scripts/brieflow_runner.py`. Everything else is a flag on that command, not a second route:

| Concern | Flag | Implemented in |
|---|---|---|
| Which stages run | `--from` / `--through` / `--only` | `resolve_phase_range` in `scripts/brieflow_runner.py` |
| Where compute happens | `--backend` (`slurm`, `local`) | `scripts/brieflow_run.py`; log and job-state adapters in `scripts/brieflow_backend.py` |
| Data movement around the run | `--lifecycle-hooks` (`noop`, `gcs`), `--screen-id` | `scripts/brieflow_hooks.py` |
| Permission to spend compute | `--approved` | `scripts/brieflow_launch_review.py` |

The stage list is data, not control flow. `scripts/brieflow_phases.py` holds one ordered
table of `(phase, notebook)` pairs — `PIPELINE_PHASES` — and derives every other view from
it: `PHASE_ORDER`, `PHASES` (`PIPELINE_PHASES` minus any `NOT_YET_BUILT` slot),
`NOTEBOOK_ONLY_PHASES` (phases whose notebook writes config but no module to submit),
`PHASE_NOTEBOOKS`. Adding or reordering a stage edits that list; the loop does not change.

The loop body is one primitive, applied by `_run_phase_range` to every phase in range:

1. **Parametrize.** `resolve_phase` (`scripts/brieflow_resolvers.py`) picks tiles, patches
   values into the notebook's marker blocks, and sweeps parameters inside the bands declared in
   `data/lab_defaults.yaml` (`sweep_bands`, `sweep_joint`, `sweep_tiles`). Sweeps run in a live
   kernel so only cells downstream of the changed parameter recompute
   (`scripts/brieflow_marimo.py`, `scripts/brieflow_sweep.py`); `run_phase_notebook` then runs
   the notebook as a script to flush config to disk.
2. **Validate.** `run_preflight` shells out to `scripts/brieflow_preflight.py`, which derives
   required config keys and accepted-value surfaces from the pipeline's own rule and script
   sources and checks the written config against them. A HIGH finding aborts pre-submit.
3. **Run under a monitor.** `run_one_phase` in `scripts/brieflow_runloop.py` launches, samples
   liveness, classifies failures and resumes — see §3.
4. **Gate.** `build_phase_qc` + `qc_gate_verdict` in `scripts/brieflow_qc.py` read the eval
   outputs the pipeline already writes, apply the thresholds in `data/qc_gates.yaml` and write
   a summary. A failing gate pauses the run.

A notebook-only phase stops after step 1. The interview for the *whole* screen resolves once,
before the loop (`resolve_all_phases`), so the loop needs no operator input and pauses only on
a classified blocker. `run_screen` returns 0 on completion and a distinct code per blocker
(1 QC gate, 2 run failure, 3 unresolved template gap, env or range, 4 disk, 5 notebook,
6 preflight, 7 upstream-rerun guard, 8 awaiting launch approval, 9 raw input data not in
the expected layout).

## 2. State to keep

Everything the run depends on or decides lives in a file, never only in the agent's context:
the run's own state in the analysis directory next to the outputs, lab policy in the plugin's
`data/`.

| Role | File | Written by |
|---|---|---|
| Operator facts about the experiment | `screen.yaml` | the operator |
| Parameters, each with its provenance | `.brieflow/interview.json` | `scripts/brieflow_interview.py` |
| Policy — what passes, and the defaults and sweep bands | `data/qc_gates.yaml`, `data/lab_defaults.yaml` | maintainer |
| Policy — per-rule memory | `slurm/mem_recommendations.json` | `scripts/brieflow_recover.py`, `scripts/brieflow_aggregate_efficiency.py` |
| Run provenance | `.brieflow/run_manifest.json` | `scripts/brieflow_manifest.py` |
| Pre-launch review | `.brieflow/launch_review.html` + `.json` | `scripts/brieflow_launch_review.py` |
| Decision journal | `.brieflow/decisions.jsonl` + `.md` | `scripts/brieflow_decisions.py` |
| Defects found, and workarounds made in place | `.brieflow/findings.json` | the agent, via `record_finding()` (`scripts/brieflow_findings.py`), required by every skill |
| Resume state | `.brieflow/progress.json` | `scripts/brieflow_runner.py` |
| Gate results | `brieflow_output/<phase>/eval/qc_summary.json` | `scripts/brieflow_qc.py` |
| Escalation draft | `.brieflow/issue_draft.md` + `.json` | `scripts/brieflow_issue_draft.py` |
| Upstream reports and receipts | `.brieflow/issues/` | `scripts/brieflow_issue.py` |

**Provenance is a field, not a convention.** Every parameter row in `.brieflow/interview.json`
carries a `value_source` from a closed set — `operator`, `screen-yaml`, `lab-default`,
`auto-probe`, `gen`, `tuned-confirmed`, `default` (`VALID_VALUE_SOURCES` in
`scripts/brieflow_buckets.py`), so a reviewer can ask of any number: who chose this? A required fact left blank in `screen.yaml` stops the run before anything else (`brieflow_screen_yaml.py`), and any other blank field is *asked*, never guessed: the row stays unresolved and surfaces as a gap that stops the run.

**The manifest is a version stamp, not a rerun guarantee.** `.brieflow/run_manifest.json`
(schema `brieflow-run-manifest/v1`) records which harness version, pipeline commit, screen
repo commit, interpreter and flags produced this run — enough, for a project still in
evolution, to explain a result and tell two runs apart. It does not promise that replaying those
versions reproduces the numbers, and `scripts/brieflow_manifest.py` says so in its own docstring.

**Decisions are journaled separately from parameters.** `scripts/brieflow_decisions.py`
appends to `.brieflow/decisions.jsonl` (schema `brieflow-decisions/v1`) and re-renders
`.brieflow/decisions.md`: choices among alternatives and judgment calls, each tagged `auto` (a
mechanical loop decision), `agent` (an LLM judgment call) or `operator` (a person's approval).
Routine parameter fills stay out; they carry `value_source`.

**Resume is a file, not an inference.** `.brieflow/progress.json` lists the phases already done
and the loop skips them, so re-running the same command after a fix continues.

## 3. Failure taxonomy

Blind retry wastes cluster time on failures that will never succeed, and abandons ones that would.
The recovery path splits into three layers of differing portability:

- **Extraction** — `scripts/brieflow_failure_logs.py` finds the workflow master log and parses
  per-rule error contexts; the adapters in `scripts/brieflow_backend.py` locate a rule's
  scheduler logs and query accounting for job state. Entirely shaped by the execution stack.
- **Classification** — `classify_failure` in `scripts/brieflow_classify.py` maps log text plus
  an accounting row to one class (`recoverable_oom`, `recoverable_fd_limit`, `nfs_latency`,
  `config_mismatch`, `unknown`); pure functions over extracted text, no I/O.
- **Policy** — `scripts/brieflow_recover.py` decides what each class earns and owns the constants
  `OOM_FIRST_BUMP_X`, `OOM_REPEAT_BUMP_X`, `PEAK_AT_CAP_FRAC`, `MAX_REASONABLE_MEM_MB`,
  `LATENCY_BUMP_LADDER`. OOM bumps that rule's memory in `slurm/mem_recommendations.json` and
  resumes (on `--backend local` it escalates); FD exhaustion resumes after
  `scripts/brieflow_run.py` raises the open-file limit to the hard cap; latency waits and
  resumes; config-mismatch and unknown escalate immediately.

`scripts/brieflow_runloop.py` applies the action, capped at `max_attempts` (a `--screen`
default in `scripts/brieflow_run.py`), and escalates when the same rule fails with the same
fingerprint twice running. `scripts/brieflow_monitor.py`
supplies the liveness signals and the cadence (burst at a boundary, steady mid-phase, idle
when quiet) and detects a hang or an OOM cascade; it is notification-free by design — turning
signals into notifications is the caller's job.

## 4. Escalation to upstream issues

Some failures are defects in the pipeline, not in the run. The agent opens issues for these
defects automatically, without claiming to speak for the operator. On any escalating exit from the phase loop, and on
a failing pre-phase hook — but never on a pre-launch return (a template gap, a missing env, a
bad range), which drafts nothing — `run_screen` calls `write_issue_draft`
(`scripts/brieflow_issue_draft.py`), which assembles `.brieflow/issue_draft.md` and `.json`
(schema `brieflow-issue-draft/v1`) from the recovery action, the diagnostic bundle
(`scripts/brieflow_diag.py`) and `run_manifest.json`, with
`<operator: fill in>` placeholders for facts the runner cannot establish. These are local
evidence files, not completed issues. `skills/brieflow-issue/SKILL.md` investigates them,
checks for an existing report, and calls `scripts/brieflow_issue.py` to open a complete
GitHub issue automatically. No per-issue confirmation is required. A report follows
Brieflow's bug template and includes a minimal general fix proposal, explicit backward-
compatibility requirements, and regression tests for both existing and new inputs.
The agent must not alter global defaults, weaken validation, or hardcode a screen-specific
exception to make one dataset pass. Proposed fixes are not applied during the run.

The submission helper saves `filed_url` under `.brieflow/issues/` and journals `issue-filed`.
Identical reports are deduplicated, including closed issues. An uncertain submission is
not blindly retried. Raw diagnostic bundles remain local; only evidence cleared for public
sharing goes in the report. Authentication or network failures leave the report locally
and are surfaced as filing failures. Standalone runner execution still needs an agent to
complete this handoff: selecting public evidence and proposing a compatible fix are agent
tasks, not deterministic runner actions. The following historical deployments illustrate
the failure-to-maintenance loop, not an evaluation of automatic issue submission:

- **Opera Phenix support** (harness v0.5.0–v0.5.2). A Phenix-format screen surfaced three
  upstream defects, fixed in brieflow PRs #246 (file-type stage metadata reader), #248
  (per-cycle metadata extraction) and #249 (well-id splitting).
- **Frac-caller calibration.** A miscalled base channel was traced to dye balance and fixed
  upstream in brieflow #259, with the analysis-side change in brieflow-analysis #88; the
  harness gained `run_dye_balance_probe` / `apply_dye_balance` in `scripts/brieflow_runner.py`,
  calibrating on the test tile before SBS preflight.

Once the fix lands and the harness is bumped, re-running `bin/brieflow-run` resumes from
`.brieflow/progress.json` at the failed phase.

## 5. Where the agent runs

The agent runs on the host that holds the data and the environment — a cluster login node, a
workstation, or a cloud VM. Two facts force this: the interview's auto-probes read raw image
data directly, and the runner is a long-lived process that must outlive any interactive
session, reached over ssh, tmux, or a service unit.

Cloud provisioning (`cloud/provision_vm.sh` and its neighbours) is an *optional pre-step* that
produces such a host. From `bin/brieflow-run` onward the flow is identical on every host; only
`--backend` and `--lifecycle-hooks` differ, and `scripts/brieflow_hooks.py` is the only place
in the phase loop that moves data — the one runner exception is the opt-in
`--free-raw-input-after-preprocess` path, which deletes raw input only once a copy is verified
elsewhere. Hook failures are graded: a raising pre-phase hook is rc 2 with a draft (inputs
never arrived), a raising post-terminal hook only warns (the work is done, the outputs may not
be off-host), and the failure and teardown hooks never raise. **Remote orchestration is not
implemented** — no mode has the agent sitting elsewhere, driving probes over the wire.

## 6. The launch gate

Before a run spends compute, the operator reads one page. After the interview resolves and
before the first phase, `scripts/brieflow_launch_review.py` writes `.brieflow/launch_review.html`
and `.json`: the phases in range, every active parameter with its provenance, remaining gaps,
screen geometry, disk required per phase, compute identity, hooks and the manifest. Unless this
invocation passed `--approved` *and* the review already on disk matched, `run_screen` returns 8
(awaiting launch) and stops — not a failure, so no issue draft and no failure hook.

The operator reads the page and re-runs the same command with `--approved`. Approval is
checked against the review a *previous* invocation left on disk: `read_prior_review_hash` is
called before the fresh review overwrites the file, and `launch_approved` requires the two
hashes to match. A first run, a changed range, backend or parameter therefore cannot be
approved by a flag alone — the page has to be re-read. A review that cannot be written leaves
the gate closed and the run refused. Approval is journaled as a `launch-approved` decision
with `source="operator"`.

## 7. Agent boundary

Stated once, here, and enforced by the scripts the agent is given.

**MAY:** inspect inputs, logs, metrics, images; set declared parameters within the bands
in `lab_defaults.yaml`; write config and run artifacts; launch, resume, recover or pause
through the provided scripts; journal decisions and findings.

**MAY NOT:** edit pipeline source (`flow.sh`, the brieflow library) during a run; change QC
thresholds; guess an operator-owned fact; retry an unknown failure; publish private run data
(raw diagnostic bundles stay local; see §4).

The notebook sweep writes into a live marimo kernel but only to change declared parameters.

## 8. Skill and command template

Two document shapes carry the harness's instructions to the agent. A **command**
(`commands/*.md`, e.g. `commands/brieflow-run.md`) is the user-facing entry point: YAML
frontmatter with a single `description:` line, then one paragraph saying what it does, a
short usage block, and a pointer to the skill that holds the protocol. A **skill**
(`skills/<name>/SKILL.md`, e.g. `skills/brieflow-run/SKILL.md`) is the protocol: frontmatter
with `name` and a trigger-phrase `description`, then the ordered steps, the preconditions under
which the command refuses, and the rules; long reference material moves to
`skills/<name>/references/*.md`, loaded on demand.

The rule that keeps both honest: **numbers live in code or policy files, and the prose points
at them.** A skill says the gate thresholds are in `data/qc_gates.yaml` and the sweep bands in
`data/lab_defaults.yaml`; it does not restate the values, because a restated value drifts
silently and the agent believes the stale copy. `tests/test_docs_match_code.py` is the gate:
every command-line flag a doc names must be declared by some script's argparse (or be an
external tool's, listed with its owner), every `scripts/`, `data/`, `commands/`, `skills/`,
`cloud/` and `bin/` path must exist, the version strings must agree, and retired names must be
gone. Docs fail the build like code does.

## 9. Porting checklist

**Reuse as-is — stage-independent; the brieflow-auto file for each step is named.**

1. **The stage table.** Copy `scripts/brieflow_phases.py`'s shape — one ordered list of
   `(stage, unit-of-work)` pairs plus derived views — and swap the contents.
2. **The loop.** `run_screen` and `_run_phase_range` in `scripts/brieflow_runner.py` are
   stage-independent control flow: skip done, check resources, parametrize, validate, run,
   gate, mark done, pause. Keep the structure and the distinct return code per blocker.
3. **The monitor.** `scripts/brieflow_monitor.py` is already notification-free; reuse it as-is.
4. **The execution seam.** `scripts/brieflow_backend.py` is already a `Protocol` with two adapters;
   write a third for your executor rather than branching on it elsewhere.
5. **The classification skeleton.** `classify_failure` in `scripts/brieflow_classify.py` is
   pure regex over log text and a job record; keep the shape and match evidence, not mentions.

**Re-implement — these are bound to the stack.**

6. **The stage body.** `run_phase_notebook` in `scripts/brieflow_runner.py` and the
   live-kernel sweep that runs before it in `resolve_phase` (`scripts/brieflow_resolvers.py`,
   with `scripts/brieflow_marimo.py` and `scripts/brieflow_sweep.py`) assume marimo notebooks with marker blocks. A script or
   container unit of work rewrites both.
7. **Failure extraction.** `scripts/brieflow_failure_logs.py` and the adapters in
   `scripts/brieflow_backend.py` are shaped by slurm accounting and Snakemake logs; a different
   scheduler rewrites them entirely.
8. **The gates.** `scripts/brieflow_preflight.py` reads a Snakemake rule tree and
   `scripts/brieflow_qc.py` reads pipeline-specific eval tables — both schema-shaped,
   both rewritten. Keep the two-gate structure: validate before compute, gate after.

**Policy you own — do not inherit these values.**

9. **Recovery constants** in `scripts/brieflow_recover.py` (bump factors, memory cap, latency
   ladder) are tuned to one cluster. The *math* ports; the numbers do not.
10. **Thresholds and bands** in `data/qc_gates.yaml` and `data/lab_defaults.yaml` encode what
    your field calls a passing result and a sensible search range. Start empty; fill from
    measurements, with a comment recording which run justified each number.

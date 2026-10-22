---
name: brieflow-setup
description: Set up a new screen on an existing host or a provisioned GCP VM, with a verified remote login handoff, staged inputs, analysis repository, environment, and screen.yaml. Use for onboarding, VM setup, connecting to the execution workspace, or continuing setup on the VM. Claude Code and pipeline execution stay on the execution machine; this is not a remote job dispatcher.
---

# Brieflow — First-time screen setup

Onboards a new OPS screen from scratch. Re-entrant: every invocation reads saved setup
context and confirms the current machine's role before selecting the next action. Operator drives transitions
by re-invoking the skill — no active polling or wait loops.

Setup's job is the machine, not the experiment: provision or confirm the host, build and
confirm the conda env, stage the raw data, and check that everything is in place. The
experiment description is the operator's `screen.yaml`; setup stages it into
`analysis/screen.yaml` and stops if only the blank template is there. It never interviews the
operator to write it.

For remote screens, provisioning happens on the setup workstation. The operator then
connects to the execution machine and starts or reconnects to Claude Code there.
The session does not automatically migrate between hosts.

## Where the agent runs

The agent session runs on the host that holds the data and the env — a cluster
login node, a workstation, or a cloud VM. Provisioning a VM is an optional
pre-step for operators who don't already have such a host; everything from
`bin/brieflow-run` onward is identical on every host, and only `--backend` and
`--lifecycle-hooks` differ. The client is a remote interface, not a pipeline controller:
Claude Code's process, tools, notebooks and runner live on the execution machine.
The model is served by its provider. SSH may provision access and verify identity, but
must not be used to drive pipeline execution from another machine.

Before any remote handoff, read and follow
[`references/connection_handoff.md`](references/connection_handoff.md). Generate a saved
connection with `scripts/brieflow_connection.py`, verify the remote account and directory,
and give the operator the exact connection and tmux reconnection instructions. Never infer
the VM username from the client's username or the email local-part. A blocked directory
check is not setup completion; finish on-machine setup and verify again.

## The plugin is agnostic

This plugin describes the **workflow**. For the GCP provisioning path, operators bring their own:

- **GCP project + billing** (no defaults baked in)
- **Bucket names** — must set `BRIEFLOW_BUCKET_INPUT` + `BRIEFLOW_BUCKET_OUTPUT`
  env vars (no defaults; bucket names are GCS-global and lab-specific)
- **Source paths on HPC** + screen slug (may differ between HPC and GCP — rename
  is supported at the transfer boundary)

Per-screen run scripts (transfer sbatch, VM first-boot script) are generated
**ad-hoc into `tmp/`** or live in the screen's analysis repo — they are not
shipped in the plugin. The plugin's `cloud/*.sh` reference implementations are
useful starting points but operators frequently run gcloud commands directly.

## Detect-state first

Every invocation must:

1. Establish whether this is the setup workstation or the intended execution machine.
   `detect_host` from `cloud/_common.sh` reports `vm` (GCP), `hpc` (Slurm tools), or
   `local`; these are capability hints, not proof of role or ownership. Check the saved
   connection and remote identity instead of dispatching solely from host detection.
2. Determine screen slug from cwd, `screen.yaml`, or AskUserQuestion.
3. (Optional) If `gs://${BRIEFLOW_BUCKET_OUTPUT}/<screen>/setup_state.json`
   exists, read it for resume context.
4. Report current stage to operator before doing anything:
   *"You're on <host>. Screen <slug> looks to be at stage S<N>. Next: <action>."*
5. Confirm with operator before taking the next destructive action (VM
   provisioning, sbatch submission, repo clone, env create).

The GCS setup state remains optional. The local connection record and a verified entrance
to the workspace are required for a remote handoff, including one-shot setups.

## Compute target branches

Ask at S1 if not already known:

- `slurm` or `local` → existing-host flow (on the chosen execution machine)
- `cloud-gcp`        → cloud flow (S1-S8, split across HPC and VM)

See [`references/cloud_flow.md`](references/cloud_flow.md) for the per-stage
cloud walkthrough and [`references/gcp_bootstrap.md`](references/gcp_bootstrap.md)
for the one-time GCP project setup (project APIs, default VPC, firewall, 2
buckets, service account + IAM).

---

## Cloud flow (compute_target = cloud-gcp)

### Pre-flight: one-time GCP bootstrap

Before the first screen on a fresh GCP project, follow
[`references/gcp_bootstrap.md`](references/gcp_bootstrap.md). Covers:

- `gcloud auth login` (ADC optional, defer unless using Python GCS clients)
- Enable compute / storage / IAM APIs
- **Create default VPC network** (fresh projects don't have one)
- **Add firewall rules** (internal + IAP SSH)
- Create 2 GCS buckets (operator-named)
- Create VM service account + IAM bind on both buckets

Run once per project. Idempotent. Skip if already done.

### Per-screen stages

```
─── HPC session ─────────────────────────────────────────
S1  Intent interview                  AskUserQuestion: screen slug,
                                       source paths (SBS + phenotype),
                                       raw barcode/design table path,
                                       expected data size, partition,
                                       email, repo URL (deferrable),
                                       machine type + zone.
                                       Supports source-name → GCP-name
                                       rename.
S2  Provision VM                       FIRST: cloud/preflight_quota.sh
                                          --machine-type <type>
                                          --concurrency <N>
                                       (per-VM-family CPU quota vs fleet;
                                       reads GRANTED limit, not requested —
                                       a 1500 ask may grant 1380).
                                       Then: cloud/provision_vm.sh <screen>
                                          --data-disk-tb <ceil(3 × input)>
                                       (3× = input + output both resident on
                                       /data all run; see cloud_flow.md S2.
                                       1.5× was wrong — undersizes every run)
                                       Tries zones in fallback order
                                       (a → b → c → f). Provisions VM
                                       in first zone with stock, then
                                       creates + attaches data disk in
                                       same zone. Optionally stop the VM
                                       immediately to save cost during S3.
S3  Source → GCS transfer              Branch on where the raw data lives:
                                       a) HPC source (slurm-managed):
                                          cloud/transfer_hpc_to_gcs.sh
                                          (generates sbatch job array;
                                          BEGIN/END/FAIL email)
                                       b) Local machine source (laptop):
                                          cloud/transfer_local_to_gcs.sh
                                          (parallel rsync in tmux + sleep
                                          prevention via caffeinate /
                                          systemd-inhibit)
                                       c) Ad-hoc sbatch / shell in tmp/
                                          for any flow not covered.
                                       All: parallelism via env vars
                                       (CLOUDSDK_STORAGE_PROCESS_COUNT etc),
                                       NOT `gcloud config set --installation`
                                       which fails on compute nodes.
S3.5 Handoff                           Save connection.json with actual VM,
                                       zone, cloud account and remote user.
                                       Verify identity; directory may await S6.
                                       Print verified status + slug + bucket
                                       URIs + sbatch jobid as plain text.
                                       Optionally write setup_state.json
                                       to gs://${BRIEFLOW_BUCKET_OUTPUT}/
                                       <screen>/ for re-entrance.

S3.6 Seamless handoff prep             Before printing handoff instructions:
                                       a) Tar plugin → tmp/brieflow-auto-plugin.tgz
                                       b) Start VM (if STOPPED during S3)
                                       c) gcloud compute scp BOTH the plugin
                                          tarball AND vm_first_boot_<screen>.sh
                                          onto the VM via --tunnel-through-iap.
                                       Net effect: operator's only remaining
                                       action is one SSH + one bash command.
                                       HARD STOP: orchestrator prints handoff
                                       and EXITS. No host-driven S5+ (A2).

─── operator leaves earth (slurm email triggers next stage) ──

─── VM session ──────────────────────────────────────────
S4  SSH to VM via IAP                  Use the saved connection's printed command
                                       (explicit user, account, project and zone).
S5  First-boot setup                   On the VM, run BY THE OPERATOR as
                                       themselves (script scp'd in S3.6):
                                          - format + mount data disk to /data
                                          - install miniforge (NOT miniconda;
                                            avoids conda 26.x ToS + anon-usage
                                            sqlite lock) + git + tmux
                                          - install gh from official apt repo;
                                            operator runs `gh auth login` as
                                            themselves (private-repo creds)
                                          - create the analysis root:
                                            $BRIEFLOW_DATA_ROOT/
                                          - clone brieflow-auto into
                                            $BRIEFLOW_DATA_ROOT/brieflow-auto/
                                            and install via:
                                            bash ~/install_plugin.sh \
                                              --dir $BRIEFLOW_DATA_ROOT/brieflow-auto
                                            (NOT --git, which puts the clone
                                            inside ~/.claude/plugins/)
                                          - persist env vars (BRIEFLOW_BUCKET_*,
                                            PROJECT, ZONE, VM_USER, DATA_ROOT)
                                            to ~/.brieflow_env
                                            sourced from ~/.bashrc; ALSO export
                                            BRIEFLOW_OPS_PLUGIN_DIR=$BRIEFLOW_DATA_ROOT/
                                            brieflow-auto/plugins/brieflow-auto so
                                            flow.sh's resource resolver runs on the
                                            VM (without it the mem-cap resolver
                                            no-ops → aggregate OOM)
                                          - gcloud storage rsync GCS → /data
                                       Operator's final action:
                                          claude   # plugin auto-activates
                                          /brieflow-setup   # picks up at S6
S6  Repo setup                         git clone <repo URL> \
                                          $BRIEFLOW_DATA_ROOT/<screen>-analysis
                                       cd $BRIEFLOW_DATA_ROOT/<screen>-analysis
                                       git submodule update --init --recursive
                                       conda create -n brieflow_<screen> \
                                          -c conda-forge python=3.12 uv pip -y
                                       conda activate brieflow_<screen>
                                       cd brieflow
                                       uv pip install -r pyproject.toml
                                       uv pip install -e .
                                       Do NOT install cpsam / stardist deps here —
                                       they are segmentation-model-specific and
                                       installed at the phenotype step ONLY when
                                       the interview lands on that model (see
                                       brieflow-notebook). Keeps the base env lean
                                       and avoids a heavy torch pin every screen
                                       doesn't need.
S7  Stage screen.yaml                  The operator writes screen.yaml; setup
                                       stages it and never authors it:
                                         brieflow_screen_yaml.py find
                                           --analysis-dir analysis
                                           [--candidate <path the operator gave>]
                                         brieflow_screen_yaml.py stage
                                           --analysis-dir analysis --source <it>
                                       (keeps the template beside it as
                                       screen.yaml.template-<timestamp>).
                                       None found → STOP: run
                                         brieflow_screen_yaml.py check
                                           --analysis-dir analysis
                                       and show the operator the fields it
                                       lists; they fill them in the template
                                       and re-invoke /brieflow-setup. Do not
                                       ask for the fields one by one and do
                                       not fill them yourself.
                                       Setup itself sets only the host facts
                                       it created, after the operator confirms:
                                          pipeline.conda_env       = brieflow_<screen>
                                          data.input_sbs_dir       = /data/<screen>/input_sbs
                                          data.input_phenotype_dir = /data/<screen>/input_ph
                                       (modality anchors; S8 reshapes the tree
                                       under them). Provenance (source paths,
                                       rename, transfer jobid, dates) goes to a
                                       co-located analysis.md, NOT screen.yaml.
S8  Sanity check + handoff             brieflow_screen_yaml.py check exits 0
                                       barcode/design table in analysis/config/
                                       env activates
                                       /data/<screen>/ tree validated +
                                         reshaped to canonical
                                         (brieflow_validate_tree.py — see below)
                                       write setup_state.json for re-entrance
                                         (write_setup_state, cloud/_common.sh)
                                       → return to router → brieflow-notebook
```

### Config co-travel: barcode + screen.yaml

The raw barcode/design table (`library.raw_design_table_fp` in screen.yaml,
e.g. `config/pool10_design.tsv`) is an operator-supplied artifact, not
derivable from the data. It must travel with `screen.yaml` from the upload
location onto the VM:

1. **S1 (HPC)** — collect the barcode/design table path from the operator.
2. **Upload (HPC)** — stage `screen.yaml` + the barcode file together in the
   upload location (the GCS launchpad / staging dir), then push to GCS. They
   ride alongside the raw-data transfer but as small config objects, kept under
   a `_config/` prefix so they're not mixed into the `plate_*/` data.
3. **VM landing** — pull both down into the cloned analysis repo:
   `screen.yaml` → `analysis/screen.yaml`, barcode → `analysis/config/`
   (matching the `config/...` path in `raw_design_table_fp`). brieflow reads
   them from there.

A blank `raw_design_table_fp` is a hard stop — ask the operator; never guess.

### Raw data staging (S8 on the VM, step 3 on an existing host): the agent's job

The raw data must be in the layout brieflow reads before anything runs. Getting it there is
the agent's job, not the operator's: the agent inspects the tree, reshapes what it can, and
asks the operator only what the tree cannot tell it. The expected structure is printed by
`scripts/brieflow_validate_tree.py --layout`. When the operator asks what the data should
look like, show that output verbatim. The runner refuses inputs that are not in the layout
(exit 9), on every launch and after a GCS pull.

1. **Check the inputs screen.yaml names** —
   `brieflow_validate_tree.py --inputs-from analysis/screen.yaml` (read-only). Exit 0 = in
   the layout; 2 = a reshape is pending; 1 = blocked; 3 = in the layout, but something needs
   the operator's confirmation. On a fresh pull, plan against the whole tree instead:
   `--root /data/<screen> --screen-yaml analysis/screen.yaml` (the reshape below needs the
   root; always pass the cycle count so surplus cycles are parked).
2. **Reshape (exit 2)** — the plan lists:
   - **rename** alias dirs (`sbs→input_sbs`, `cycle_1→c1`, `Plate_1→plate_1`, `r1→round_1`);
   - **move** = the plate-major→modality-major transpose (raw GCS data often arrives as
     `plate_<N>/input_sbs/c<M>/`);
   - **wrap** single units (loose phenotype images → `round_1/`; no plate level → `plate_1/`);
   - **park** surplus cycles above `iss.cycles` → `_excluded_cycles/plate_<N>/c<M>/`, and
     junk dirs or stray non-image files → `_non_image/`.

   Show the plan in one AskUserQuestion (moves relocate real data), then `--apply` with the
   same arguments; it runs to a fixpoint and re-validates. Reshape only the operator's own
   copy of the data, never a shared archive in place: stage a copy or a symlink tree first.
3. **Blockers (exit 1)** — never auto-fixed, but resolved by the agent with the operator,
   one AskUserQuestion per decision, and recorded with `record_finding()` (R5):

   | Code | What it means | What the agent does |
   |---|---|---|
   | `cycle-unseparated` / `round-unseparated` | images sit in one folder, cycles or rounds told apart only by file name | propose a file→cycle (or →round) mapping from the vendor metadata, folder names or the acquisition manifest, have the operator confirm it, then build `c<M>/` / `round_<R>/` dirs (symlinks into the source) |
   | `cycle-duplicate-acquisition` / `round-duplicate-acquisition` | the same file names in two subfolders of one cycle or round: two exports of the same fields | ask which export the screen uses; move the other under `_excluded_acquisitions/`. A later phenotype pass is its own `round_<R>/`, not a duplicate |
   | `cycle-unit-empty` / `round-unit-empty` | a cycle or round dir with no images | ask whether the data is missing (re-pull) or the dir is spurious (park it) |
   | `round-plate-mismatch` | plates carry different phenotype rounds | ask which rounds each plate really has; restage the missing round or park the extra |
   | `cycle-gap`, `*-collision`, `*-mixed`, `ambiguous-root`, `missing-*` | gaps below the declared count, two dirs for one index, loose images beside unit dirs, both layouts at the root, a missing modality | show the paths and ask how the acquisition was meant to be split |

4. **Confirmations (exit 3)** — in the layout, but worth a question: per-plate cycle counts
   that differ (reconcile with `additional_notes` via `census_tree(<root>,
   declared_cycles=<iss.cycles>)` → `verify_notes_against_data(screen, census)` in
   `brieflow_interview.py`), or phenotype rounds that hold different numbers of image
   files (expected when rounds image different channel counts as separate files;
   otherwise a round is missing fields or holds an extra acquisition). Ask once; on a yes,
   the run proceeds with `--accept-layout`, which is journaled.
5. **Re-pull durability** — `_`-prefixed sidecars are ignored by the planner, so `--apply`
   is idempotent. Before any GCS re-pull, write the filter file (`--rsync-filter
   analysis/rsync_filter.txt`) and pass it to the pull so a still plate-major bucket does
   not re-introduce parked or reshaped-away data; re-check after every pull regardless.

### Multi-plate with intentionally divergent cycle counts (branch-and-converge)

When plates were acquired with **different SBS cycle counts on purpose** (e.g.
jannu: plate 1 = 12 cycles, plate 2 = 11), do NOT let S8 park down to uniformity:

- **Validate with `--cycles <MAX>`** (the longer plate's count). `--cycles <MIN>`
  parks the extra cycle of the longer plate into `_excluded_cycles/` — silently
  dropping real data. The non-uniform warning is expected and correct here.
- brieflow has no per-plate SBS cycle *config*; cycle membership rides in the
  sample/combo tables (one union table is fine — `--only-plate` slices it at
  DAG-build). The only global SBS scalars that can't span divergent plates are
  `manual_background_cycle_index` and `skip_cycles_indices`.
- **Branch SBS, converge at merge:** two configs differing only in those scalars,
  both pointing at the same `root_fp`/tables, each run
  `flow.sh sbs --only-plate <N> --configfile config/config_p<N>.yml`.
  SBS outputs are `{plate}`-keyed so both plates coexist with no collision;
  `n_cycles` is runtime-derived per tile. Then run `phenotype merge aggregate
  cluster` ONCE over the union (no `--only-plate`) — merge is per-(plate,well),
  aggregate reads all plates from the combined tree.
- **The runner drives this branch natively** — no manual SBS pass. Declare the
  divergence as `iss.plate_overrides` in `screen.yaml` (per-plate `skip_cycles`,
  optional `manual_background_cycle`); the SBS phase then generates a
  `config/config_p<N>.yml` plus a per-plate barcode library and runs one SBS pass
  per plate, and the later phases converge over the union. Full contract:
  [`brieflow-run/SKILL.md`](../brieflow-run/SKILL.md).

---

## Existing-host flow (compute_target = slurm or local)

Operator does the analysis on one chosen execution host (workstation, HPC, or remote VM).
For an existing remote machine, first follow `references/connection_handoff.md`; a generic
SSH connection needs no cloud provisioning or alternate execution backend. For an already
local session, confirm the current account and analysis path without creating an SSH record.

Briefly:
1. Repo creation (operator clones from template)
2. Conda env (per brieflow docs, same as S6 above)
3. Raw data placement (already on shared FS, or rsync into place), then "Raw data staging"
   above until `--inputs-from analysis/screen.yaml` exits 0 (or 3, confirmed)
4. Stage the operator's `screen.yaml` as in S7 (`brieflow_screen_yaml.py find` / `stage`;
   stop on `check` if none is filled in), then set the on-prem `input_*_dir` paths
5. Establish compute backend + base slurm profile (see below)
6. Turn on run records (see below)
7. Sanity check
8. Exit to router → brieflow-run

### Compute backend + base slurm profile (step 5)

Never hardcode a partition/account — probe and establish them. Run
`brieflow_compute.py --probe` (JSON: `{backend, partitions, partition_default,
accounts}`):

- **`backend: local`** (no `sbatch` — e.g. a cloud VM): write nothing. The run uses
  `--backend local`; mem is advisory (bound `--cores`). Done.
- **`backend: slurm`**: the operator PICKS the partition — one `AskUserQuestion`
  seeded with `partitions` (never auto-select; the cluster default is often not the
  one you run on). Account is used automatically when a single candidate, else ask.
  Then `brieflow_compute.py --write --analysis-dir <analysis> --backend slurm
  --partition <chosen> --account <chosen>` writes `slurm/config.yaml` (base profile
  only — per-rule mem is sized at run time by `resolve_resources`, so no
  `set-resources`). `jobs`/`runtime`/`mem_mb` are general defaults, operator-editable.
  An existing `slurm/config.yaml` with no `slurm_partition` under `default-resources`
  (the analysis template ships one with partition and account commented out) is not
  established: probe and `--write` as above, which replaces it; `brieflow_run.py`
  refuses to submit with it.

### Run records (step 6, on by default)

Transcript capture is part of setup, not a later opt-in. Tell the operator once that the
captured Claude Code transcripts stay local and can contain private inputs, tool output and
credentials (details: `../../scripts/TRACE_RECORDS.md`). Unless they decline, enable capture
for the session workspace (the directory the operator's Claude Code session runs in) and add
the ignore rules to the screen repo:

```bash
python <scripts-dir>/brieflow_rollout_hook.py --enable-workspace <session-workspace> \
  --ignore-repo <screen-repo>
```

This writes `.brieflow/record_rollouts.json` and appends `**/.brieflow/rollouts/` and
`**/.brieflow/traces/` to the repo's `.gitignore`. If the operator declines, journal it with
`record_decision(kind="rollout-capture", phase="setup", choice="declined", why=..., source="operator")`. On a cloud VM,
the operator runs the same command on the VM after first boot.

Every workaround made during setup (an input symlink tree, a renamed channel, a
hand-written profile entry) is a finding: call `record_finding()` (router rule R5).

---

## Rules

- **Orchestrator stops at S3.6 (HARD STOP).** The HPC/orchestrating host does
  S1–S3.6 only (provision, transfer → GCS, push first-boot script + plugin
  tarball, print handoff) and then EXITS. It must NOT SSH in to run first-boot,
  clone the repo, build the env, stage screen.yaml, or launch the runner/notebook.
  Everything from first-boot onward is the operator's, **run as themselves on the
  VM** — driving VM setup from a foreign gcloud identity makes `/data`, the env,
  the repo, and `~/.claude` unreachable by the operator (A1/A2), skips their `gh`
  auth (A3), and bypasses the on-VM interview against real images (E2).
- **Verify the operator's VM identity before handoff.** Use OS Login's assigned
  username when enabled, otherwise an explicitly chosen Linux account, then verify it
  through the saved connection. Set `$BRIEFLOW_VM_USER` to that verified username only.
  A read-only identity/directory probe is allowed before the S3.6 handoff. Actual first-boot,
  environment setup and analysis remain on-machine operator actions. Never repair a login
  mismatch by creating another user, changing ownership, or disabling OS Login.
- **Quota preflight before provisioning.** Run `cloud/preflight_quota.sh` before
  any `provision_vm.sh`; it checks the per-VM-family CPU quota against the intended
  fleet and reports the *granted* limit (a request may be granted below ask).
  Distinct from zone stockout, which provision_vm.sh handles via zone fallback.
- **Re-entrant.** Every invocation re-reads state from cwd/GCS. No assumption
  about prior in-memory context.
- **Operator confirms destructive actions.** VM provisioning, sbatch submission,
  repo clone, env create, and any data-tree reshape (`brieflow_validate_tree.py
  --apply`). Each requires a single y/N.
- **No active polling.** After kicking off slow async work (S3 sbatch), exit
  cleanly. Don't sleep, don't sacct-loop.
- **Plugin stays agnostic.** No lab-specific defaults baked into plugin code.
  Bucket names, project IDs, machine types come from operator env vars / args.
  The `cloud/*.sh` reference implementations (provision, transfer, quota
  preflight, teardown) ARE shipped with the plugin and are canonical starting
  points (D3) — invoke them as `cloud/<script>.sh`. Only the per-screen
  *generated* artifacts they emit (filled-in transfer sbatch, the concrete
  `vm_first_boot_<screen>.sh`) live in `tmp/` or the analysis repo, never the
  plugin. If `cloud/` appears missing at runtime, the install is stale — reinstall
  with `install_plugin.sh`.
- **Match `brieflow_<screen>` env naming.** Per-screen env follows lab
  convention (one env per screen).
- **screen.yaml from template only.** Don't author ad-hoc. The cloned
  `<screen>-analysis` ships `analysis/screen.yaml` (the brieflow-analysis
  template scaffold) — fill that in place.
- **No baked-image env reliance.** Each screen's brieflow env is built fresh
  against the pinned submodule at S6.
- **Stop the VM during S3 transfer.** ~$30/hr c3d-highmem-360 + $4.7/hr 20 TB
  pd-ssd. Stop while waiting for the slurm transfer email; start again after.
- **Use `--tunnel-through-iap` for SSH.** Default firewall rules don't open
  port 22 to public IPs.

## References

- [`references/connection_handoff.md`](references/connection_handoff.md) — required for remote access, identity verification and runtime handoff

- [`references/gcp_bootstrap.md`](references/gcp_bootstrap.md) — **start here** for one-time GCP project setup
- [`references/cloud_flow.md`](references/cloud_flow.md) — per-stage cloud walkthrough (operator-readable)
- [`references/setup_state_schema.md`](references/setup_state_schema.md) — optional `setup_state.json` schema for re-entrance
- [`references/conda_env.md`](references/conda_env.md) — env build notes (zarr3-era caveats)
- [`references/teardown_flow.md`](references/teardown_flow.md) — end-of-screen teardown: parallel per-subtree rsync, gcloud regex pitfalls, verification, boot-disk snapshot for cheap respin
- [`references/machine_handoff.md`](references/machine_handoff.md) — mid-run VM switch (GPU↔CPU by phase class): same-zone disk reattach not copy, downsize-on-stockout, mtime-scramble→rerun-cascade avoidance
- `cloud/*.sh` — shipped helper scripts: `preflight_quota.sh` (pre-provision quota check), `provision_vm.sh`, `transfer_hpc_to_gcs.sh` (bounded-retry rsync), `transfer_local_to_gcs.sh`, `teardown_vm.sh`, `_common.sh`
- `brieflow/docs/source/2.installation_analysis_setup.md` — canonical install
  doc (S6 follows this verbatim)

## Exit

Return to router (`brieflow-auto`) once `screen.yaml` is filled, env activates,
and data is at the declared paths on the execution machine. Remote setup must also have a
verified connection to the analysis directory. Router dispatches to `brieflow-run`, which resolves
the full interview up front; `brieflow-notebook` is the per-phase route on request.

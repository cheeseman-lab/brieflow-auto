# Cloud flow — per-stage walkthrough

Reference for the `compute_target = cloud-gcp` path of `brieflow-setup`.

This doc covers **per-screen** setup. For one-time GCP project setup (project,
APIs, network, firewall, buckets, service account), see [`gcp_bootstrap.md`](gcp_bootstrap.md).

Total wall-time for a multi-TB screen: ~6-36 hours end-to-end, mostly bounded
by HPC outbound bandwidth at the transfer step. Operator's active attention
~30-60 min spread across stages.

## Standard VM directory structure

Every VM follows this layout. The skill and the operator should assume it
consistently — it is never screen-specific or ad-hoc.

```
/data/                     ← the attached data disk, mounted by provision_vm.sh
  <screen>/                ← raw nd2s, rsync'd from GCS at S5

$BRIEFLOW_DATA_ROOT/       ← analysis root; on a VM, a directory under /data
  brieflow-auto/           ← git clone of plugin (branch per screen, push PRs here)
  <screen>-analysis/       ← analysis repo (cloned at S6)
```

Keeping `$BRIEFLOW_DATA_ROOT` on the data disk rather than the OS disk puts
plugin dev and analysis work together, so the same structure survives disk
snapshots and can be reproduced identically on a replacement VM. On a login
node or workstation it is wherever the operator has room; nothing else in the
flow changes.

## Two buckets

The plugin uses exactly 2 buckets per project. Operator must set both env
vars; no defaults.

| Env var | Purpose | Path convention |
|---|---|---|
| `BRIEFLOW_BUCKET_INPUT` | Raw nd2s + barcode libs per screen | `gs://<input>/<screen>/{sbs,phenotype}/...` |
| `BRIEFLOW_BUCKET_OUTPUT` | Everything else — outputs, optional state file, telemetry, review artifacts | `gs://<output>/<screen>/{output,reviews,telemetry}/...` |

## Stages

```
─── HPC session ─────────────────────────────────────────
S1  Intent interview                  AskUserQuestion: screen slug, source
                                       paths, expected data size, email,
                                       partition. Repo URL deferrable.
S2  Provision VM                       cloud/provision_vm.sh <screen>
                                          --data-disk-tb <ceil(3 × input)>
                                       (zone fallback: a → b → c → f)
S3  HPC → GCS transfer                 cloud/transfer_hpc_to_gcs.sh <screen>
                                          --sbs-dir <...> --phenotype-dir <...>
                                          --email <op> --partition <slurm>
                                       → capture sbatch jobid
                                       (optional: rename — src screen name on
                                       HPC may differ from GCP screen name)
S3.5 Handoff                           Print SSH command + slug + bucket URIs
                                       + sbatch jobid in plain text.
                                       (Optional: write setup_state.json to
                                       gs://${BRIEFLOW_BUCKET_OUTPUT}/<screen>/
                                       for re-entrance — see setup_state_schema.md)

─── operator leaves earth (sbatch email triggers next stage) ──

─── VM session ──────────────────────────────────────────
S4  SSH to VM via IAP                  Use connection.json's printed command
                                       with explicit user/account/project/zone.
S5  First-boot setup                   On the VM (script scp'd in S3.6):
                                          - format + mount data disk to /data
                                          - install miniforge + git + gh + tmux
                                          - create $BRIEFLOW_DATA_ROOT/
                                          - git clone brieflow-auto →
                                            $BRIEFLOW_DATA_ROOT/brieflow-auto/
                                            install_plugin.sh --dir (not --git)
                                            checkout -b <screen>
                                          - write ~/.brieflow_env + source from
                                            ~/.bashrc
                                          - gcloud storage rsync
                                            gs://$BRIEFLOW_BUCKET_INPUT/<screen>/
                                            → /data/<screen>/
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
                                       (cpsam / stardist deps are NOT installed
                                       here — they go in at the phenotype step
                                       when the interview picks that model; see
                                       brieflow-notebook Step 3b.)
S7  Stage screen.yaml                  Stage the operator's screen.yaml
                                       (brieflow_screen_yaml.py find / stage);
                                       stop on check if none is filled in.
                                       Set only conda_env and the
                                       input_sbs_dir / input_phenotype_dir paths.
S8  Sanity check + handoff             screen.yaml parses
                                       barcode_library.tsv in place
                                       env activates
                                       /data/<screen>/{sbs,phenotype} have files
                                       → router → brieflow-notebook
```

---

## S1 — Intent interview (~5 min)

The wizard collects:

| Field | Example | Used at |
|---|---|---|
| Screen slug (GCP-side name) | `<screen>` | All downstream paths |
| Source SBS dir on HPC | `<source-root>/<source-name>/plate_*/input_sbs` | S3 transfer |
| Source phenotype dir on HPC | `<source-root>/<source-name>/plate_*/input_ph` | S3 transfer |
| Expected total raw data size (TB) | `13` | S2 data disk = ceil(3 × input) |
| Slurm partition for transfer | `<partition>` | S3 sbatch |
| Operator email | `<your-email>` | S3 `--mail-user` |
| Repo URL (analysis repo) | `https://github.com/<org>/<screen>-analysis.git` | S6 (deferrable) |

**Rename support.** The HPC source name and the GCP screen name can differ:
`<source-root>/<source-name>/` → `gs://${BRIEFLOW_BUCKET_INPUT}/<screen>/`. The
transfer script's source paths are independent of the screen slug; rename
happens at the destination path.

**Pre-S1 operator action** (deferred until S6 is fine):
- Create the analysis repo manually: "Use this template" on
  [cheeseman-lab/brieflow-analysis](https://github.com/cheeseman-lab/brieflow-analysis),
  name it `<screen>-analysis`.

---

## S2 — Provision VM (~5 min when stock is available)

Three things happen here:

1. **VM provisioning** with the operator's chosen machine type
2. **Data disk creation** in the same zone as the VM (disks are zone-locked)
3. **First boot** — startup script mounts + formats `/data`

**Disk sizing — `ceil(3 × input)` (NOT 1.5×).** The `/data` disk holds the raw
**input** *and* all brieflow **output** (zarr conversion + segmentation + feature
tables) for the whole run — we don't prune or stream-to-GCS mid-run, so both are
resident at peak. Empirically output ≈ 1.3× input (snowdon: 13.4 TB in → 17 TB
out), so input + output ≈ 2.3× input; **3× input** keeps peak utilization
~70–75% with headroom for output variability. The old 1.5× factor undersized
*every* run — it can't even hold input + its own zarr conversion (a 10 TB disk on
6.5 TB input hit 83% mid-preprocess). pd-ssd grows online if a run gets tight:
`gcloud compute disks resize <disk> --size=<GB> --zone=<z>` then
`sudo resize2fs /dev/nvme0n2` — but mind the regional **`SSD_TOTAL_GB` quota**
(default 80 TB; a 4-screen 3× fleet plus stray disks can hit it).

**Quota preflight FIRST (D1/D2).** Before creating any VM, check the region's
per-VM-family CPU quota against the intended fleet. The generic create error
conflates quota with stockout, so a fleet that won't fit is otherwise discovered
only at the Nth provision. And a quota *request* may be granted below what you
asked (e.g. ask 1500, get 1380 → only 3×360-vCPU VMs fit, not 4) — the preflight
reads the live granted limit, not the requested value:

```bash
cloud/preflight_quota.sh --machine-type c3d-highmem-360 --concurrency 4
# exit 0 = fits; exit 1 = prints the deficit + the exact quota-request steps.
# Re-run after a quota grant to confirm the GRANTED limit actually fits.
```

The provision script tries zones in fallback order (default: `a → b → c → f`
within `BRIEFLOW_GCP_REGION`):

```bash
cloud/provision_vm.sh <screen> --data-disk-tb 20
```

**Stockout is real.** Large machine types (c3d-highmem-360, m2-megamem-*) often
fail with `reason: stockout` in popular zones. The fallback list typically lands
within 1-2 tries; if all zones fail, request a future-reservation via the GCP
console (24-48h lead time) or accept a smaller machine type.

**Cost ticker starts at S2.** Per-screen VM idles at $25-30/hr (c3d-highmem-360)
plus $4.7/hr for a 20 TB pd-ssd data disk. While waiting for S3 transfer, use:

```bash
gcloud compute instances stop <vm-name> --zone=<zone>
```

to pause compute charges (~$4.7/hr disk only). Start again post-transfer:
```bash
gcloud compute instances start <vm-name> --zone=<zone>
```
**Restart can stockout too** — same machine type, same zone, no guarantee of
capacity. For mission-critical timing, use a future reservation.

---

## S3 — Source → GCS transfer (~6-36 hours depending on data size + bandwidth)

Two variants depending on where the raw data lives. **Both produce the same
GCS layout:** `gs://${BRIEFLOW_BUCKET_INPUT}/<screen>/<source-subpath>/`.

### S3a — HPC source (slurm + sbatch)

Generated sbatch script does parallel `gcloud storage rsync` of each source
dir into `gs://${BRIEFLOW_BUCKET_INPUT}/<screen>/`. The wizard generates a
**job array** (one task per source dir) so partial failures restart cleanly.

```bash
cloud/transfer_hpc_to_gcs.sh <screen> \
    --sbs-dir <source-sbs-dir> \
    --phenotype-dir <source-phenotype-dir> \
    --email <operator-email> \
    --partition <slurm-partition>
```

### S3b — Local-machine source (laptop / workstation, no slurm)

`cloud/transfer_local_to_gcs.sh` wraps parallel rsync + tmux/screen +
sleep-prevention in one command:

```bash
cloud/transfer_local_to_gcs.sh <screen> \
    --src ~/data/<screen>/plate_1/sbs:plate_1/sbs \
    --src ~/data/<screen>/plate_1/ph:plate_1/ph  \
    --src ~/data/<screen>/plate_2/sbs:plate_2/sbs \
    --src ~/data/<screen>/plate_2/ph:plate_2/ph
```

Each `--src` is `<local-path>:<dest-subpath>`. The script:
- Launches a detached `tmux` session (`brieflow-upload-<screen>`) so the
  transfer survives terminal close
- Wraps in `caffeinate -i` (macOS) or `systemd-inhibit` (Linux) to prevent
  idle sleep
- Runs one `gcloud storage rsync` per source in parallel
- Uses env vars (`CLOUDSDK_STORAGE_PROCESS_COUNT/_THREAD_COUNT`) for
  parallelism

Re-attach: `tmux a -t brieflow-upload-<screen>`. Detach: `Ctrl-b d`.

**Bandwidth caveat for local sources.** Laptops typically have 100 Mbps – 1
Gbps outbound vs HPC's 1-10 Gbps. For 13 TB:
- 1 Gbps sustained → ~30 hours
- 100 Mbps sustained → ~12 days

Keep the machine plugged in. For genuinely huge transfers from off-cloud,
consider [Cloud Storage Transfer Service](https://cloud.google.com/storage-transfer-service)
with physical-media ingest (ship HDDs to Google).

**Critical pitfall — `gcloud config set --installation`:** The default
parallelism config writes to `/usr/local/google-cloud-sdk/` which requires
sudo. **HPC compute nodes don't have sudo for the user**, so this fails. The
issue is HPC-specific (your laptop owns its gcloud install) but the fix is
universal — **prefer env vars in any script you write:**

```bash
export CLOUDSDK_STORAGE_PROCESS_COUNT=16
export CLOUDSDK_STORAGE_THREAD_COUNT=4
```

Both `transfer_hpc_to_gcs.sh` and `transfer_local_to_gcs.sh` use env vars.
If you write ad-hoc sbatch scripts in `tmp/`, remember this.

**Throughput.** Practical sustained throughput from a single HPC compute node
to GCS lands around 200-500 MB/s (HPC outbound bandwidth is the bottleneck,
not GCS ingest). For 13 TB expect 6-18 hours. Monitor with:

```bash
sacct -j <jobid> -o JobID,State,Elapsed,MaxRSS
gcloud storage ls -r gs://${BRIEFLOW_BUCKET_INPUT}/<screen>/ | grep -c '\.nd2$'
tail -f tmp/transfer_<screen>_<jobid>_1.err
```

> **⚠️ Completion signal (C2) — count files, not bytes.** Large phenotype `.nd2`s
> copy first and push **byte**-progress (`gcloud storage du`) to ~100% while tens
> of thousands of small SBS tile files are still pending. Reading byte-% as "done"
> nearly triggered a premature VM provision on incomplete data. Use **destination
> file-count vs source file-count** and/or **`sacct State == COMPLETED`** as the
> completion signal — never byte totals.

> **⚠️ `/archive` is slow (C3).** When the only source is the archival tier
> (`/archive`, autofs), sustained aggregate throughput was ~170–256 MB/s across 4
> concurrent jobs — ~19 TB took ~13–16 h, ~2–3× the optimistic estimate. Prefer a
> non-archival source path when one exists; if pulling from `/archive`, the ETA is
> bandwidth-bound, so size the sbatch `--time` generously.

**Resumability + retry.** `gcloud storage rsync` skips files that already exist
with matching size/checksum. The generated sbatch wraps each rsync in a bounded
retry (treats a transient GCS 5xx / "Temporary components were not uploaded
correctly" as recoverable rather than failing the whole task under `set -e`).
Safe to re-submit the same sbatch if a task still exhausts its retries.

---

## S3.5 — Handoff (instant)

The wizard prints a plain-text instructions block for the wait window:

```
TRANSFER SUBMITTED
  screen:    <screen>
  sbatch job: <jobid>
  email:     <operator-email>

WHILE YOU WAIT (~6-36h):
  - VM is provisioned and STOPPED to save cost
  - Check progress: gcloud storage du -s gs://${BUCKET_INPUT}/<screen>/
```

## S3.6 — Seamless handoff prep (when transfer finishes)

Before printing the final SSH instructions, the wizard does the prep
operator-side so the VM session is one command away from running:

```bash
# 1. Tar the plugin
tar -czf tmp/brieflow-auto-plugin.tgz -C plugins/ brieflow-auto/

# 2. Start the VM
gcloud compute instances start <vm-name> --zone=<zone>

# 3. Resolve and verify the connection per connection_handoff.md.
#    Reuse its account, remote user, project, instance and actual zone below.

# 4. Push plugin tarball + first-boot script to VM via IAP
gcloud compute scp tmp/brieflow-auto-plugin.tgz tmp/vm_first_boot_<screen>.sh \
    <verified-user>@<vm-name>:~/ \
    --account=<account> --project=<project> --zone=<zone> --tunnel-through-iap
```

Final printed handoff:

```
LOGIN VERIFIED; analysis-directory readiness is reported separately.
  ssh:   use the exact command printed from connection.json
  then:  bash ~/vm_first_boot_<screen>.sh
  then:  enter the named remote tmux session from the handoff
  then:  claude (unless already running in that session)
  then:  /brieflow-setup    # continue from saved setup context
```

> **🛑 Hard stop — the orchestrator's job ends at S3.6 (A2).** The HPC/orchestrating
> host does S1–S3.6 only: provision the VM, transfer raw data → GCS, push the
> first-boot script + plugin tarball, print this handoff, and **exit**. It must NOT
> SSH in to run first-boot, clone the repo, build the env, stage screen.yaml, or
> launch the runner/notebook — doing so runs everything as the wrong Linux user
> (A1), skips the operator's own `gh` auth (A3), and bypasses the on-VM interview
> that must happen against the real images (E2). Everything from first-boot onward
> is the operator's, run as themselves on the VM.

**Optional state file.** If the operator wants the wizard to be re-entrant
across HPC + VM sessions, the HPC stage writes `setup_state.json` to
`gs://${BRIEFLOW_BUCKET_OUTPUT}/<screen>/setup_state.json`. The VM stage
reads it and resumes from `stage: "S3-complete"`. See
[setup_state_schema.md](setup_state_schema.md) for the schema.

For one-shot manual setups (driven by a human operator), the state file is
overkill — the text handoff above captures everything that needs to cross
HPC→VM. Skip it.

---

## S4 — SSH to VM

Follow [connection_handoff.md](connection_handoff.md) first. Use the saved, verified
identity and actual zone, not defaults from the originating laptop or HPC account.

```bash
gcloud compute ssh <verified-user>@<vm-name> \
    --account=<account> \
    --zone=<zone> \
    --tunnel-through-iap \
    --project=<project>
```

`--tunnel-through-iap` is required because the default firewall rules only
allow SSH from Google's IAP range (35.235.240.0/20), not from the operator's
laptop IP. You may need `roles/iap.tunnelResourceAccessor` on the project
(project owner has it implicitly).

If `instances start` fails with stockout, wait 5-15 min or pick a different
strategy (smaller machine, different region, future reservation).

---

## S5 — First-boot setup (~15 min)

The wizard already pushed `vm_first_boot_<screen>.sh` + `brieflow-auto-plugin.tgz`
in S3.6. **The operator SSHes in as themselves and runs the script** — this is the
handoff boundary (see the callout below). Do NOT run S5+ from the orchestrating
host.

> **⚠️ Identity (A1) — run VM setup as the operator, not the orchestrator.**
> Cloud account, client username, and remote Linux username are separate identities.
> OS Login supplies an assigned username when enabled; otherwise choose the intended
> Linux account explicitly. Verify it with `brieflow_connection.py` and set
> `$BRIEFLOW_VM_USER` to the verified value. Use that saved identity from every client.
> Never derive it from an email local-part, create a second workspace to hide a mismatch,
> or change ownership automatically. See [connection_handoff.md](connection_handoff.md).

What it does on the VM:

1. **Identify + format the data disk.** `lsblk` finds the multi-TB attached
   disk; `mkfs.ext4` if blank; mount at `/data`; persist via `/etc/fstab`.
2. **Install tooling:** **miniforge** (NOT miniconda — see below), git, gh,
   tmux, rsync.

   **miniforge, not miniconda (B1/B2).** Miniconda ships conda 26.x, which (a)
   refuses `conda create` until Anaconda default-channel ToS are accepted *even
   when only `-c conda-forge` is requested*, and (b) bundles `anaconda-anon-usage`,
   whose sqlite db contends under scripted installs and surfaces as an opaque
   `sqlite3 OperationalError: database is locked`. Miniforge defaults to
   conda-forge, carries neither the ToS gate nor the anon-usage plugin, and is the
   canonical installer for this flow:
   ```bash
   curl -fsSL -o ~/miniforge.sh \
     https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh
   bash ~/miniforge.sh -b -p "$HOME/miniforge3"
   eval "$("$HOME/miniforge3/bin/conda" shell.bash hook)"
   conda init bash
   ```
   *(Fallback only if an operator is stuck on miniconda: `conda tos accept
   --override-channels --channel https://repo.anaconda.com/pkgs/main` (and `.../pkgs/r`),
   then create with `CONDA_NO_PLUGINS=true conda create --solver=classic ...`.)*

   **`gh` (A3).** The private analysis repo needs `gh` to clone + push config back.
   apt's default repo lacks a usable version — install from the official repo:
   ```bash
   curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
     | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
   echo "deb [signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] \
     https://cli.github.com/packages stable main" \
     | sudo tee /etc/apt/sources.list.d/github-cli.list
   sudo apt-get update && sudo apt-get install -y gh
   ```
   The operator then runs `gh auth login` **as themselves** (their own credentials).

   **Node.js 20 via NodeSource** (apt's `nodejs` on ubuntu-2204-lts is v12 —
   too old for `claude-code` which requires Node ≥18). Sequence:

   ```bash
   # Remove conflicting old dev packages first (they own /usr/include/node/*)
   sudo apt-get remove -y libnode-dev libnode72 nodejs-doc

   # NodeSource repo + install
   curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
   sudo apt-get install -y nodejs

   # Then claude-code
   sudo npm install -g @anthropic-ai/claude-code
   ```

   If `apt-get install nodejs` still trips on dpkg conflicts, fall back to
   `sudo apt-get install -y -o Dpkg::Options::="--force-overwrite" nodejs`.
3. **Install the brieflow-auto plugin.** Pick the analysis root first and
   export it — everything from here on (S6, teardown, the launch recipe) reads
   `$BRIEFLOW_DATA_ROOT`, so `export BRIEFLOW_DATA_ROOT=<a directory on the
   data disk>` now and persist it at step 4. Then clone from GitHub into it and
   install via `--dir` (not `--git`, which puts the clone inside
   `~/.claude/plugins/` and is awkward for branch work):

   ```bash
   mkdir -p $BRIEFLOW_DATA_ROOT
   git clone https://github.com/cheeseman-lab/brieflow-auto \
       $BRIEFLOW_DATA_ROOT/brieflow-auto
   bash ~/install_plugin.sh --dir $BRIEFLOW_DATA_ROOT/brieflow-auto
   ```

   **Edit → test loop:** after changing plugin files in `$BRIEFLOW_DATA_ROOT/brieflow-auto/`,
   re-run `bash ~/install_plugin.sh --dir $BRIEFLOW_DATA_ROOT/brieflow-auto`
   and then `/reload-plugins` in Claude to pick up the changes.

   **Branching:** create a per-screen branch immediately after cloning:
   ```bash
   git -C $BRIEFLOW_DATA_ROOT/brieflow-auto checkout -b <screen>
   ```
   Commit fixes found during bring-up, push, and open a PR to main.
4. **Persist env vars.** Write `BRIEFLOW_GCP_PROJECT`, `BRIEFLOW_GCP_ZONE`,
   `BRIEFLOW_BUCKET_{INPUT,OUTPUT}`, `BRIEFLOW_VM_USER` and
   `BRIEFLOW_DATA_ROOT` (see `cloud/README.md`, Environment) to
   `~/.brieflow_env`, and source it from `~/.bashrc` so future `claude`
   sessions inherit them.
5. **Pull data:** `gcloud storage rsync gs://${BRIEFLOW_BUCKET_INPUT}/<screen>/ /data/<screen>/`.

**Why not bake an image?** The baked image (`build_vm_image.sh`) saves ~10 min
per provision and pre-installs the same tooling. For one screen at a time, the
savings don't justify image-family lifecycle management. Default flow installs
from base `ubuntu-2204-lts` and is reproducible from the gcloud CLI commands.

---

## S6 — Repo setup (~10-15 min)

```bash
# On the VM — clone into the analysis root
git clone <repo URL> $BRIEFLOW_DATA_ROOT/<screen>-analysis
cd $BRIEFLOW_DATA_ROOT/<screen>-analysis
git submodule update --init --recursive

# Per-screen env (NOT reusing any baked env)
# With miniforge (S5) this just works — no ToS accept, no anon-usage lock.
conda create -n brieflow_<screen> -c conda-forge python=3.12 uv pip -y
conda activate brieflow_<screen>

cd brieflow
uv pip install -r pyproject.toml
uv pip install -e .
```

Segmentation-model deps are **not** installed at env-build time. They go in at
the phenotype step, gated on the interview's `CELLPOSE_MODEL` / `SEGMENTATION_METHOD`
(brieflow-notebook Step 3b), so the base env stays lean and screens that don't use
them never pull the heavy torch pin:

```bash
# cpsam (cellpose-SAM — e.g. RPE1's irregular cells):
uv pip install cellpose==4.0.4 torch==2.7.0 torchvision==0.22.0
# stardist:
uv pip install "numpy<2.0" stardist==0.9.1 csbdeep==0.8.1
```

---

## S7 — Stage screen.yaml (~5 min)

The operator writes `screen.yaml`; setup stages it. Find the operator's filled-in copy (the path
they gave, the repository root, or the working directory) and copy it over the template:

```bash
python scripts/brieflow_screen_yaml.py find --analysis-dir analysis [--candidate <path>]
python scripts/brieflow_screen_yaml.py stage --analysis-dir analysis --source <path>
```

If `find` returns nothing, stop. `brieflow_screen_yaml.py check --analysis-dir analysis` lists
the blank fields; the operator fills them in `analysis/screen.yaml` and re-invokes
`/brieflow-setup`. Do not ask for them one by one and do not fill them in.

Setup then sets only the host facts it created, after the operator confirms:

```yaml
pipeline:
  conda_env: brieflow_<screen>

data:
  input_sbs_dir: /data/<screen>/input_sbs        # absolute VM-local path
  input_phenotype_dir: /data/<screen>/input_ph
```

Provenance (source paths, rename, transfer job id, dates) goes to `analysis/analysis.md`, not
into `screen.yaml`.

> **⚠️ `input_sbs_dir` / `input_phenotype_dir` are required, NOT derived (E1).**
> These are operator-locked in screen.yaml — the preprocess notebook does *not*
> derive them. Left null, they surface only later as runner
> `SBS_IMAGES_DIR_FP` / `PHENOTYPE_IMAGES_DIR_FP` gaps. Fill them at S7 with the
> VM-local raw-data paths. For multi-plate screens point at the screen root that
> contains the canonical `plate_*/input_sbs` + `plate_*/input_ph` tree (e.g.
> `/data/<screen>`), which `brieflow_validate_tree.py` has already normalized at
> S8. The preflight gate (`validate_screen_yaml_inputs`) fails early if either is
> blank, alongside the barcode/design-table check.

Other fields (library, iss, phenotype channels) — collected via the template's
inline-comment interview using `/brieflow-notebook`.

> **On-VM interview (E2).** Genuine analysis-design decisions — `SEGMENTATION_METHOD`
> (sbs + phenotype), `ALIGN`, `CHANNEL_COMBOS`, `MIN_CELL_CUTOFFS` — are resolved
> in the interview **on the VM, against the real images**. That's *why* the handoff
> model puts the interview on the VM; the orchestrator must never pre-answer or
> guess them.

---

## S8 — Sanity check + handoff (~2 min)

Verify:

- `screen.yaml` parses (`python -c "import yaml; yaml.safe_load(open('analysis/screen.yaml'))"`)
- barcode/design table (`raw_design_table_fp`) in `analysis/config/` — it
  co-travels with `screen.yaml` from the upload location (see S1/upload)
- `conda activate brieflow_<screen>` works
- the pulled `/data/<screen>/` tree is in the layout brieflow reads
  (`scripts/brieflow_validate_tree.py --layout` prints it:
  `input_sbs/plate_<N>/c<M>/` and `input_ph/plate_<N>/round_<R>/`). The agent stages
  it as in "Raw data staging" in `SKILL.md`: dry-run → confirm → `--apply`, blockers
  resolved with the operator, until `--inputs-from analysis/screen.yaml` exits 0. The
  runner checks the same thing after its own GCS pull and stops with exit 9.
- `screen.yaml:experiment.screen_title` matches the slug

Exit to the brieflow-auto router → dispatches to `brieflow-notebook` for
preprocess phase.

---

## Launching on the VM

Setup ends at S8; the run itself is `bin/brieflow-run`, the same entry point as
on any other host (see "Where the agent runs" in `SKILL.md`). On a VM the only
differences are `--backend local` and `--lifecycle-hooks gcs`.

Launch it as a **transient systemd unit**, not tmux — a tmux server started
from an SSH session dies with the session and silently orphans the run.

```bash
gcloud compute ssh <verified-user>@<vm-name> --account=<account> --project=<project> --zone=<zone> --tunnel-through-iap

# Then on the VM, as $BRIEFLOW_VM_USER. RUN_DIR is the screen repo root; the
# analysis dir the runner wants is analysis/ beneath it.
RUN_DIR=$BRIEFLOW_DATA_ROOT/<screen>-analysis
PLUGIN_BIN=$BRIEFLOW_DATA_ROOT/brieflow-auto/plugins/brieflow-auto/bin
SCREEN_PY=$HOME/miniforge3/envs/brieflow_<screen>/bin/python
RUN_CMD="$PLUGIN_BIN/brieflow-run --analysis-dir $RUN_DIR/analysis --backend local --lifecycle-hooks gcs --screen-id <screen>"

sudo systemd-run --unit=brieflow-<screen>-$(date +%H%M) \
    --uid=$BRIEFLOW_VM_USER --gid=$BRIEFLOW_VM_USER \
    --working-directory="$RUN_DIR" -p KillMode=control-group \
    --setenv=HOME=/home/$BRIEFLOW_VM_USER --setenv=USER=$BRIEFLOW_VM_USER \
    /bin/bash -lc "set -a; . \$HOME/.brieflow_env; set +a; \
        PYTHON=$SCREEN_PY $RUN_CMD >> \$HOME/<screen>_run.log 2>&1"
```

**Source `~/.brieflow_env` and name the interpreter inside the unit**, as above:
a systemd unit inherits neither, `~/.bashrc` bails out early in a non-interactive
shell so `-lc` alone does not pick the file up, and with
`--lifecycle-hooks gcs` the GCS hooks are constructed — and raise on unset
`BRIEFLOW_BUCKET_INPUT` / `BRIEFLOW_BUCKET_OUTPUT` — *before* the launch gate
ever writes a review.

The unit runs as the user and group that own the analysis tree; on a stock VM
that group is the operator's own. Use a **fresh unit name each time** — a
stopped transient unit still "exists". Add `--through <phase>` to stop after one phase;
the next launch picks up from the phases already done.

**The first launch stops at the gate.** It writes
`.brieflow/launch_review.html` and exits 8 before spending any compute. Read
the review, then re-run the exact command from `.brieflow/launch_review.json`
→ `how_to_proceed.command` — it already carries `--approved`, the resolved
`--analysis-dir`, and a `PYTHON=` prefix when the screen env is not on `PATH`.
It opens with a `bin/brieflow-run` that is relative **to the plugin root**, so
from `$RUN_DIR` prefix it with `$PLUGIN_BIN/..` or run it from there. Watch the
run with `systemctl status brieflow-<screen>-*`, `journalctl -u <unit> -f`, and
`~/<screen>_run.log`.

---

## Re-entrance and recovery

- **Re-invoke the skill any time.** Each invocation re-detects host (HPC vs VM)
  and re-reads optional `setup_state.json` if present.
- **Start over.** Drop the optional state file
  (`gcloud storage rm gs://${BRIEFLOW_BUCKET_OUTPUT}/<screen>/setup_state.json`),
  tear down the VM (`cloud/teardown_vm.sh <screen>`), delete the data disk
  (`gcloud compute disks delete`). Re-run the wizard.
- **End-of-screen teardown.** See `references/teardown_flow.md` for the full
  procedure (parallel per-subtree rsync to preserve outputs, gcloud regex
  pitfalls, verification, boot-disk snapshot for cheap respin). The bundled
  `cloud/teardown_vm.sh` works for the legacy `/data/<screen>/` layout but
  not the screen-repo layout used by recent screens — prefer the manual
  procedure in `teardown_flow.md` for current screens.
- **Transfer fails mid-flight.** Re-submit the same sbatch; rsync resumes from
  where it stopped.

## Common gotchas (from real-world bring-up)

| Symptom | Cause | Fix |
|---|---|---|
| `network 'default' not found` at S2 | Fresh project, no VPC | See `gcp_bootstrap.md` Step 2 |
| `Connection refused` at S4 | No IAP firewall rule | See `gcp_bootstrap.md` Step 3 |
| `Cannot modify Cloud SDK installation directory` at S3 | `gcloud config set --installation` on compute node | Use env vars |
| `reason: stockout` at S2 | Zone has no capacity | provision_vm.sh tries fallback zones; if all fail, request reservation |
| VM idle cost while transfer runs | $25-30/hr compute + $4.7/hr disk | `instances stop` during S3; `start` after S3 |
| `bucket name is not available` | Globally taken | Pick another name; bucket names are GCS-global |
| `EBADENGINE Unsupported engine ... required: { node: '>=18.0.0' }` at S5 | Default apt `nodejs` on ubuntu-2204-lts is v12 | Install Node 20 via NodeSource before `npm install -g @anthropic-ai/claude-code` |
| VS Code Remote SSH hangs / `Operation timed out` | `gcloud compute config-ssh` writes direct IP + no `ProxyCommand`; port 22 is IAP-only | Update `~/.ssh/config` entry: set `HostName` to the instance name (not IP) and add `ProxyCommand /path/to/gcloud compute start-iap-tunnel %h %p --listen-on-stdin --project=<project> --zone=<zone>` |

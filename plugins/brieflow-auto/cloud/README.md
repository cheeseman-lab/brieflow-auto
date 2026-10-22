# brieflow-auto cloud/ — GCP scripts for per-screen runs

Operator shell scripts that handle the GCP-side of per-screen brieflow runs.
Scripts, not Terraform — the goal is "one command per screen", not full IaC
state management.

The plugin is **lab-agnostic**: no defaults for bucket names, lab prefixes,
or other site-specific choices. Operators set env vars per their project.

## Environment

Every script sources `_common.sh`, which declares these; the scripts that need
one check it with `require_env`, so a missing value is an error naming the
variable — never a silent fallback to somebody else's project. The last two are
read by the setup skill's recipes rather than by `cloud/*.sh`.

| Variable | Meaning |
|---|---|
| `BRIEFLOW_GCP_PROJECT` | GCP project that owns the VMs and buckets |
| `BRIEFLOW_BUCKET_INPUT` | bucket holding raw image data per screen |
| `BRIEFLOW_BUCKET_OUTPUT` | bucket receiving `brieflow_output/` and logs |
| `BRIEFLOW_VM_USER` | login user on the VM |
| `BRIEFLOW_DATA_ROOT` | directory on the host that holds `<screen>-analysis/` (any path) |

Optional, each with a working default in `_common.sh`: `BRIEFLOW_GCP_REGION`,
`BRIEFLOW_GCP_ZONE`, `BRIEFLOW_BUDGET_USD`, `BRIEFLOW_VM_MACHINE_TYPE`,
`BRIEFLOW_VM_BOOT_DISK_SIZE_GB`, `BRIEFLOW_VM_BOOT_DISK_TYPE`,
`BRIEFLOW_VM_IMAGE_FAMILY`, `BRIEFLOW_VM_IMAGE_PROJECT`,
`BRIEFLOW_VM_SERVICE_ACCOUNT`.

Optional with no default — empty until you set them:

| Variable | Read by | Unset means |
|---|---|---|
| `BRIEFLOW_GCP_BILLING_ACCOUNT` | `setup_gcp_project.sh` | the billing-budget step is skipped |
| `BRIEFLOW_GCP_ZONE_FALLBACK` | `provision_vm.sh` | fall back to the region's a/b/c/f zones (comma-separated list to override) |
| `BRIEFLOW_TRANSFER_PARTITION` | `transfer_hpc_to_gcs.sh` | `--partition` is required on the command line |
| `BRIEFLOW_TRANSFER_GBPS` | `transfer_hpc_to_gcs.sh` | the transfer-time estimate assumes 1 Gbps |

Set them in the shell or in `~/.brieflow_env` sourced from `~/.bashrc`:

```bash
# example
export BRIEFLOW_GCP_PROJECT=my-ops-project
export BRIEFLOW_BUCKET_INPUT=my-ops-input
export BRIEFLOW_BUCKET_OUTPUT=my-ops-output
export BRIEFLOW_VM_USER=ops
export BRIEFLOW_DATA_ROOT=/data/analysis
```

## One-time setup (per GCP project)

See [`skills/brieflow-setup/references/gcp_bootstrap.md`](../skills/brieflow-setup/references/gcp_bootstrap.md)
for the full walkthrough. TL;DR:

```bash
# 1. Auth (once per machine)
gcloud auth login
gcloud config set project <YOUR_PROJECT>

# 2. Pick names + set env vars (bucket names are GCS-global; must be unique)
export BRIEFLOW_GCP_PROJECT=<YOUR_PROJECT>
export BRIEFLOW_BUCKET_INPUT=<your-input-bucket>
export BRIEFLOW_BUCKET_OUTPUT=<your-brieflow-bucket>
export BRIEFLOW_GCP_BILLING_ACCOUNT=<billing-id>   # optional, for budget alert

# 3. Bootstrap: APIs, default network, firewall rules, 2 buckets, service account
bash setup_gcp_project.sh

# 4. Optionally bake a VM image (~15 min). Otherwise provision_vm.sh uses
#    base ubuntu-2204-lts and installs tooling on first SSH.
bash build_vm_image.sh
```

## Per-screen workflow

```bash
export BRIEFLOW_GCP_PROJECT=<YOUR_PROJECT>
export BRIEFLOW_BUCKET_INPUT=<your-input-bucket>
export BRIEFLOW_BUCKET_OUTPUT=<your-brieflow-bucket>

# Provision (tries zone fallback if first zone stockout)
bash provision_vm.sh <screen-id> --data-disk-tb N [--no-image]

# Kick off HPC → GCS transfer (slurm sbatch, generates job array)
bash transfer_hpc_to_gcs.sh <screen-id> \
    --sbs-dir /path/to/<source>/sbs/ \
    --phenotype-dir /path/to/<source>/phenotype/ \
    --email <op-email> --partition <slurm-partition>

# Stop VM during transfer to save ~$25-30/hr; start again post-transfer.
gcloud compute instances stop <vm-name> --zone=<zone>
gcloud compute instances start <vm-name> --zone=<zone>

# SSH to VM (IAP-tunneled — no public SSH)
gcloud compute ssh <verified-user>@<vm-name> --account=<account> --project=<project> --zone=<zone> --tunnel-through-iap

# After analysis: teardown
bash teardown_vm.sh <screen-id>

# Operator visibility
bash dashboard.sh   # list running brieflow VMs + per-screen cost
```

## Layout

| Script | What it does |
|---|---|
| `_common.sh` | shared env vars + helper funcs sourced by every other script |
| `setup_gcp_project.sh` | one-time: enable APIs, create default network + firewall rules, create 2 GCS buckets, create service account, bind IAM, optional billing budget |
| `build_vm_image.sh` | (optional) bake a VM image with miniconda base + brieflow-auto plugin + claude + gh + tmux |
| `provision_vm.sh <screen-id>` | `gcloud compute instances create` with zone fallback (a → b → c → f); attaches a separate pd-ssd data disk |
| `transfer_hpc_to_gcs.sh <screen-id>` | **HPC source**: generates + submits a slurm sbatch array (one task per source dir); uses env vars for gcloud rsync parallelism |
| `transfer_local_to_gcs.sh <screen-id>` | **Local-machine source** (laptop / workstation): parallel `gcloud storage rsync` in a detached tmux session with sleep-prevention (caffeinate / systemd-inhibit) |
| `transfer_gcs_to_vm.sh <screen-id>` | runs on the VM; `gcloud storage rsync` from GCS to /data/ in a detached tmux session |
| `bin/brieflow-run --backend local --lifecycle-hooks gcs --screen-id <id> [--through <phase>]` | run on the VM under a transient systemd unit (recipe in the setup skill) |
| `teardown_vm.sh <screen-id>` | pushes outputs to GCS and deletes the VM + attached data disk |
| `dashboard.sh` | `gcloud compute instances list` filtered by `brieflow-screen=*` label + per-VM cost via Cloud Billing API |

## Bucket layout (2 buckets)

```
gs://${BRIEFLOW_BUCKET_INPUT}/<screen-id>/      — raw nd2s + barcode library
gs://${BRIEFLOW_BUCKET_OUTPUT}/<screen-id>/     — analysis outputs + reviews + telemetry + optional setup_state.json
```

Operator sets both env vars. No defaults — GCS bucket names are globally
unique, so the operator must pick names that aren't taken.

## VM naming + labeling

- Name: `brieflow-<screen-id>-<YYYYMMDD>`
- Labels: `brieflow-screen=<screen-id>`, `brieflow-managed=true` (for dashboard filtering + cost attribution)
- Boot: 200 GB pd-ssd, default `ubuntu-2204-lts` (override via baked image)
- Data disk: separate pd-ssd in same zone as VM (zone-locked); size via `--data-disk-tb`

## SSH

Start with the [verified connection handoff](../skills/brieflow-setup/references/connection_handoff.md).
Save the actual project, zone, instance, cloud account and remote Linux user with
`scripts/brieflow_connection.py`; verify identity and workspace access before declaring
setup complete. Reuse the printed command from every client. Never infer the Linux user
from the client's username or an email local-part. Claude Code and analysis execution
remain on the VM; SSH is the operator's access route, not a pipeline dispatcher.

The default network's firewall rules allow SSH **only via IAP** (no public
0.0.0.0/0:22 rule). Always SSH with:

```bash
gcloud compute ssh <verified-user>@<vm> --account=<account> --project=<project> --tunnel-through-iap --zone=<zone>
```

You may need `roles/iap.tunnelResourceAccessor` on the project (project owner
gets it implicitly).

## Cost controls

- No auto-stop ceiling: `provision_vm.sh` deliberately sets no
  max-run-duration, because a screen runs for days and a wall-clock STOP would
  kill a live run mid-phase. Stopping the VM when the screen is done is the
  cost control, not a kill switch.
- `teardown_vm.sh` is the explicit shutdown when done; the data disk persists
  separately until deleted.
- Stop the VM during the multi-hour HPC→GCS transfer to save compute charges:
  ~$30/hr c3d-highmem-360 + $4.7/hr 20 TB pd-ssd vs $4.7/hr disk-only when STOPPED.
- `dashboard.sh` flags VMs running past expected wall time.

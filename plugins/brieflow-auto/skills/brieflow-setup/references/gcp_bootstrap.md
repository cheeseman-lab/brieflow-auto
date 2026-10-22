# GCP one-time bootstrap

Steps required **once per GCP project** before the first brieflow cloud screen.
Captures every gap we hit on a fresh project. Idempotent — safe to re-run.

> The plugin's `cloud/setup_gcp_project.sh` automates most of this, but operators
> who want explicit control (or hit a step the script doesn't cover) can run
> the gcloud commands below directly.

## Inputs the operator must decide

| Field | Example | Notes |
|---|---|---|
| GCP project ID | `<gcp-project>` | Existing project; billing must be linked |
| Billing account | `<billing-account-id>` | Linked to the project (verify before starting) |
| Region | `us-central1` | Co-locate buckets + VMs to avoid egress |
| Bucket names | `<input-bucket>`, `<output-bucket>` | Globally unique on GCS; pick now and stick with it |

The plugin **does not** ship default bucket names. Operator must set
`BRIEFLOW_BUCKET_INPUT` and `BRIEFLOW_BUCKET_OUTPUT` env vars (or pass values
directly to the bootstrap script).

## Step 0 — auth

```bash
gcloud auth login                                # browser-based; once per machine
gcloud config set project <YOUR_PROJECT>
```

**Application Default Credentials (ADC)** is **only** required if you'll run
Python client libraries (`google-cloud-storage`, etc.) locally against the
project. For the bootstrap + per-screen workflow (all `gcloud` CLI), user
creds from `gcloud auth login` are sufficient. Don't block on ADC.

If you do need ADC later, run on the HPC head node:

```bash
gcloud auth application-default login --no-launch-browser
# prints a URL → open in any browser → paste back the verification code
```

## Step 1 — enable APIs

```bash
gcloud services enable \
    compute.googleapis.com \
    storage.googleapis.com \
    iam.googleapis.com
```

Compute API enablement can take ~30s. Storage + IAM are instant.

## Step 2 — create the default VPC network

**Fresh GCP projects do not have a `default` network.** `gcloud compute
instances create` will fail with *"The referenced network resource cannot
be found"* unless you create one explicitly.

```bash
gcloud compute networks create default \
    --subnet-mode=auto \
    --bgp-routing-mode=regional
```

The auto-mode `default` network provisions subnets in every GCP region.
`bgp-routing-mode=regional` is the safer default (limits cross-region traffic).

## Step 3 — firewall rules

The default network ships **with no firewall rules** as of mid-2024. You must
add them:

```bash
# Internal traffic between instances on the network
gcloud compute firewall-rules create default-allow-internal \
    --network=default --direction=INGRESS --action=ALLOW \
    --rules=tcp,udp,icmp \
    --source-ranges=10.128.0.0/9

# SSH via Identity-Aware Proxy (recommended; tunnels through Google)
gcloud compute firewall-rules create default-allow-ssh-iap \
    --network=default --direction=INGRESS --action=ALLOW \
    --rules=tcp:22 \
    --source-ranges=35.235.240.0/20
```

Do **not** add `0.0.0.0/0` SSH ingress unless org policy forces it — IAP-tunneled
SSH is the standard for production VMs:

```bash
gcloud compute ssh <verified-user>@<vm> --account=<account> --project=<project> --tunnel-through-iap --zone=<zone>
```

If you've never used IAP on this project, you may need to grant yourself
`roles/iap.tunnelResourceAccessor` on the project (project owner has it
implicitly).

### VS Code Remote SSH via IAP

Resolve and verify the remote user through [connection_handoff.md](connection_handoff.md)
first. The cloud account, Linux username and instance ID below come from that record,
not from the local client's username. Keep each client's own SSH keys and gcloud install.

`gcloud compute config-ssh` writes a direct-IP `~/.ssh/config` entry with no
`ProxyCommand`. With IAP-only firewall rules, VS Code will hang or time out.
Fix the generated entry:

```
Host <vm-name>.<zone>.<project>
    HostName <vm-name>          # ← instance name, NOT the IP
    User <vm-user>
    IdentityFile ~/.ssh/google_compute_engine
    UserKnownHostsFile=~/.ssh/google_compute_known_hosts
    HostKeyAlias=compute.<id>
    IdentitiesOnly=yes
    CheckHostIP=no
    ProxyCommand /path/to/gcloud compute start-iap-tunnel %h %p \
        --listen-on-stdin --account=<account> --project=<project> --zone=<zone>
```

Find your `gcloud` path with `which gcloud`. After editing, VS Code Remote SSH
("Remote-SSH: Connect to Host...") will route through IAP automatically.

## Step 4 — create the 2 GCS buckets

Two buckets per project. Different concerns, different lifecycles:

| Bucket | Purpose |
|---|---|
| `${BRIEFLOW_BUCKET_INPUT}` | Raw nd2s + barcode libraries per screen: `gs://<input>/<screen>/{sbs,phenotype}/...` |
| `${BRIEFLOW_BUCKET_OUTPUT}` | Analysis outputs, optional setup-state files, telemetry: `gs://<brieflow>/<screen>/...` |

```bash
# Pick names + check global availability
export BRIEFLOW_BUCKET_INPUT=<input-bucket>
export BRIEFLOW_BUCKET_OUTPUT=<output-bucket>

gcloud storage buckets create gs://${BRIEFLOW_BUCKET_INPUT} \
    --location=us-central1 \
    --uniform-bucket-level-access \
    --public-access-prevention

gcloud storage buckets create gs://${BRIEFLOW_BUCKET_OUTPUT} \
    --location=us-central1 \
    --uniform-bucket-level-access \
    --public-access-prevention
```

**Bucket names are globally unique on GCS** — if `create` returns *"requested
bucket name is not available"*, pick another name. Underscores are valid in
GCS bucket names but consider sticking with dashes for cleaner URLs.

## Step 5 — VM service account + IAM

VMs auth as a project service account (not the operator). One SA shared across
all per-screen VMs.

```bash
gcloud iam service-accounts create brieflow-vm-sa \
    --display-name="brieflow VM service account" \
    --description="Used by per-screen brieflow VMs to read/write GCS"

SA=brieflow-vm-sa@$(gcloud config get-value project).iam.gserviceaccount.com

# Grant storage.objectAdmin on both buckets
for B in "$BRIEFLOW_BUCKET_INPUT" "$BRIEFLOW_BUCKET_OUTPUT"; do
    gcloud storage buckets add-iam-policy-binding gs://${B} \
        --member="serviceAccount:${SA}" \
        --role="roles/storage.objectAdmin"
done
```

## Step 6 (optional) — billing budget alert

Informational only — doesn't auto-shutdown VMs. Requires
`roles/billing.user` on the billing account.

```bash
gcloud billing budgets create \
    --billing-account=$BRIEFLOW_GCP_BILLING_ACCOUNT \
    --display-name=brieflow-budget \
    --budget-amount=500USD \
    --threshold-rule=percent=0.5 \
    --threshold-rule=percent=0.9 \
    --threshold-rule=percent=1.0 \
    --filter-projects=projects/$(gcloud config get-value project)
```

If it returns 403, skip — bucket + VM provisioning don't depend on it.

## Verification

```bash
# Everything created?
gcloud storage buckets list --format="value(name)" | grep -E "$BRIEFLOW_BUCKET_INPUT|$BRIEFLOW_BUCKET_OUTPUT"
gcloud compute networks list
gcloud compute firewall-rules list --format="table(name,allowed[].ports)"
gcloud iam service-accounts list --filter="email:brieflow-vm-sa@*"
```

## What this DOES NOT do

- **Create a VM image.** Per-screen flow can provision from base
  `ubuntu-2204-lts` and install tooling on first boot — see `cloud_flow.md`.
  Building an image (`cloud/build_vm_image.sh`) is optional; the only thing
  it bakes is miniconda + git + gh + Claude Code + the plugin clone.
- **Provision per-screen resources.** Those happen at S2 of the per-screen flow:
  `cloud/provision_vm.sh` + ad-hoc transfer sbatch.
- **Reserve compute capacity.** Stockouts on large machine types
  (c3d-highmem-360, m2-megamem-416) are real; provision_vm.sh has zone
  fallback logic but if all us-central1 zones are stockout, request a
  future-reservation via the console or wait.

## Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `bucket name is not available` | Globally taken | Pick another name |
| `network 'default' not found` | Step 2 skipped on fresh project | Run Step 2 |
| `ssh: connect to host 35.x.x.x: Connection refused` | No IAP firewall rule | Run Step 3 |
| `Permission denied on billing account` | No `roles/billing.user` | Skip Step 6 or get role |
| `stockout` on instance create | Zone has no capacity for that machine type | Try another zone in same region |
| `cannot perform action on Google Cloud SDK installation directory` from sbatch | `gcloud config set --installation` requires sudo (not available on compute nodes) | Use env vars: `export CLOUDSDK_STORAGE_PROCESS_COUNT=N CLOUDSDK_STORAGE_THREAD_COUNT=M` |

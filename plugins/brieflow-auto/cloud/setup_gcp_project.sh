#!/usr/bin/env bash
# setup_gcp_project.sh — one-time GCP bootstrap for brieflow cloud runs.
#
# Idempotent: re-running on an already-set-up project skips existing resources.
#
# What it does:
#   1. Enable required GCP APIs
#   2. Create the default VPC network (auto-mode) if missing
#   3. Add the 2 minimum firewall rules (internal + SSH via IAP)
#   4. Create the 2 GCS buckets (INPUT + BRIEFLOW)
#   5. Create the VM service account with GCS read/write IAM on both
#   6. (Optional) Configure a billing budget alert at $BRIEFLOW_BUDGET_USD
#
# Required env:
#   BRIEFLOW_GCP_PROJECT
#   BRIEFLOW_BUCKET_INPUT      (operator-chosen, GCS-global unique)
#   BRIEFLOW_BUCKET_OUTPUT   (operator-chosen, GCS-global unique)
# Optional env:
#   BRIEFLOW_GCP_BILLING_ACCOUNT (required for budget step; skips if absent)
#   BRIEFLOW_GCP_REGION (default us-central1)
#   BRIEFLOW_BUDGET_USD (default 500)
#
# See skills/brieflow-setup/references/gcp_bootstrap.md for the full playbook.

source "$(dirname "$0")/_common.sh"
require_gcloud
require_project
require_env BRIEFLOW_BUCKET_INPUT
require_env BRIEFLOW_BUCKET_OUTPUT

log "project=${BRIEFLOW_GCP_PROJECT} region=${BRIEFLOW_GCP_REGION}"
log "buckets: input=${BRIEFLOW_BUCKET_INPUT}, brieflow=${BRIEFLOW_BUCKET_OUTPUT}"
gcloud config set project "$BRIEFLOW_GCP_PROJECT" >/dev/null

# ---------------------------------------------------------------------------
# 1. Enable APIs
# ---------------------------------------------------------------------------

log "enabling required APIs (idempotent)"
gcloud services enable \
    compute.googleapis.com \
    storage.googleapis.com \
    iam.googleapis.com

if [[ -n "${BRIEFLOW_GCP_BILLING_ACCOUNT:-}" ]]; then
    gcloud services enable cloudbilling.googleapis.com billingbudgets.googleapis.com 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# 2. Default VPC network (fresh projects do NOT have one)
# ---------------------------------------------------------------------------

if gcloud compute networks describe default >/dev/null 2>&1; then
    log "default VPC network already exists — skip"
else
    log "creating default VPC network (auto-mode, regional BGP)"
    gcloud compute networks create default \
        --subnet-mode=auto \
        --bgp-routing-mode=regional >/dev/null
fi

# ---------------------------------------------------------------------------
# 3. Firewall rules (default network ships with NO rules)
# ---------------------------------------------------------------------------

create_fw_rule_if_missing() {
    local name="$1"; shift
    if gcloud compute firewall-rules describe "$name" >/dev/null 2>&1; then
        log "  firewall rule ${name} already exists — skip"
    else
        log "  creating firewall rule ${name}"
        gcloud compute firewall-rules create "$name" "$@" >/dev/null
    fi
}

log "ensuring minimum firewall rules"
create_fw_rule_if_missing default-allow-internal \
    --network=default --direction=INGRESS --action=ALLOW \
    --rules=tcp,udp,icmp --source-ranges=10.128.0.0/9

create_fw_rule_if_missing default-allow-ssh-iap \
    --network=default --direction=INGRESS --action=ALLOW \
    --rules=tcp:22 --source-ranges=35.235.240.0/20

# NOTE: We DO NOT add a 0.0.0.0/0:22 rule. SSH via IAP only:
#   gcloud compute ssh <vm> --tunnel-through-iap --zone=<zone>

# ---------------------------------------------------------------------------
# 4. GCS buckets (2 physical buckets)
# ---------------------------------------------------------------------------

create_bucket_if_missing() {
    local name="$1"
    if gcloud storage buckets describe "gs://${name}" >/dev/null 2>&1; then
        log "  bucket gs://${name} already exists — skip"
    else
        log "  creating gs://${name}"
        gcloud storage buckets create "gs://${name}" \
            --location="$BRIEFLOW_GCP_REGION" \
            --uniform-bucket-level-access \
            --public-access-prevention
    fi
}

log "creating GCS buckets"
create_bucket_if_missing "$BRIEFLOW_BUCKET_INPUT"
create_bucket_if_missing "$BRIEFLOW_BUCKET_OUTPUT"

# ---------------------------------------------------------------------------
# 5. Service account + IAM
# ---------------------------------------------------------------------------

SA_EMAIL="brieflow-vm-sa@${BRIEFLOW_GCP_PROJECT}.iam.gserviceaccount.com"

if gcloud iam service-accounts describe "$SA_EMAIL" >/dev/null 2>&1; then
    log "service account ${SA_EMAIL} already exists — skip"
else
    log "creating service account ${SA_EMAIL}"
    gcloud iam service-accounts create brieflow-vm-sa \
        --display-name="brieflow VM service account" \
        --description="Used by per-screen brieflow VMs to read/write GCS"
fi

log "binding storage.objectAdmin on the 2 buckets (idempotent)"
for bucket in "$BRIEFLOW_BUCKET_INPUT" "$BRIEFLOW_BUCKET_OUTPUT"; do
    gcloud storage buckets add-iam-policy-binding "gs://${bucket}" \
        --member="serviceAccount:${SA_EMAIL}" \
        --role="roles/storage.objectAdmin" >/dev/null
done

# ---------------------------------------------------------------------------
# 6. Billing budget (optional — skips cleanly if no billing role)
# ---------------------------------------------------------------------------

if [[ -n "${BRIEFLOW_GCP_BILLING_ACCOUNT:-}" ]]; then
    BUDGET_NAME="brieflow-budget"
    log "configuring billing budget '${BUDGET_NAME}' at \$${BRIEFLOW_BUDGET_USD}"

    EXISTING_BUDGET=$(gcloud billing budgets list \
        --billing-account="$BRIEFLOW_GCP_BILLING_ACCOUNT" \
        --filter="displayName:${BUDGET_NAME}" \
        --format="value(name)" 2>/dev/null | head -1 || true)

    if [[ -n "$EXISTING_BUDGET" ]]; then
        log "  budget ${BUDGET_NAME} already exists — skip"
    else
        gcloud billing budgets create \
            --billing-account="$BRIEFLOW_GCP_BILLING_ACCOUNT" \
            --display-name="$BUDGET_NAME" \
            --budget-amount="${BRIEFLOW_BUDGET_USD}USD" \
            --threshold-rule=percent=0.5 \
            --threshold-rule=percent=0.9 \
            --threshold-rule=percent=1.0 \
            --filter-projects="projects/${BRIEFLOW_GCP_PROJECT}" 2>&1 | tail -5 || {
                log "WARNING: budget creation failed. Likely missing 'roles/billing.user' on \$BRIEFLOW_GCP_BILLING_ACCOUNT. Skipping budget setup."
            }
    fi
else
    log "skipping billing budget step (no BRIEFLOW_GCP_BILLING_ACCOUNT set)"
fi

log "bootstrap complete."
log "  buckets: gs://${BRIEFLOW_BUCKET_INPUT}/  gs://${BRIEFLOW_BUCKET_OUTPUT}/"
log "  SA:      ${SA_EMAIL}"
log "  SSH:     gcloud compute ssh <vm> --tunnel-through-iap --zone=<zone>"
log "next: cloud/provision_vm.sh <screen-id> (per-screen flow)"

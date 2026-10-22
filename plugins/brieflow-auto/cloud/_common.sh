#!/usr/bin/env bash
# _common.sh — shared env vars + helpers for cloud/*.sh scripts.
# Sourced (not executed) by other scripts: `source "$(dirname "$0")/_common.sh"`.

set -euo pipefail

# ---------------------------------------------------------------------------
# Required env vars (caller MUST set these before invoking any script)
# ---------------------------------------------------------------------------

require_env() {
    local var="$1"
    if [[ -z "${!var:-}" ]]; then
        echo "ERROR: \$${var} must be set." >&2
        echo "See cloud/README.md for the one-time setup." >&2
        exit 2
    fi
}

# ---------------------------------------------------------------------------
# GCP project / region / zone — env-overridable with sensible defaults
# ---------------------------------------------------------------------------

: "${BRIEFLOW_GCP_PROJECT:=}"                  # required
: "${BRIEFLOW_GCP_REGION:=us-central1}"
: "${BRIEFLOW_GCP_ZONE:=us-central1-a}"
: "${BRIEFLOW_GCP_BILLING_ACCOUNT:=}"          # required for setup_gcp_project.sh
: "${BRIEFLOW_BUDGET_USD:=500}"                # billing alert threshold

# ---------------------------------------------------------------------------
# Bucket names — no defaults (plugin is lab-agnostic).
# Operator MUST set BRIEFLOW_BUCKET_INPUT + BRIEFLOW_BUCKET_OUTPUT.
# ---------------------------------------------------------------------------
# Two buckets per project:
#   INPUT   — raw nd2s + barcode libraries per screen
#   OUTPUT  — everything else: analysis outputs, optional setup_state.json,
#             telemetry, review artifacts (organized as subdir prefixes
#             gs://<output>/<screen>/{output,reviews,telemetry,...}/)

: "${BRIEFLOW_BUCKET_INPUT:=}"
: "${BRIEFLOW_BUCKET_OUTPUT:=}"

# ---------------------------------------------------------------------------
# VM defaults
# ---------------------------------------------------------------------------

# Machine type — brieflow's memory footprint dominates; high-mem class.
# n2-highmem-32: 32 vCPU, 256 GB RAM, ~$2.16/hr (us-central1, 2026-05).
# Override with $BRIEFLOW_VM_MACHINE_TYPE for cheaper or beefier instances.
: "${BRIEFLOW_VM_MACHINE_TYPE:=n2-highmem-32}"
: "${BRIEFLOW_VM_BOOT_DISK_SIZE_GB:=200}"
: "${BRIEFLOW_VM_BOOT_DISK_TYPE:=pd-ssd}"
: "${BRIEFLOW_VM_IMAGE_FAMILY:=brieflow-vm}"   # image family name (build_vm_image.sh creates this)
: "${BRIEFLOW_VM_IMAGE_PROJECT:=${BRIEFLOW_GCP_PROJECT}}"
# NOTE: deliberately NO max-run-duration / auto-stop default — brieflow screens
# run for days, so a wall-clock STOP would kill a live run mid-phase. Cost is
# managed by stopping the VM when the screen is done, not a kill switch.

# Service account that VMs use to read/write GCS buckets.
: "${BRIEFLOW_VM_SERVICE_ACCOUNT:=brieflow-vm-sa@${BRIEFLOW_GCP_PROJECT}.iam.gserviceaccount.com}"

# Login user on the VM: resolve OS Login or explicitly select the account, then
# verify with brieflow_connection.py. Never derive this from local USER or email.
: "${BRIEFLOW_VM_USER:=}"

# ---------------------------------------------------------------------------
# Host layout — no default (any path on whichever host runs the pipeline)
# ---------------------------------------------------------------------------

# Directory holding <screen>-analysis/ (and a plugin clone, if any): a VM data
# disk, a cluster project dir, a workstation drive. Used by the setup skill's
# recipes; no cloud/*.sh reads it.
: "${BRIEFLOW_DATA_ROOT:=}"

# ---------------------------------------------------------------------------
# Helper funcs
# ---------------------------------------------------------------------------

log() { echo "[brieflow-cloud] $*" >&2; }

vm_name_for_screen() {
    # $1 = screen-id → "brieflow-<screen-id>-<YYYYMMDD>"
    local screen="$1"
    local date
    date=$(date +%Y%m%d)
    echo "brieflow-${screen}-${date}"
}

require_gcloud() {
    if ! command -v gcloud >/dev/null 2>&1; then
        echo "ERROR: gcloud CLI not found. Install: https://cloud.google.com/sdk/docs/install" >&2
        exit 3
    fi
    if ! gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>/dev/null | grep -q .; then
        echo "ERROR: no active gcloud auth. Run 'gcloud auth login' first." >&2
        exit 3
    fi
}

require_project() {
    require_env BRIEFLOW_GCP_PROJECT
}

# ---------------------------------------------------------------------------
# Host detection + setup_state.json helpers
# ---------------------------------------------------------------------------

detect_host() {
    # Capability hint only; this does not establish the machine's setup/runtime role.
    if curl -fs -m 1 -H "Metadata-Flavor: Google" \
        http://169.254.169.254/computeMetadata/v1/instance/id >/dev/null 2>&1; then
        echo "vm"
    elif command -v sbatch >/dev/null 2>&1; then
        echo "hpc"
    else
        echo "local"
    fi
}

setup_state_uri() {
    # $1 = screen-id → gs://<output-bucket>/<screen>/setup_state.json
    # (Optional state file for re-entrance; see setup_state_schema.md.)
    local screen="$1"
    echo "gs://${BRIEFLOW_BUCKET_OUTPUT}/${screen}/setup_state.json"
}

read_setup_state() {
    # $1 = screen-id; echoes JSON to stdout, exits 1 if not found.
    local screen="$1"
    local uri
    uri=$(setup_state_uri "$screen")
    gcloud storage cat "$uri" 2>/dev/null || {
        echo "ERROR: no setup_state.json at ${uri}" >&2
        return 1
    }
}

write_setup_state() {
    # $1 = screen-id; reads JSON from stdin and writes to GCS.
    local screen="$1"
    local uri
    uri=$(setup_state_uri "$screen")
    local tmp
    tmp=$(mktemp -p .)
    trap "rm -f '$tmp'" RETURN
    cat > "$tmp"
    # Validate JSON before upload.
    if ! python3 -c "import json,sys; json.load(open('$tmp'))" 2>/dev/null; then
        echo "ERROR: write_setup_state received invalid JSON" >&2
        return 1
    fi
    gcloud storage cp "$tmp" "$uri" >/dev/null
    log "wrote setup_state to ${uri}"
}

#!/usr/bin/env bash
# teardown_vm.sh <screen-id> — push outputs to GCS and delete the VM.
#
# Lifecycle hooks (Phase 1.1) push outputs to GCS automatically on
# successful runloop exit; this script is the explicit "I'm done, kill it"
# operator action. It also handles partial-output rescue: pushes whatever
# is in /data/<screen-id>/brieflow_output/ to gs://${BRIEFLOW_BUCKET_OUTPUT}/<screen-id>/
# before deletion, even if the runloop is mid-flight.
#
# Use with care: this DESTROYS the VM. Diagnostic data not yet pushed to
# GCS is lost. For diagnostic harvesting, SSH in first.

source "$(dirname "$0")/_common.sh"
require_gcloud
require_project
require_env BRIEFLOW_BUCKET_OUTPUT

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <screen-id> [--force]" >&2
    exit 2
fi
SCREEN_ID="$1"
FORCE="${2:-}"
VM_NAME=$(vm_name_for_screen "$SCREEN_ID")

if ! gcloud compute instances describe "$VM_NAME" --zone="$BRIEFLOW_GCP_ZONE" >/dev/null 2>&1; then
    log "VM ${VM_NAME} not found. Already torn down?"
    exit 0
fi

# Confirm destruction unless --force
if [[ "$FORCE" != "--force" ]]; then
    STATUS=$(gcloud compute instances describe "$VM_NAME" --zone="$BRIEFLOW_GCP_ZONE" --format='value(status)')
    echo "About to delete VM ${VM_NAME} (status: ${STATUS})."
    echo "Type 'yes' to confirm (or re-run with --force to skip this prompt):"
    read -r confirm
    if [[ "$confirm" != "yes" ]]; then
        log "aborting teardown."
        exit 0
    fi
fi

# Push outputs to GCS (best-effort; lifecycle hooks usually already did this)
log "pushing /data/${SCREEN_ID}/brieflow_output/ to gs://${BRIEFLOW_BUCKET_OUTPUT}/${SCREEN_ID}/"
gcloud compute ssh "$VM_NAME" --zone="$BRIEFLOW_GCP_ZONE" --command="
    if [[ -d /data/${SCREEN_ID}/brieflow_output ]]; then
        gsutil -m rsync -r /data/${SCREEN_ID}/brieflow_output gs://${BRIEFLOW_BUCKET_OUTPUT}/${SCREEN_ID}/brieflow_output/ 2>&1 | tail -5 || true
    fi
    if [[ -d /data/${SCREEN_ID}/logs ]]; then
        gsutil -m rsync -r /data/${SCREEN_ID}/logs gs://${BRIEFLOW_BUCKET_OUTPUT}/${SCREEN_ID}/logs/ 2>&1 | tail -5 || true
    fi
    if [[ -d /data/${SCREEN_ID}/.brieflow_runloop ]]; then
        gsutil -m rsync -r /data/${SCREEN_ID}/.brieflow_runloop gs://${BRIEFLOW_BUCKET_OUTPUT}/${SCREEN_ID}/.brieflow_runloop/ 2>&1 | tail -5 || true
    fi
" 2>&1 | tail -10 || log "WARN: GCS push had issues; proceed with deletion anyway"

# Delete VM
log "deleting VM ${VM_NAME}"
gcloud compute instances delete "$VM_NAME" --zone="$BRIEFLOW_GCP_ZONE" --quiet

log "VM ${VM_NAME} deleted. Outputs at gs://${BRIEFLOW_BUCKET_OUTPUT}/${SCREEN_ID}/"

#!/usr/bin/env bash
# provision_vm.sh <screen-id> [--data-disk-tb N] [--no-image] — spin up a brieflow VM.
#
# Creates a VM with:
#   - 200 GB boot disk (OS + plugin + miniconda base if using baked image)
#   - separate PD-SSD data disk (default 1 TB; --data-disk-tb to override)
#   - service account for GCS access
#   - startup script that formats + mounts the data disk to /data/
#
# NO max-run-duration / auto-stop: a brieflow screen runs for days (sbs +
# phenotype + merge + aggregate + cluster), so an auto-STOP ceiling would
# guillotine a live run mid-phase. Cost is bounded by the operator stopping
# the VM when the screen finishes, not by a wall-clock kill switch.
#
# Default uses BRIEFLOW_VM_IMAGE_FAMILY (built by build_vm_image.sh). Pass
# --no-image to provision from the base ubuntu-2204-lts image instead and
# install tooling on first SSH (lighter-weight; no image-family management).
#
# Zone fallback: tries BRIEFLOW_GCP_ZONE first, then the rest of the region's
# zones via BRIEFLOW_GCP_ZONE_FALLBACK (default: same-region zones a/b/c/f).
# Large machine types (c3d-highmem-360, m2-megamem-*) often stockout in
# popular zones; the fallback typically lands within 1-2 tries.
#
# Tags the VM with the screen-id (for dashboard.sh + cost attribution).
# Idempotent-ish: if a VM with the screen-id-date already exists, prints
# its name and exits 0. Run teardown_vm.sh first if you want a fresh VM.

source "$(dirname "$0")/_common.sh"
require_gcloud
require_project

DATA_DISK_TB=1
SCREEN_ID=""
USE_IMAGE=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --data-disk-tb)
            DATA_DISK_TB="$2"
            shift 2
            ;;
        --data-disk-tb=*)
            DATA_DISK_TB="${1#*=}"
            shift
            ;;
        --no-image)
            USE_IMAGE=0
            shift
            ;;
        -h|--help)
            echo "usage: $0 <screen-id> [--data-disk-tb N] [--no-image]" >&2
            exit 0
            ;;
        *)
            if [[ -z "$SCREEN_ID" ]]; then
                SCREEN_ID="$1"
                shift
            else
                echo "unexpected argument: $1" >&2
                echo "usage: $0 <screen-id> [--data-disk-tb N] [--no-image]" >&2
                exit 2
            fi
            ;;
    esac
done

if [[ -z "$SCREEN_ID" ]]; then
    echo "usage: $0 <screen-id> [--data-disk-tb N]" >&2
    exit 2
fi

# Integer TB only, 1..64 reasonable bounds.
if ! [[ "$DATA_DISK_TB" =~ ^[0-9]+$ ]] || (( DATA_DISK_TB < 1 )) || (( DATA_DISK_TB > 64 )); then
    echo "ERROR: --data-disk-tb must be an integer 1..64 (got: ${DATA_DISK_TB})" >&2
    exit 2
fi

VM_NAME=$(vm_name_for_screen "$SCREEN_ID")
DATA_DISK_NAME="${VM_NAME}-data"
DATA_DISK_SIZE_GB=$((DATA_DISK_TB * 1024))

# Provision log lives in the cwd, never /tmp (shared HPC head node).
mkdir -p ./tmp
PROVISION_LOG="./tmp/${USER}_provision_${SCREEN_ID}.log"

# Build zone-fallback list: BRIEFLOW_GCP_ZONE first, then the others in the
# same region (a → b → c → f). Configurable via BRIEFLOW_GCP_ZONE_FALLBACK
# (comma-separated zone names).
REGION_DEFAULT_ZONES=(
    "${BRIEFLOW_GCP_REGION}-a"
    "${BRIEFLOW_GCP_REGION}-b"
    "${BRIEFLOW_GCP_REGION}-c"
    "${BRIEFLOW_GCP_REGION}-f"
)
if [[ -n "${BRIEFLOW_GCP_ZONE_FALLBACK:-}" ]]; then
    IFS=',' read -ra REGION_DEFAULT_ZONES <<< "$BRIEFLOW_GCP_ZONE_FALLBACK"
fi
ZONE_ORDER=("$BRIEFLOW_GCP_ZONE")
for z in "${REGION_DEFAULT_ZONES[@]}"; do
    [[ "$z" != "$BRIEFLOW_GCP_ZONE" ]] && ZONE_ORDER+=("$z")
done

# Already exists in any candidate zone?
for Z in "${ZONE_ORDER[@]}"; do
    if gcloud compute instances describe "$VM_NAME" --zone="$Z" >/dev/null 2>&1; then
        log "VM ${VM_NAME} already exists in zone ${Z}. Use teardown_vm.sh first if you want a fresh one."
        log "  status: $(gcloud compute instances describe "$VM_NAME" --zone="$Z" --format='value(status)')"
        exit 0
    fi
done

# Decide image source: baked image family vs base ubuntu-2204-lts.
if [[ "$USE_IMAGE" == "1" ]]; then
    IMAGE_ARGS=(--image-family="$BRIEFLOW_VM_IMAGE_FAMILY" --image-project="$BRIEFLOW_VM_IMAGE_PROJECT")
    IMAGE_DESC="baked image family ${BRIEFLOW_VM_IMAGE_FAMILY}"
else
    IMAGE_ARGS=(--image-family="ubuntu-2204-lts" --image-project="ubuntu-os-cloud")
    IMAGE_DESC="base ubuntu-2204-lts (install tooling on first SSH)"
fi

log "provisioning ${VM_NAME}"
log "  machine:   ${BRIEFLOW_VM_MACHINE_TYPE}"
log "  zone:      ${BRIEFLOW_GCP_ZONE} (fallback: ${ZONE_ORDER[*]})"
log "  boot:      ${BRIEFLOW_VM_BOOT_DISK_SIZE_GB} GB ${BRIEFLOW_VM_BOOT_DISK_TYPE}, ${IMAGE_DESC}"
log "  data disk: ${DATA_DISK_TB} TB pd-ssd (mounted at /data/)"

# Try each zone in order; stop on first success.
ATTEMPTED_ZONE=""
for Z in "${ZONE_ORDER[@]}"; do
    log "attempting zone ${Z}"
    if gcloud compute instances create "$VM_NAME" \
    --zone="$Z" \
    --machine-type="$BRIEFLOW_VM_MACHINE_TYPE" \
    "${IMAGE_ARGS[@]}" \
    --boot-disk-size="${BRIEFLOW_VM_BOOT_DISK_SIZE_GB}GB" \
    --boot-disk-type="$BRIEFLOW_VM_BOOT_DISK_TYPE" \
    --create-disk="name=${DATA_DISK_NAME},size=${DATA_DISK_SIZE_GB}GB,type=pd-ssd,auto-delete=yes,device-name=brieflow-data" \
    --service-account="$BRIEFLOW_VM_SERVICE_ACCOUNT" \
    --scopes=cloud-platform \
    --labels="brieflow-screen=${SCREEN_ID},brieflow-managed=true" \
    --metadata="brieflow-screen-id=${SCREEN_ID},brieflow-provisioned-at=$(date -Iseconds)" \
    --metadata-from-file=startup-script=<(cat <<STARTUP_SCRIPT
#!/bin/bash
# VM startup: format + mount the attached data disk at /data/.
set -e

DEVICE=/dev/disk/by-id/google-brieflow-data
MOUNT=/data
SCREEN_DATA_DIR=\${MOUNT}/${SCREEN_ID}

# Wait for device to appear.
for i in {1..30}; do
    [[ -e "\$DEVICE" ]] && break
    sleep 2
done

if [[ ! -e "\$DEVICE" ]]; then
    echo "ERROR: data disk \$DEVICE never appeared" >&2
    exit 1
fi

# Format if blank.
if ! blkid "\$DEVICE" >/dev/null 2>&1; then
    mkfs.ext4 -F -L brieflow-data "\$DEVICE"
fi

mkdir -p "\$MOUNT"
if ! mount | grep -q " \$MOUNT "; then
    mount "\$DEVICE" "\$MOUNT"
fi

# Persist via fstab.
if ! grep -q "\$MOUNT " /etc/fstab; then
    echo "\$DEVICE \$MOUNT ext4 defaults,nofail 0 2" >> /etc/fstab
fi

chmod 1777 "\$MOUNT"
mkdir -p "\$SCREEN_DATA_DIR"
chmod 1777 "\$SCREEN_DATA_DIR"

{
    echo "brieflow VM startup complete: \$(date -Iseconds)"
    echo "  data disk mounted: \$MOUNT"
    echo "  screen data dir:   \$SCREEN_DATA_DIR"
} > /var/log/brieflow-startup.log
STARTUP_SCRIPT
) 2>&1 | tee ${PROVISION_LOG}; then
        ATTEMPTED_ZONE="$Z"
        break
    fi

    # Zone capacity shortfall? Try next zone. Anything else, abort. gcloud
    # reports this several ways depending on the machine family: the older
    # `reason: stockout`, and the newer ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS
    # (message "does not have enough resources", state:STOCKOUT, reason:
    # resource_availability). Match all of them so fallback actually fires.
    if grep -qiE "stockout|ZONE_RESOURCE_POOL_EXHAUSTED|does not have enough resources|resource pool exhausted|resource_availability" "${PROVISION_LOG}" 2>/dev/null; then
        log "  capacity shortfall in ${Z}; trying next zone"
        continue
    fi
    log "  unexpected error in ${Z}; aborting (see ${PROVISION_LOG})"
    exit 3
done

if [[ -z "$ATTEMPTED_ZONE" ]]; then
    log "ERROR: stockout in all candidate zones (${ZONE_ORDER[*]})."
    log "  Options: wait + retry, file a future-reservation, use a smaller machine type."
    exit 4
fi

# Wait for SSH ready
log "waiting for SSH (via IAP)"
for i in {1..30}; do
    if gcloud compute ssh "$VM_NAME" --zone="$ATTEMPTED_ZONE" --tunnel-through-iap --command="echo ok" >/dev/null 2>&1; then
        break
    fi
    sleep 10
done

log "VM ${VM_NAME} ready in zone ${ATTEMPTED_ZONE}."
log "  SSH:  gcloud compute ssh ${VM_NAME} --zone=${ATTEMPTED_ZONE} --tunnel-through-iap"
log "  data: /data/${SCREEN_ID}/  (${DATA_DISK_TB} TB pd-ssd)"
log "next: kick off HPC→GCS transfer with cloud/transfer_hpc_to_gcs.sh"
log "tip:  stop the VM while transfer runs to save ~\$25-30/hr:"
log "        gcloud compute instances stop ${VM_NAME} --zone=${ATTEMPTED_ZONE}"

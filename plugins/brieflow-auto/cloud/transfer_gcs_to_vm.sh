#!/usr/bin/env bash
# transfer_gcs_to_vm.sh <screen-id> — pull GCS-resident screen data onto the VM.
#
# Runs on the VM (after provision_vm.sh + transfer_hpc_to_gcs.sh have finished).
# `gcloud storage rsync` of:
#   gs://${BRIEFLOW_BUCKET_INPUT}/<screen>/sbs/        → /data/<screen>/sbs/
#   gs://${BRIEFLOW_BUCKET_INPUT}/<screen>/phenotype/  → /data/<screen>/phenotype/
#
# Wraps in tmux (session name: brieflow-pull-<screen>) so the operator can
# detach and reconnect later. Idempotent — re-runnable; rsync skips files
# already in place.
#
# Pre-checks:
#   - /data/ is mounted (provision_vm.sh's startup script handles this)
#   - GCS source has data (gcloud storage du > 0)
#   - /data/ has enough free space (≥ source size)
#   - no existing tmux session with the same name (use --resume to attach)
#
# Usage:
#   transfer_gcs_to_vm.sh <screen-id>           # kick off in detached tmux
#   transfer_gcs_to_vm.sh <screen-id> --resume  # attach to existing session
#   transfer_gcs_to_vm.sh <screen-id> --foreground  # run inline (no tmux)

set -euo pipefail
source "$(dirname "$0")/_common.sh"

SCREEN_ID=""
MODE="detached"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --resume)
            MODE="resume"; shift
            ;;
        --foreground|--fg)
            MODE="foreground"; shift
            ;;
        -h|--help)
            grep '^# ' "$0" | head -25
            exit 0
            ;;
        *)
            if [[ -z "$SCREEN_ID" ]]; then
                SCREEN_ID="$1"; shift
            else
                echo "unexpected argument: $1" >&2
                exit 2
            fi
            ;;
    esac
done

[[ -n "$SCREEN_ID" ]] || { echo "usage: $0 <screen-id> [--resume|--foreground]" >&2; exit 2; }

command -v gcloud >/dev/null 2>&1 || { echo "ERROR: gcloud not found" >&2; exit 3; }
command -v tmux >/dev/null 2>&1   || { echo "ERROR: tmux not found" >&2; exit 3; }
require_env BRIEFLOW_BUCKET_INPUT

TMUX_SESSION="brieflow-pull-${SCREEN_ID}"
DATA_ROOT="/data/${SCREEN_ID}"
SRC_BASE="gs://${BRIEFLOW_BUCKET_INPUT}/${SCREEN_ID}"

# ---------------------------------------------------------------------------
# --resume: just attach to existing session
# ---------------------------------------------------------------------------

if [[ "$MODE" == "resume" ]]; then
    if ! tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
        echo "ERROR: no tmux session ${TMUX_SESSION} to resume" >&2
        exit 1
    fi
    exec tmux attach -t "$TMUX_SESSION"
fi

# ---------------------------------------------------------------------------
# Pre-checks
# ---------------------------------------------------------------------------

# /data mounted?
if ! mount | grep -q ' /data '; then
    echo "ERROR: /data is not mounted. provision_vm.sh's startup script should have done this." >&2
    echo "       Try: sudo mount /data  (or check /var/log/brieflow-startup.log)" >&2
    exit 4
fi

mkdir -p "${DATA_ROOT}/sbs" "${DATA_ROOT}/phenotype"

# GCS source has data?
log "checking GCS source: ${SRC_BASE}"
SRC_BYTES=$(gcloud storage du -s "${SRC_BASE}/" --readable-sizes 2>/dev/null | awk '{print $1}' || echo "")
SRC_TOTAL=$(gcloud storage du -s "${SRC_BASE}/" 2>/dev/null | awk '{sum+=$1} END {print sum+0}')

if [[ "$SRC_TOTAL" -eq 0 ]]; then
    echo "ERROR: no data at ${SRC_BASE}/ — has the HPC→GCS transfer completed?" >&2
    echo "       Check the Slurm job status on the HPC: sacct -j <jobid>" >&2
    exit 5
fi

SRC_HUMAN=$(numfmt --to=iec --suffix=B "$SRC_TOTAL")
log "GCS source size: ${SRC_HUMAN}"

# Local free space sufficient?
DST_FREE=$(df -B1 /data | awk 'NR==2 {print $4}')
DST_FREE_HUMAN=$(numfmt --to=iec --suffix=B "$DST_FREE")
log "/data free space: ${DST_FREE_HUMAN}"

if (( SRC_TOTAL > DST_FREE )); then
    echo "ERROR: source (${SRC_HUMAN}) exceeds /data free space (${DST_FREE_HUMAN})" >&2
    echo "       Re-provision the VM with a larger --data-disk-tb." >&2
    exit 6
fi

# Existing tmux session?
if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    echo "tmux session ${TMUX_SESSION} already exists." >&2
    echo "  attach: $0 ${SCREEN_ID} --resume" >&2
    echo "  kill:   tmux kill-session -t ${TMUX_SESSION}" >&2
    exit 7
fi

# ---------------------------------------------------------------------------
# Build the rsync command
# ---------------------------------------------------------------------------

RSYNC_CMD=$(cat <<RSYNC
set -e
echo "[\$(date -Iseconds)] starting GCS→VM rsync for ${SCREEN_ID}"
echo "  source: ${SRC_BASE}/"
echo "  dest:   ${DATA_ROOT}/"

gcloud storage rsync --recursive ${SRC_BASE}/sbs/       ${DATA_ROOT}/sbs/
gcloud storage rsync --recursive ${SRC_BASE}/phenotype/ ${DATA_ROOT}/phenotype/

echo "[\$(date -Iseconds)] rsync complete"
echo "VM_PULL_COMPLETE"
RSYNC
)

# ---------------------------------------------------------------------------
# Foreground vs detached
# ---------------------------------------------------------------------------

if [[ "$MODE" == "foreground" ]]; then
    bash -c "$RSYNC_CMD"
    exit 0
fi

# Detached tmux
log "launching tmux session: ${TMUX_SESSION}"
tmux new-session -d -s "$TMUX_SESSION" "bash -c \"$RSYNC_CMD; echo; echo 'session done; press any key to close'; read -n1\""

log "transfer running in tmux session ${TMUX_SESSION}"
log "  attach: tmux attach -t ${TMUX_SESSION}"
log "          or: $0 ${SCREEN_ID} --resume"
log "  detach inside tmux: Ctrl-b d"

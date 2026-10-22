#!/usr/bin/env bash
# transfer_hpc_to_gcs.sh — kick off HPC → GCS data transfer via Slurm sbatch.
#
# Runs on the HPC head node. Generates an sbatch script that wraps
# `gcloud storage rsync` of the operator's SBS + phenotype raw data dirs
# into gs://${BRIEFLOW_BUCKET_INPUT}/<screen>/{sbs,phenotype}/.
#
# Pre-flight:
#   1. du -sh of both source dirs (total size)
#   2. Transfer time estimate (assumes 1 Gbps sustained, override via
#      BRIEFLOW_TRANSFER_GBPS)
#   3. GCS storage cost estimate ($0.020/GB/month, us-central1 standard)
#   4. Operator confirm (y/N) — skip with --yes
#
# Submits sbatch with --mail-type=END,FAIL --mail-user=<operator email>.
# Prints the Slurm jobid for the wizard to record in setup_state.json.
#
# Notes:
#   - The screen-id used here is the GCP-side name (destination). If the HPC
#     source dir has a different name (e.g. a legacy alias of the screen),
#     pass the GCP name and point --sbs-dir/--phenotype-dir at the HPC paths.
#   - Inside the generated sbatch, gcloud parallelism is set via env vars
#     (CLOUDSDK_STORAGE_PROCESS_COUNT / _THREAD_COUNT). The legacy
#     `gcloud config set storage/...` approach with --installation fails on
#     compute nodes (no sudo on /usr/local/google-cloud-sdk/).
#
# Usage:
#   transfer_hpc_to_gcs.sh <screen-id> \
#       --sbs-dir /path/to/raw/<screen>/sbs/ \
#       --phenotype-dir /path/to/raw/<screen>/phenotype/ \
#       --email <operator-email> \
#       [--partition <slurm-partition>] \
#       [--cpus N]   (default 16; parallelism for gcloud rsync)
#       [--yes]

set -euo pipefail
source "$(dirname "$0")/_common.sh"

# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------

SCREEN_ID=""
SBS_DIR=""
PHENOTYPE_DIR=""
EMAIL=""
PARTITION="${BRIEFLOW_TRANSFER_PARTITION:-}"
CPUS=16
YES=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --sbs-dir)        SBS_DIR="$2"; shift 2 ;;
        --phenotype-dir)  PHENOTYPE_DIR="$2"; shift 2 ;;
        --email)          EMAIL="$2"; shift 2 ;;
        --partition)      PARTITION="$2"; shift 2 ;;
        --cpus)           CPUS="$2"; shift 2 ;;
        --yes|-y)         YES=1; shift ;;
        -h|--help)
            grep '^# ' "$0" | head -30
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

# Required
[[ -n "$SCREEN_ID"      ]] || { echo "ERROR: missing <screen-id>" >&2; exit 2; }
[[ -n "$SBS_DIR"        ]] || { echo "ERROR: missing --sbs-dir" >&2; exit 2; }
[[ -n "$PHENOTYPE_DIR"  ]] || { echo "ERROR: missing --phenotype-dir" >&2; exit 2; }
[[ -n "$EMAIL"          ]] || { echo "ERROR: missing --email" >&2; exit 2; }
[[ -n "$PARTITION"      ]] || { echo "ERROR: missing --partition (or set BRIEFLOW_TRANSFER_PARTITION)" >&2; exit 2; }

[[ -d "$SBS_DIR"       ]] || { echo "ERROR: SBS dir does not exist: $SBS_DIR" >&2; exit 2; }
[[ -d "$PHENOTYPE_DIR" ]] || { echo "ERROR: phenotype dir does not exist: $PHENOTYPE_DIR" >&2; exit 2; }

command -v sbatch >/dev/null 2>&1 || { echo "ERROR: sbatch not found — must be run on a Slurm cluster" >&2; exit 3; }
require_gcloud
require_env BRIEFLOW_BUCKET_INPUT

# ---------------------------------------------------------------------------
# Pre-flight: size + cost + time estimate
# ---------------------------------------------------------------------------

log "measuring source sizes (du -sb)..."
SBS_BYTES=$(du -sb "$SBS_DIR" | awk '{print $1}')
PHENO_BYTES=$(du -sb "$PHENOTYPE_DIR" | awk '{print $1}')
TOTAL_BYTES=$((SBS_BYTES + PHENO_BYTES))

# Format sizes via numfmt
SBS_HUMAN=$(numfmt --to=iec --suffix=B "$SBS_BYTES")
PHENO_HUMAN=$(numfmt --to=iec --suffix=B "$PHENO_BYTES")
TOTAL_HUMAN=$(numfmt --to=iec --suffix=B "$TOTAL_BYTES")

# Transfer time: bytes * 8 / (gbps * 1e9) seconds
SUSTAINED_GBPS="${BRIEFLOW_TRANSFER_GBPS:-1}"
TRANSFER_SECONDS=$(awk -v b="$TOTAL_BYTES" -v g="$SUSTAINED_GBPS" 'BEGIN { print int(b * 8 / (g * 1000000000)) }')
TRANSFER_HOURS=$(awk -v s="$TRANSFER_SECONDS" 'BEGIN { printf "%.1f", s/3600 }')

# Storage cost: $0.020/GB/month (us-central1 standard)
TOTAL_GB=$(awk -v b="$TOTAL_BYTES" 'BEGIN { printf "%.1f", b / 1000000000 }')
COST_USD=$(awk -v gb="$TOTAL_GB" 'BEGIN { printf "%.2f", gb * 0.020 }')

DEST_SBS="gs://${BRIEFLOW_BUCKET_INPUT}/${SCREEN_ID}/sbs/"
DEST_PHENO="gs://${BRIEFLOW_BUCKET_INPUT}/${SCREEN_ID}/phenotype/"

cat <<EOF >&2

HPC → GCS transfer preview for screen "${SCREEN_ID}"

Source                                            Size
  ${SBS_DIR}        ${SBS_HUMAN}
  ${PHENOTYPE_DIR}  ${PHENO_HUMAN}
  ─────────────────────────────────────────────   ─────
  total                                            ${TOTAL_HUMAN}

Destination
  ${DEST_SBS}
  ${DEST_PHENO}

Estimates
  transfer time @ ${SUSTAINED_GBPS} Gbps sustained: ~${TRANSFER_HOURS} hours
  GCS standard storage:                            ~\$${COST_USD}/month while in bucket

Slurm
  partition:      ${PARTITION}
  email (END,FAIL): ${EMAIL}

EOF

if [[ "$YES" != "1" ]]; then
    read -r -p "Submit? [y/N] " ans
    case "$ans" in
        y|Y|yes|YES) : ;;
        *) echo "aborted"; exit 0 ;;
    esac
fi

# ---------------------------------------------------------------------------
# Generate + submit sbatch
# ---------------------------------------------------------------------------

SBATCH_FILE=$(mktemp -p . "${USER}_brieflow_xfer_${SCREEN_ID}.XXXXXX.sbatch")
trap "rm -f '$SBATCH_FILE'" EXIT

# Safe time ceiling: 2× estimate, rounded up to nearest hour, min 4h, max 7 days.
TIME_HOURS=$(awk -v h="$TRANSFER_HOURS" 'BEGIN { t=int(h*2+1); if (t<4) t=4; if (t>168) t=168; print t }')

cat > "$SBATCH_FILE" <<EOF
#!/bin/bash
#SBATCH --job-name=${USER}_brieflow_xfer_${SCREEN_ID}
#SBATCH --partition=${PARTITION}
#SBATCH --array=1-2
#SBATCH --cpus-per-task=${CPUS}
#SBATCH --mem=32G
#SBATCH --time=${TIME_HOURS}:00:00
#SBATCH --mail-type=BEGIN,END,FAIL
#SBATCH --mail-user=${EMAIL}
#SBATCH --output=%x_%A_%a.out
#SBATCH --error=%x_%A_%a.err

set -euo pipefail

# One sbatch array task per source dir. Independent retry per dir on failure.
case "\$SLURM_ARRAY_TASK_ID" in
    1) SRC=${SBS_DIR%/}/;        DST=${DEST_SBS}   ;;
    2) SRC=${PHENOTYPE_DIR%/}/;  DST=${DEST_PHENO} ;;
    *) echo "ERROR: bad array idx \$SLURM_ARRAY_TASK_ID" >&2; exit 2 ;;
esac

echo "brieflow HPC→GCS transfer task \$SLURM_ARRAY_TASK_ID starting: \$(date -Iseconds)"
echo "  screen:    ${SCREEN_ID}"
echo "  src:       \$SRC"
echo "  dst:       \$DST"
echo "  estimate:  ${TRANSFER_HOURS} hours @ ${SUSTAINED_GBPS} Gbps (total)"

# Crank up gcloud parallelism via env vars. The legacy approach
#   gcloud config set storage/process_count N --installation
# fails on compute nodes because /usr/local/google-cloud-sdk/ requires sudo.
export CLOUDSDK_STORAGE_PROCESS_COUNT=${CPUS}
export CLOUDSDK_STORAGE_THREAD_COUNT=4

# gcloud storage rsync is idempotent: re-running skips files already present
# with matching size/checksum. A single transient GCS 5xx (e.g. "Temporary
# components were not uploaded correctly") otherwise fails the whole task under
# set -e — wrap in a bounded retry. Each retry resumes, skipping completed
# objects, so this is cheap and safe.
MAX_TRIES=5
try=1
while true; do
    if gcloud storage rsync --recursive "\$SRC" "\$DST"; then
        break
    else
        rc=\$?  # exit status of the rsync condition above
        if (( try >= MAX_TRIES )); then
            echo "ERROR: rsync failed after \${MAX_TRIES} attempts (last rc=\$rc)" >&2
            exit \$rc
        fi
        backoff=\$(( try * 30 ))
        echo "rsync attempt \$try failed (rc=\$rc); retrying in \${backoff}s (resumable, skips done objects)" >&2
        sleep "\$backoff"
        try=\$(( try + 1 ))
    fi
done

echo "TRANSFER_TASK_COMPLETE \$SLURM_ARRAY_TASK_ID \$(date -Iseconds)"
EOF

log "submitting sbatch script: $SBATCH_FILE"
SBATCH_OUT=$(sbatch "$SBATCH_FILE")
echo "$SBATCH_OUT" >&2

JOBID=$(echo "$SBATCH_OUT" | awk '{print $NF}')

# Emit just the jobid on stdout so the wizard can capture it cleanly.
echo "$JOBID"

log "jobid: $JOBID"
log "check status: sacct -j $JOBID --format=State,ExitCode,Elapsed,MaxRSS"
log "Slurm will email ${EMAIL} on END or FAIL."

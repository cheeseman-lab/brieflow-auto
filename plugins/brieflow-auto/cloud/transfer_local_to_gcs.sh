#!/usr/bin/env bash
# transfer_local_to_gcs.sh — kick off LOCAL machine → GCS data transfer.
#
# Runs on the operator's laptop or workstation (NO slurm). The sibling
# transfer_hpc_to_gcs.sh handles the HPC + slurm case.
#
# What it does:
#   1. du -sh of each source dir (size + cost estimate)
#   2. Operator confirm (y/N) — skip with --yes
#   3. Launches one parallel `gcloud storage rsync` per --src arg in a
#      detached tmux/screen session so it survives terminal close
#   4. Sleep-prevents the host where possible (caffeinate on macOS,
#      systemd-inhibit on linux). The operator should still keep the
#      machine plugged in.
#
# Usage:
#   transfer_local_to_gcs.sh <screen-id> \
#       --src <local-dir>:<dest-subpath> \
#       --src <local-dir>:<dest-subpath> \
#       ...
#       [--cpus N]    (default = nproc/2 or 4)
#       [--no-tmux]   (run in foreground; cwd-blocked until done)
#       [--yes]
#
# Each --src is "<local-path>:<dest-subpath>" — operator chooses how to
# organize the GCS dest. Example for a 2-plate, sbs+ph screen:
#
#   --src ~/data/<screen>/plate_1/input_sbs:plate_1/input_sbs \
#   --src ~/data/<screen>/plate_1/input_ph:plate_1/input_ph  \
#   --src ~/data/<screen>/plate_2/input_sbs:plate_2/input_sbs \
#   --src ~/data/<screen>/plate_2/input_ph:plate_2/input_ph
#
# Produces: gs://${BRIEFLOW_BUCKET_INPUT}/<screen-id>/<dest-subpath>/
#
# All transfers are launched concurrently in shell backgrounds inside a
# tmux session named `brieflow-upload-<screen-id>`. `gcloud storage rsync`
# is resumable — re-run after partial failures to pick up where it stopped.

set -euo pipefail
source "$(dirname "$0")/_common.sh"
require_gcloud
require_env BRIEFLOW_BUCKET_INPUT

SCREEN_ID=""
SRCS=()
CPUS=""
USE_TMUX=1
YES=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --src)       SRCS+=("$2"); shift 2 ;;
        --cpus)      CPUS="$2"; shift 2 ;;
        --no-tmux)   USE_TMUX=0; shift ;;
        --yes|-y)    YES=1; shift ;;
        -h|--help)
            grep '^# ' "$0" | head -40 | sed 's/^# //'
            exit 0
            ;;
        *)
            if [[ -z "$SCREEN_ID" ]]; then
                SCREEN_ID="$1"; shift
            else
                echo "unexpected argument: $1" >&2; exit 2
            fi
            ;;
    esac
done

[[ -n "$SCREEN_ID" ]] || { echo "ERROR: missing <screen-id>" >&2; exit 2; }
[[ ${#SRCS[@]} -gt 0 ]] || { echo "ERROR: need at least one --src <local-path>:<dest-subpath>" >&2; exit 2; }

# Default CPU parallelism: half the cores, floor 4
if [[ -z "$CPUS" ]]; then
    if command -v nproc >/dev/null 2>&1; then
        CPUS=$(( $(nproc) / 2 ))
    elif command -v sysctl >/dev/null 2>&1; then
        CPUS=$(( $(sysctl -n hw.ncpu) / 2 ))
    else
        CPUS=4
    fi
    (( CPUS < 4 )) && CPUS=4
fi

# Parse + validate each --src arg
declare -a SRC_PATHS DEST_SUBS
TOTAL_BYTES=0
for spec in "${SRCS[@]}"; do
    src="${spec%%:*}"
    dst="${spec#*:}"
    [[ "$src" != "$spec" ]] || { echo "ERROR: --src must be <local-path>:<dest-subpath> (got: $spec)" >&2; exit 2; }
    [[ -d "$src" ]] || { echo "ERROR: not a directory: $src" >&2; exit 2; }
    SRC_PATHS+=("${src%/}")
    DEST_SUBS+=("${dst%/}")
    if [[ "$(uname)" == "Darwin" ]]; then
        bytes=$(du -sk "$src" | awk '{print $1 * 1024}')
    else
        bytes=$(du -sb "$src" | awk '{print $1}')
    fi
    TOTAL_BYTES=$((TOTAL_BYTES + bytes))
done

TOTAL_HUMAN=$(awk -v b="$TOTAL_BYTES" 'BEGIN { unit="B"; n=b; for (u="KMGTP"; n>=1024 && length(u)>0; u=substr(u,2)) { n/=1024; unit=substr(u,1,1) "B" } printf "%.1f %s", n, unit }')
COST_USD=$(awk -v b="$TOTAL_BYTES" 'BEGIN { printf "%.2f", b/1000/1000/1000 * 0.020 }')

cat <<EOF >&2

LOCAL → GCS transfer preview for screen "${SCREEN_ID}"

Source(s):
EOF
for i in "${!SRC_PATHS[@]}"; do
    echo "  ${SRC_PATHS[$i]}  →  gs://${BRIEFLOW_BUCKET_INPUT}/${SCREEN_ID}/${DEST_SUBS[$i]}/" >&2
done
cat <<EOF >&2

Total size:        ${TOTAL_HUMAN}
GCS storage:       ~\$${COST_USD}/month while in bucket
gcloud parallelism: ${CPUS} processes × 4 threads
Run mode:          $([[ $USE_TMUX == 1 ]] && echo 'tmux (detached)' || echo 'foreground')

EOF

if [[ "$YES" != "1" ]]; then
    read -r -p "Proceed? [y/N] " ans
    case "$ans" in y|Y|yes|YES) : ;; *) echo "aborted"; exit 0 ;; esac
fi

# Sleep-prevent the host while the transfer runs
if [[ "$(uname)" == "Darwin" ]] && command -v caffeinate >/dev/null 2>&1; then
    SLEEP_PREVENT_CMD="caffeinate -i"
elif command -v systemd-inhibit >/dev/null 2>&1; then
    SLEEP_PREVENT_CMD="systemd-inhibit --what=idle:sleep --who=brieflow --why=upload"
else
    SLEEP_PREVENT_CMD=""
fi

# Build the inner shell block: env vars + parallel rsync per source
INNER=$(cat <<INNER_EOF
set -euo pipefail
export CLOUDSDK_STORAGE_PROCESS_COUNT=${CPUS}
export CLOUDSDK_STORAGE_THREAD_COUNT=4

echo "brieflow LOCAL→GCS transfer starting: \$(date -Iseconds)"
echo "  screen: ${SCREEN_ID}"

pids=()
INNER_EOF
)

for i in "${!SRC_PATHS[@]}"; do
    INNER+=$'\n'"echo '  - ${SRC_PATHS[$i]} → ${DEST_SUBS[$i]}'"
    INNER+=$'\n'"gcloud storage rsync --recursive '${SRC_PATHS[$i]}' 'gs://${BRIEFLOW_BUCKET_INPUT}/${SCREEN_ID}/${DEST_SUBS[$i]}/' &"
    INNER+=$'\n'"pids+=(\$!)"
done

INNER+=$'\n'"echo 'all rsync jobs launched: '\${pids[@]}"
INNER+=$'\n'"wait \${pids[@]}"
INNER+=$'\n'"echo 'TRANSFER_COMPLETE '\$(date -Iseconds)"

SESSION="brieflow-upload-${SCREEN_ID}"
LOG="brieflow-upload-${SCREEN_ID}.log"

if [[ "$USE_TMUX" == "1" ]] && command -v tmux >/dev/null 2>&1; then
    if tmux has-session -t "$SESSION" 2>/dev/null; then
        log "tmux session ${SESSION} already exists. Re-attach with: tmux a -t ${SESSION}"
        exit 0
    fi
    tmux new-session -d -s "$SESSION" "$SLEEP_PREVENT_CMD bash -c \"$INNER\" 2>&1 | tee $LOG"
    log "transfer launched in tmux session: ${SESSION}"
    log "  re-attach: tmux a -t ${SESSION}"
    log "  detach:    Ctrl-b d"
    log "  log:       ${LOG}"
elif [[ "$USE_TMUX" == "1" ]]; then
    log "tmux not found; falling back to nohup. Install tmux for re-attach support."
    nohup bash -c "$SLEEP_PREVENT_CMD bash -c \"$INNER\"" > "$LOG" 2>&1 &
    log "transfer launched as PID $!; log: ${LOG}"
else
    eval "$SLEEP_PREVENT_CMD bash -c \"$INNER\" 2>&1 | tee $LOG"
fi

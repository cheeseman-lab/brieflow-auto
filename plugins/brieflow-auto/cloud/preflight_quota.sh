#!/usr/bin/env bash
# preflight_quota.sh — pre-provision GCP quota check for a planned VM fleet.
#
# The generic `gcloud compute instances create` failure conflates two very
# different problems — zone stockout vs per-VM-family CPU quota — behind the
# same "try another zone or request more quota" message. This script settles
# the quota question *before* any VM is created, so a fleet plan that can't
# fit is caught up front rather than at the Nth provision.
#
# It answers two questions the onboarding hit in practice:
#   D1. Does the region's per-VM-family CPU quota fit (machine vCPUs ×
#       intended concurrency) + current usage?
#   D2. What is the *granted* quota limit (a request for 1500 may be granted
#       1380 → only 3×360 fit, not 4)? We read the live limit, never the
#       requested value, so the plan is checked against reality.
#
# Usage:
#   preflight_quota.sh [--machine-type TYPE] [--concurrency N] [--region REGION]
#
#   --machine-type  default $BRIEFLOW_VM_MACHINE_TYPE
#   --concurrency   how many such VMs you intend to run at once (default 1)
#   --region        default $BRIEFLOW_GCP_REGION
#
# Exit: 0 = fits; 1 = shortfall (prints the deficit + request steps); 2 = usage error.

source "$(dirname "$0")/_common.sh"
require_gcloud
require_project

MACHINE_TYPE="$BRIEFLOW_VM_MACHINE_TYPE"
CONCURRENCY=1
REGION="$BRIEFLOW_GCP_REGION"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --machine-type)  MACHINE_TYPE="$2"; shift 2 ;;
        --concurrency)   CONCURRENCY="$2"; shift 2 ;;
        --region)        REGION="$2"; shift 2 ;;
        -h|--help)       grep '^# ' "$0" | head -30; exit 0 ;;
        *) echo "unexpected argument: $1" >&2; exit 2 ;;
    esac
done

[[ "$CONCURRENCY" =~ ^[0-9]+$ ]] && (( CONCURRENCY >= 1 )) || {
    echo "ERROR: --concurrency must be a positive integer (got: ${CONCURRENCY})" >&2; exit 2; }

# vCPUs for this machine type — query gcloud rather than parsing the name, so
# custom and predefined types both resolve. Needs a zone; use region-a.
PROBE_ZONE="${REGION}-a"
GUEST_CPUS=$(gcloud compute machine-types describe "$MACHINE_TYPE" \
    --zone="$PROBE_ZONE" --format='value(guestCpus)' 2>/dev/null || true)
if [[ -z "$GUEST_CPUS" ]]; then
    echo "ERROR: could not resolve vCPUs for machine type '${MACHINE_TYPE}' in ${PROBE_ZONE}." >&2
    echo "  Check the type name and that the region is correct." >&2
    exit 2
fi

NEEDED=$(( GUEST_CPUS * CONCURRENCY ))

# Per-VM-family CPU quota metric. Family = token before the first '-'
# (c3d-highmem-360 → c3d → C3D_CPUS). Fall back to the region-wide CPUS metric
# if the family-specific quota isn't published for this region.
FAMILY=$(echo "$MACHINE_TYPE" | cut -d- -f1 | tr '[:lower:]' '[:upper:]')
FAMILY_METRIC="${FAMILY}_CPUS"

# Read one region quota metric's limit + usage via JSON.
read_metric() {
    local metric="$1"
    gcloud compute regions describe "$REGION" \
        --format="json(quotas)" 2>/dev/null \
    | python3 -c "
import json,sys
m='${metric}'
d=json.load(sys.stdin)
for q in d.get('quotas',[]):
    if q.get('metric')==m:
        print(q.get('limit',0), q.get('usage',0)); break
"
}

METRIC="$FAMILY_METRIC"
RES=$(read_metric "$FAMILY_METRIC")
if [[ -z "$RES" ]]; then
    METRIC="CPUS"
    RES=$(read_metric "CPUS")
fi
if [[ -z "$RES" ]]; then
    echo "ERROR: neither ${FAMILY_METRIC} nor CPUS quota found for ${REGION}." >&2
    exit 2
fi

LIMIT=$(echo "$RES" | awk '{printf "%d", $1}')
USAGE=$(echo "$RES" | awk '{printf "%d", $2}')
AVAILABLE=$(( LIMIT - USAGE ))

log "quota preflight for ${CONCURRENCY}× ${MACHINE_TYPE} in ${REGION}"
log "  metric:     ${METRIC} (granted limit, not requested)"
log "  vCPUs/VM:   ${GUEST_CPUS}"
log "  need:       ${NEEDED}  (= ${GUEST_CPUS} × ${CONCURRENCY})"
log "  limit:      ${LIMIT}"
log "  in use:     ${USAGE}"
log "  available:  ${AVAILABLE}"

if (( NEEDED <= AVAILABLE )); then
    FITS=$(( AVAILABLE / GUEST_CPUS ))
    log "✓ fits — ${AVAILABLE} available vCPUs hold up to ${FITS} concurrent ${MACHINE_TYPE}."
    exit 0
fi

DEFICIT=$(( NEEDED - AVAILABLE ))
MAX_NOW=$(( AVAILABLE / GUEST_CPUS ))
cat >&2 <<EOF

✗ QUOTA SHORTFALL — the planned fleet does not fit.
    need ${NEEDED} vCPUs, only ${AVAILABLE} available (limit ${LIMIT} − usage ${USAGE}).
    deficit: ${DEFICIT} vCPUs. At ${GUEST_CPUS} vCPUs/VM, you can run ${MAX_NOW} now, not ${CONCURRENCY}.

To raise ${METRIC} in ${REGION}:
  console: IAM & Admin → Quotas → filter "${METRIC}" / region ${REGION} → Edit → request
           ceil(need + headroom). Requests can be granted BELOW what you ask —
           re-run this preflight after approval to confirm the *granted* limit fits.
  cli:     gcloud alpha services quota update \\
             --service=compute.googleapis.com --consumer=projects/${BRIEFLOW_GCP_PROJECT} \\
             --metric=compute.googleapis.com/${METRIC,,} --value=${NEEDED} --unit=1/{project}/{region}
           (or file via the console; CLI quota edits require alpha + permissions)

Note: this is a QUOTA shortfall, distinct from a zone STOCKOUT. Provisioning may
still fail on stockout even with quota headroom — provision_vm.sh handles that via
zone fallback.
EOF
exit 1

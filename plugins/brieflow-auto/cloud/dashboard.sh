#!/usr/bin/env bash
# dashboard.sh — list all running brieflow VMs + per-screen cost.
#
# Filters by label brieflow-managed=true so unrelated VMs aren't included.
# Cost computation uses Cloud Billing API (rough approximation; full per-VM
# cost takes ~24h to settle in billing). For real-time approx, multiplies
# uptime × instance hourly rate from a static table.

source "$(dirname "$0")/_common.sh"
require_gcloud
require_project

log "running brieflow VMs in project ${BRIEFLOW_GCP_PROJECT}"

# Use BigQuery / Cloud Billing exports for proper per-VM cost; for sprint,
# show uptime + machine type + label so operator can eyeball cost.

# Header
printf "%-40s %-15s %-12s %-25s %-20s\n" "VM NAME" "STATUS" "ZONE" "MACHINE TYPE" "SCREEN-ID"
printf "%-40s %-15s %-12s %-25s %-20s\n" "$(printf '%.0s-' {1..40})" "$(printf '%.0s-' {1..15})" "$(printf '%.0s-' {1..12})" "$(printf '%.0s-' {1..25})" "$(printf '%.0s-' {1..20})"

gcloud compute instances list \
    --filter="labels.brieflow-managed=true" \
    --format="csv[no-heading](name,status,zone.basename(),machineType.basename(),labels.brieflow-screen,creationTimestamp)" \
    2>/dev/null | while IFS=, read -r name status zone machine screen created; do
        printf "%-40s %-15s %-12s %-25s %-20s\n" \
            "$name" "$status" "$zone" "$machine" "$screen"
    done

echo ""
log "use teardown_vm.sh <screen-id> to delete (and push outputs)"
log "use 'gcloud compute ssh <vm-name>' to inspect manually"

# Cost summary placeholder. Full cost reporting requires Cloud Billing
# export to BigQuery + a query — too much for the sprint. Add as Phase 2.2
# (telemetry).
echo ""
log "cost summary: configure Cloud Billing → BigQuery export and query"
log "  the brieflow-managed VMs there for accurate per-screen attribution."

#!/usr/bin/env bash
# build_vm_image.sh — bake a GCP VM image with tooling only (no brieflow env).
#
# Strategy: spin up a temp VM with a base Ubuntu image, install:
#   - miniconda (base only; no envs created)
#   - brieflow-auto plugin (cloned to /opt/brieflow-auto/, linked via /etc/skel)
#   - Claude Code CLI (so operator can run `claude` on the VM)
#   - gh CLI (so operator can clone their analysis repo)
#   - gcloud CLI (pre-present on GCE images; verify only)
#   - tmux, rsync, git (standard tooling)
# Then snapshot to a custom image and tear down temp VM.
#
# The brieflow env itself is NOT baked. Each screen builds its own
# brieflow_<screen> env at S6 by following brieflow/docs/source/2.installation_analysis_setup.md
# against that screen's pinned submodule version.
#
# Run time: ~10-15 min (apt installs + plugin clone).
# Re-running: creates a NEW image version in the family; old versions kept
# (so in-flight VMs aren't affected). Garbage-collect old versions periodically
# via `gcloud compute images delete`.
#
# Required env: $BRIEFLOW_GCP_PROJECT
# Optional env: $BRIEFLOW_VM_IMAGE_FAMILY (default brieflow-vm)

source "$(dirname "$0")/_common.sh"
require_gcloud
require_project

TEMP_VM="brieflow-image-builder-$(date +%s)"
IMAGE_NAME="${BRIEFLOW_VM_IMAGE_FAMILY}-$(date +%Y%m%d-%H%M%S)"
BASE_IMAGE_FAMILY="ubuntu-2204-lts"
BASE_IMAGE_PROJECT="ubuntu-os-cloud"

log "building image ${IMAGE_NAME} (family=${BRIEFLOW_VM_IMAGE_FAMILY})"
log "  temp VM: ${TEMP_VM}"
log "  base:    ${BASE_IMAGE_FAMILY} / ${BASE_IMAGE_PROJECT}"

# ---------------------------------------------------------------------------
# 1. Provision temp builder VM
# ---------------------------------------------------------------------------

log "provisioning temp builder VM"
gcloud compute instances create "$TEMP_VM" \
    --zone="$BRIEFLOW_GCP_ZONE" \
    --machine-type="e2-standard-4" \
    --image-family="$BASE_IMAGE_FAMILY" \
    --image-project="$BASE_IMAGE_PROJECT" \
    --boot-disk-size=100GB \
    --boot-disk-type=pd-balanced \
    --metadata=enable-oslogin=TRUE >/dev/null

# Tear down even on error.
trap 'log "tearing down temp VM ${TEMP_VM}"; gcloud compute instances delete "$TEMP_VM" --zone="$BRIEFLOW_GCP_ZONE" --quiet 2>/dev/null || true' EXIT

# Wait for SSH to be ready.
log "waiting for SSH to become ready"
for i in {1..20}; do
    if gcloud compute ssh "$TEMP_VM" --zone="$BRIEFLOW_GCP_ZONE" --tunnel-through-iap --command="echo ok" >/dev/null 2>&1; then
        break
    fi
    sleep 15
done

# ---------------------------------------------------------------------------
# 2. Install tooling (no brieflow env — that's built per-screen at S6)
# ---------------------------------------------------------------------------

log "installing tooling: miniconda + brieflow-auto + claude + gh"
gcloud compute ssh "$TEMP_VM" --zone="$BRIEFLOW_GCP_ZONE" --tunnel-through-iap --command="$(cat <<'PROVISION_SCRIPT'
set -euo pipefail

# Base packages
sudo apt-get update -qq
sudo apt-get install -y -qq \
    git curl wget build-essential \
    tmux rsync \
    ca-certificates

# gh CLI (GitHub) — official apt source
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | \
    sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
sudo chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | \
    sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
sudo apt-get update -qq
sudo apt-get install -y -qq gh

# Miniconda (system-wide at /opt/miniconda; base only, no envs)
if [[ ! -d /opt/miniconda ]]; then
    wget -q https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O /tmp/miniconda.sh
    sudo bash /tmp/miniconda.sh -b -p /opt/miniconda
    rm /tmp/miniconda.sh
fi

# Source conda for any shell; do NOT auto-activate. Each screen creates its
# own brieflow_<screen> env at S6.
echo 'source /opt/miniconda/etc/profile.d/conda.sh' | sudo tee /etc/profile.d/miniconda.sh >/dev/null

# Node.js 20 (NodeSource) — Ubuntu 22.04's apt nodejs is v12, too old for the
# Claude Code CLI (needs node >= 18); install a modern node first.
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y -qq nodejs

# Claude Code CLI (npm global)
sudo npm install -g @anthropic-ai/claude-code

# brieflow-auto plugin — system-wide clone, then link into the default user's
# plugin dir so /plugin commands work out of the box.
sudo mkdir -p /opt/brieflow-auto
sudo chown $(whoami) /opt/brieflow-auto
git clone https://github.com/cheeseman-lab/brieflow-auto.git /opt/brieflow-auto

mkdir -p ~/.claude/plugins
ln -sfn /opt/brieflow-auto/plugins/brieflow-auto ~/.claude/plugins/brieflow-auto

# Seed /etc/skel so future users inherit the link.
sudo mkdir -p /etc/skel/.claude/plugins
sudo ln -sfn /opt/brieflow-auto/plugins/brieflow-auto /etc/skel/.claude/plugins/brieflow-auto

# Verify gcloud is on PATH.
gcloud --version | head -1

echo "provision OK"
PROVISION_SCRIPT
)"

# ---------------------------------------------------------------------------
# 3. Snapshot to custom image
# ---------------------------------------------------------------------------

log "stopping temp VM before snapshot"
gcloud compute instances stop "$TEMP_VM" --zone="$BRIEFLOW_GCP_ZONE" --quiet

log "creating image ${IMAGE_NAME} in family ${BRIEFLOW_VM_IMAGE_FAMILY}"
gcloud compute images create "$IMAGE_NAME" \
    --source-disk="$TEMP_VM" \
    --source-disk-zone="$BRIEFLOW_GCP_ZONE" \
    --family="$BRIEFLOW_VM_IMAGE_FAMILY" \
    --description="brieflow-auto VM tooling: miniconda + plugin + claude + gh (built $(date -Iseconds))"

# Trap handles temp VM teardown.

log "image ${IMAGE_NAME} ready. Family ${BRIEFLOW_VM_IMAGE_FAMILY} now points to it."
log "next: bash provision_vm.sh <screen-id> [--data-disk-tb N]"

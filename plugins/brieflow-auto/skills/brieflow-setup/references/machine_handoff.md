# Machine handoff — switch a screen's VM class mid-run without recopying data

Each destination needs a new [verified connection record](connection_handoff.md) naming
its actual instance ID, zone, remote account and workspace. Reuse the verified Linux
identity where the identity policy permits, but do not assume equal usernames imply equal
UIDs or disk access. Verify after mounting the disk and before resuming. A mismatch requires
operator/administrator review, never an automatic recursive ownership change. Reconnect
Claude Code on the destination; the source workstation does not drive pipeline work over SSH.

Mid-run VM migration. Goal: move an in-flight screen between a GPU box and a
CPU box when the phase class changes, **without** recopying the multi-TB data
disk and **without** tripping snakemake into re-running completed phases.

brieflow's phases split by hardware, and the GPU is only needed in the
**middle**. **preprocess** is CPU-bound — no GPU path. **SBS + phenotype**
contain the cpsam/cellpose-SAM segmentation, a GPU-class transformer, so that
window wants a GPU VM (e.g. `n1-standard-64` + 8× V100). **merge / aggregate /
cluster** are CPU- and RAM-bound pandas work with no GPU path, and merge in
particular OOMs on the GPU box's ~240 GB — so they want a high-memory CPU VM
(e.g. `c3d-highmem-180`, ~1.4 TB RAM).

So the phase→hardware shape is **CPU → GPU → CPU**, and a screen crosses
machine class *twice*: hand CPU→GPU after preprocess, and GPU→CPU after
phenotype. Running the CPU-bound preprocess on an idle 8× V100 box just burns
GPU cost, so provision preprocess on the CPU box and spin the GPU box up only
for the segmentation window. The **same CPU box** can do both bracketing legs
(preprocess, then merge onward) — the disk just ping-pongs to the GPU box in
between.

This document captures the jannu handoffs (2026-07-06). The mechanics are
identical in both directions — you'll typically run both.

---

## The core rule: reattach the disk, do not copy it

The screen's whole working tree — `<screen>-analysis` repo, `screen.yaml`,
`.brieflow/interview.json`, `config_*.yml`, `brieflow_output/`, the raw data,
the `brieflow-auto` plugin clone — lives on one zonal pd-ssd data disk. Keep
both VMs in the **same zone** and the handoff is a disk detach + reattach
(seconds), not a transfer.

**Reattach is not just faster than rsync — it is safer, for a specific
reason.** A file copy (`rsync`, `gcloud storage cp`) rewrites destination
mtimes. brieflow's `flow.sh` invokes snakemake with `--rerun-triggers mtime`,
so if a completed upstream output (phenotype/sbs) lands with an mtime *newer*
than its downstream (merge), snakemake decides upstream is stale and drags the
entire completed phase back into the merge DAG — silently recomputing
thousands of tiles. A disk reattach preserves every mtime exactly, so the DAG
sees upstream as done. **Copying the data across is how you manufacture the
exact rerun cascade you're trying to avoid.**

If you ever *must* copy instead of reattach (e.g. cross-zone move), plan on a
post-copy mtime repair (`touch` upstream outputs newer than the copied inputs)
and a `--dry-run` gate that shows **zero** upstream jobs before you resume.

---

## Preconditions

1. **Both VMs in the same zone.** pd-ssd disks are zonal; a disk cannot attach
   to a VM in another zone. Crossing zones requires a snapshot + recreate
   (slow for multi-TB) — avoid by keeping the screen's VMs co-zoned.
2. **Source VM stopped, no live writers.** Detach only after the run is
   `stopped`/`complete` for the current phase. Detaching a disk with a live
   pipeline writing to it corrupts the snapshot-in-time.
3. **The destination VM already exists (ideally).** A stopped VM keeps its
   boot-disk state — the `brieflow_<screen>` conda env, `~/.brieflow_env`,
   gcloud/gh auth, the plugin install. Reusing a stopped CPU box means **no
   env rebuild** for the merge phase (torch/GPU are irrelevant to merge). If
   you must create fresh, run the normal brieflow-setup bring-up first.

---

## Handoff sequence

```bash
Z=us-central1-c
DATA=<screen>-data          # the multi-TB pd-ssd data disk
SRC=brieflow-<screen>-gpu   # VM the disk is leaving
DST=brieflow-<screen>-cpu   # VM the disk is joining

# 1. Detach from the (stopped) source VM. Verify it has no users after.
gcloud compute instances detach-disk $SRC --disk=$DATA --zone=$Z
gcloud compute disks describe $DATA --zone=$Z --format="value(users)"   # -> empty

# 2. Attach to the (stopped) destination VM with a stable device name.
gcloud compute instances attach-disk $DST --disk=$DATA --device-name=<screen>-data --zone=$Z

# 3. Start the destination VM.
gcloud compute instances start $DST --zone=$Z
```

Then mount the disk on arrival — its UUID is constant across VMs, but the new
VM's `/etc/fstab` will not have an entry:

```bash
# On the destination VM:
UUID=$(lsblk -o NAME,UUID,SIZE | grep <data-disk-size> | awk '{print $2}')
sudo mkdir -p /data
sudo mount UUID=$UUID /data
# Make it durable + boot-safe (nofail = a missing disk never blocks boot):
grep -q $UUID /etc/fstab || \
  echo "UUID=$UUID /data ext4 discard,defaults,nofail 0 2" | sudo tee -a /etc/fstab
df -h /data
```

Verify operator-ready before handing back to the run: `conda env list` shows
`brieflow_<screen>`, `~/.brieflow_env` present, `git -C $BRIEFLOW_DATA_ROOT/<screen>-analysis/brieflow describe`
and the plugin version match remote.

---

## Gotchas

### Big-SKU stockout — downsize instead of waiting or zone-hopping

The largest machine types stock out often: `c3d-highmem-360` in
`us-central1-c` returned `ZONE_RESOURCE_POOL_EXHAUSTED` on the jannu start.
You **cannot** hop zones (the disk is zone-pinned), so the fix is to shrink
the machine type in place — the VM must be `TERMINATED` to change it:

```bash
gcloud compute instances set-machine-type $DST --machine-type=c3d-highmem-180 --zone=$Z
gcloud compute instances start $DST --zone=$Z
```

Smaller highmem tiers are far more available *and* cheaper, and merge doesn't
saturate 360 cores anyway. Even `c3d-highmem-180` (1.4 TB RAM) is ~6× the
`n1-standard-64` GPU box's 240 GB — which is what OOM'd merge there in the
first place. Downsizing is usually the *right* call, not just the available
one.

### mtime scramble (see "the core rule")

The single most important gotcha, restated because it's easy to reintroduce:
never `rsync`/`cp` the data across when a reattach would do. If a handoff is
followed by a merge that suddenly wants to re-run phenotype, suspect scrambled
mtimes first, not a config problem. Confirm with a `--dry-run` — it should
show only merge/aggregate/cluster jobs, zero upstream.

### GPU box: kernel auto-upgrade breaks the NVIDIA driver on restart

Specific to *starting* a GPU VM that was stopped for a while: the DLVM base
auto-upgrades the kernel on boot (e.g. `6.5.0-1016-gcp` → `6.8.0-1060-gcp`),
but the pinned NVIDIA driver's DKMS module was built only for the old kernel
and won't compile against the new one — `nvidia-smi` fails after the restart.
Pin the known-good kernel in grub (a `99-*.cfg` snippet under
`/etc/default/grub.d/` wins over the DLVM's `50-cloudimg-settings.cfg`) and
`apt-mark hold` it. Not a concern for CPU destinations (no driver).

### A stopped VM is not a torn-down VM

Reusing a *stopped* destination VM keeps its env; a *deleted* one does not.
Don't delete the off-class VM until the screen fully completes — you may hand
back the other direction, and its boot-disk env is expensive to rebuild. Stop
it (compute → $0) between uses instead.

---

## When to hand off

| Boundary | From | To | Trigger |
|---|---|---|---|
| preprocess done | CPU highmem box | GPU box (V100, cpsam) | preprocess complete; SBS/phenotype segmentation is next |
| segmentation done | GPU box (V100, cpsam) | CPU highmem box | phenotype complete; merge/aggregate/cluster is next |

After the destination phases finish, **stop** the VM (compute → $0), same as
end-of-run — but only tear down (delete VM + disks) per `teardown_flow.md`
once the *whole* screen is complete and outputs are verified in GCS.

---

## Reference: handoff in one diagram

```
[CPU highmem VM: preprocess]
        │  hand off: detach disk → attach to GPU VM (same zone, both stopped)
        ▼
[GPU VM: sbs + phenotype segmentation on cpsam]
        │  hand off: detach disk → attach back to CPU VM (downsize if stockout)
        ▼
[same CPU highmem VM: merge + aggregate + cluster]
        │  stop when done (compute → $0); GPU VM kept stopped, not deleted
        ▼
[whole screen complete] ──> teardown_flow.md
```

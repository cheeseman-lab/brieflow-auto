# Teardown flow — preserve outputs then nuke the VM

End-of-screen teardown. Goal: get the on-VM analysis tree into durable GCS
storage, then destroy the expensive compute + persistent disks, keeping a
cheap option to respin the VM later.

This document captures lessons from real screen teardowns (snowdon,
2026-05-29). The provided `cloud/teardown_vm.sh` script is a starting point
but its assumed `/data/<screen>/brieflow_output/` layout doesn't match the
screen-repo layout (`$BRIEFLOW_DATA_ROOT/<screen>-analysis/analysis/...`)
used by recent screens. Prefer the manual procedure below for current
screens.

---

## Pre-teardown checklist

Run before any destructive action.

1. **All three repos pushed.** `brieflow-analysis` fork (or `<screen>-analysis`
   fork), the `brieflow` submodule, and any screen-specific branch on
   `brieflow-auto`. For each: `git status` clean (or only known
   diagnostic-file untracked), `git push` returned without error, branch
   tracking remote.
2. **Submodule pointer matches its remote HEAD.** Otherwise the screen repo
   references a commit that nobody else can fetch.
3. **`screen.yaml` + `config/` checked in.** These are load-bearing for
   future respin; not in `.gitignore` but easy to forget on a screen-specific
   fork.

## Output transfer strategy — **do not** use a single big rsync

For multi-TB output trees, **`gcloud storage rsync` on the whole root is
pathologically slow**: gcloud serially walks the source tree before any
transfers begin. Observed on snowdon: 20+ minutes with 32 worker processes
all sleeping while a single Python process walked `$BRIEFLOW_DATA_ROOT`
(~3.7M files). Zero bytes transferred during that window.

**Use parallel per-subtree rsync jobs.** Each one scans its own subtree
concurrently; wall-clock = max(per-subtree scan) instead of sum. snowdon's
final config: 10 parallel jobs, ~7 hours wall-clock for 15 TB
(the second-to-last job was slow because of a regex bug — see gotchas).

### Recommended layout for screen-repo trees

```
DEST_BASE=gs://${BRIEFLOW_BUCKET_OUTPUT}/<screen>/analysis

# 6 parallel phase jobs (per-phase rsync, each with explicit dest)
gcloud storage rsync -r --exclude="$CACHE_EXCLUDE" \
  $BRIEFLOW_DATA_ROOT/<screen>-analysis/analysis/brieflow_output/<phase> \
  $DEST_BASE/<screen>-analysis/analysis/brieflow_output/<phase> &
# (repeat for preprocess, sbs, phenotype, merge, aggregate, cluster)

# 1 logs job
gcloud storage rsync -r \
  $BRIEFLOW_DATA_ROOT/<screen>-analysis/analysis/logs \
  $DEST_BASE/<screen>-analysis/analysis/logs &

# 2 sibling repo jobs (brieflow-analysis canonical + brieflow-auto local clone)
gcloud storage rsync -r --exclude="$CACHE_EXCLUDE" $BRIEFLOW_DATA_ROOT/brieflow-analysis $DEST_BASE/brieflow-analysis &
gcloud storage rsync -r --exclude="$CACHE_EXCLUDE" $BRIEFLOW_DATA_ROOT/brieflow-auto $DEST_BASE/brieflow-auto &

# 1 catch-all rest job (with exclusions for the heavy subtrees)
gcloud storage rsync -r --exclude="$REST_EXCLUDE" \
  $BRIEFLOW_DATA_ROOT/<screen>-analysis $DEST_BASE/<screen>-analysis &

wait
```

Tune per-job parallelism via env vars (set once, inherited by children):

```bash
export CLOUDSDK_STORAGE_PROCESS_COUNT=16
export CLOUDSDK_STORAGE_THREAD_COUNT=4
```

Defaults on a 360-core VM otherwise spawn far too many workers; 16×4 per
job × 10 jobs = ~640 concurrent ops, which is well-matched to a same-region
us-central1 → us-central1 transfer and pd-ssd source read throughput.

---

## Gotchas

### `gcloud storage rsync --exclude` uses *relative* paths

The regex is matched against the path *relative to the rsync SRC*, not the
absolute filesystem path. **Absolute-style patterns silently fail.**

```python
# BROKEN — none of these match because the relative path has no leading slash
EXCLUDE='.*(/\.snakemake|/__pycache__|/analysis/brieflow_output)(/.*)?$'

# CORRECT
EXCLUDE='^(.*/)?(\.snakemake|__pycache__)(/.*)?$|^analysis/brieflow_output(/.*)?$'
```

The slash-prefixed cache patterns happen to match when the file lives one
level deep (e.g. `analysis/.snakemake/foo` does contain `/\.snakemake`),
but anything at the relative-path *root* (e.g. `analysis/brieflow_output/...`)
is not anchored by a `/` on its left side and won't match. The result:
exclusions partly work, partly fail, and you don't notice until you compare
what was uploaded.

Always validate the regex first with `--dry-run`:

```bash
gcloud storage rsync -r --dry-run --exclude="$YOUR_REGEX" SRC DEST | head -50
# Confirm intended skips don't appear in the would-copy list.
```

### `Copying ...` log lines drop under high parallelism

Don't use `grep -c "^Copying" job.log` as an authoritative file count. With
high `process_count` × `thread_count`, gcloud emits "Copying" lines on a
best-effort basis — observed on snowdon: a job logged 631K Copying lines
but `find SRC | wc -l` returned 655K source files, despite the rsync
completing successfully (`Average throughput: ...` summary printed, `rc=0`).
A subsequent re-rsync to that destination reported `0 copies needed`,
proving all 655K files were in fact uploaded. The Copying line stream isn't
guaranteed to flush every file.

**Authoritative verification:** re-run the same rsync. If it reports zero
copies (and `Average throughput` summary, no errors), the transfer was
complete. If it does copy files, those were genuinely missing.

```bash
# Verification pass — same command as the original push
gcloud storage rsync -r --exclude="$EXCLUDE" SRC DEST
# Look for "Copying" lines in the output. If none, you're synced.
```

### Don't run rsync against a live source

The rsync's diff phase assumes a stable source. If the pipeline is still
writing files mid-rsync, the destination ends up with a snapshot of an
ill-defined moment. For teardown rsync: pipeline must be `complete` or
`stopped`, no live writers to `brieflow_output/`.

---

## Storage class strategy

Default Standard ($0.020/GB-month in us-central1) is fine if you might
re-touch the data this month. For longer retention with the option to
respin, transition older objects to colder classes via bucket lifecycle:

```bash
gcloud storage buckets update gs://${BRIEFLOW_BUCKET_OUTPUT} \
  --lifecycle-file=<(cat <<EOF
{
  "rule": [
    {"action": {"type": "SetStorageClass", "storageClass": "COLDLINE"},
     "condition": {"age": 30}},
    {"action": {"type": "SetStorageClass", "storageClass": "ARCHIVE"},
     "condition": {"age": 90}}
  ]
}
EOF
)
```

| Class | $/GB-month us-central1 | Retrieval | Min retention |
|---|---|---|---|
| Standard | 0.020 | $0 | none |
| Nearline | 0.010 | $0.01/GB | 30 days |
| Coldline | 0.004 | $0.02/GB | 90 days |
| Archive | 0.0012 | $0.05/GB | 365 days |

Archive class for 15 TB outputs ≈ $18/month. **Files are still online with
ms-latency retrieval** — Archive is a billing tier, not a tape archive.

---

## VM/disk teardown sequence

After outputs are verified in GCS:

### 1. Snapshot the boot disk (so respin doesn't reinstall everything)

The 200 GB boot disk holds the installed `brieflow_<screen>` conda env,
gcloud config, ssh keys, and the brieflow-auto plugin install. Worth ~$1-3/mo
to preserve as an Archive-class snapshot. Without this, respin runs the
full brieflow-setup wizard from a fresh VM (~1-2 hours for conda env build).

```bash
gcloud compute snapshots create <screen>-boot-$(date +%Y%m%d) \
  --source-disk=<boot-disk-name> \
  --source-disk-zone=$BRIEFLOW_GCP_ZONE \
  --storage-location=us-central1 \
  --snapshot-type=ARCHIVE
```

**Note:** the `--snapshot-type=ARCHIVE` flag lives only on the newer
`gcloud compute snapshots create` subcommand, not on the legacy
`gcloud compute disks snapshot ...` form. Older gcloud SDKs may not
recognize `--snapshot-type=ARCHIVE` — drop the flag to fall back to
Standard-class snapshot (~$1.30/mo for 200 GB used vs ~$0.60/mo Archive
— trivial difference).

Get the boot disk name from `gcloud compute instances describe <vm>
--zone=<zone> --format='value(disks[0].source)'` (the trailing segment
after the last `/`).

**zsh note:** Interactive zsh does not strip `#` comments by default. If
you copy-paste a command block containing `# some comment` lines, zsh will
error with `command not found: #`. Either strip comments first or run
`setopt interactive_comments` in your shell.

### 2. Verify disk auto-delete flags

By default, disks created at instance-create time have `autoDelete=true`
and vanish with the VM. Manually-attached disks default to `false` and will
survive (continuing to bill at pd-ssd rates).

```bash
gcloud compute instances describe <vm-name> --zone=<zone> \
  --format='yaml(disks[].deviceName, disks[].autoDelete, disks[].source)'
```

If any disk shows `autoDelete: false` and you want it gone, either flip the
flag first or delete it explicitly after the VM:

```bash
gcloud compute instances set-disk-auto-delete <vm-name> \
  --zone=<zone> --disk=<disk-name> --auto-delete
```

### 3. Delete the VM

```bash
gcloud compute instances delete <vm-name> --zone=<zone>
```

This destroys: compute, boot disk (if autoDelete), data disk (if autoDelete),
and any auto-created network endpoints.

### 4. Delete any surviving disks

```bash
gcloud compute disks delete <data-disk> <boot-disk> --zone=<zone> --quiet
```

### 5. Release reserved IPs (if any)

```bash
gcloud compute addresses list --filter="region:us-central1"
gcloud compute addresses delete <addr-name> --region=us-central1
```

VPC, firewall rules, IAP TCP forwarding — usually keep these for the next
screen's bring-up.

---

## Cost expectations (snowdon-class VM, 35 TB pd-ssd data + 200 GB pd-ssd boot)

| State | Compute | Boot disk | Data disk | Snapshot | GCS Standard 15 TB | Total/mo |
|---|---|---|---|---|---|---|
| Running | $1-2K | $34 | $5,950 | — | — | ~$7K |
| Stopped, disks kept | $0 | $34 | $5,950 | — | — | ~$6K |
| Stopped, data-disk-only kept | $0 | $34 | — | — | — | $34 |
| **Nuked, snapshot+GCS** (recommended) | $0 | — | — | $1-3 | $300 | **~$303** |
| Nuked, snapshot+GCS-archive | $0 | — | — | $1-3 | $18 | **~$21** |
| Nuked, no preservation | $0 | — | — | — | — | $0 (no respin path) |

GCS storage of 15 TB at Archive class: ~$18/month, 365-day minimum, $750
one-time retrieval cost when you respin.

---

## Respin path

```bash
# 1. Recreate boot disk from snapshot
gcloud compute disks create <screen>-boot-new \
  --source-snapshot=<screen>-boot-<YYYYMMDD> \
  --zone=$BRIEFLOW_GCP_ZONE --type=pd-ssd --size=200GB

# 2. Create the VM using the restored boot disk + a fresh data disk
gcloud compute instances create brieflow-<screen>-<datestamp> \
  --zone=$BRIEFLOW_GCP_ZONE \
  --machine-type=<machine> \
  --disk=name=<screen>-boot-new,boot=yes \
  --create-disk=size=<TB>,type=pd-ssd,name=data,auto-delete=yes

# 3. SSH in, mount data disk to /data (fstab entry will likely need updating
#    for the new device path), then hydrate from GCS:
gcloud storage rsync -r \
  gs://${BRIEFLOW_BUCKET_OUTPUT}/<screen>/analysis/ \
  $BRIEFLOW_DATA_ROOT/

# 4. The runloop checkpoint state (.snakemake/, .brieflow/) was preserved
#    in the rsync, so the pipeline can resume mid-phase without re-running
#    discovery.
```

---

## Reference: lifecycle in one diagram

```
[active screen] ──run──> [outputs in /data, expensive disks]
                   │
                   │ teardown
                   ▼
[verified GCS outputs] + [boot-disk snapshot]
   │
   │ respin (optional)
   ▼
[new VM, new data disk, hydrated from GCS]
```

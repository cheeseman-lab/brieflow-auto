# `setup_state.json` schema (optional)

**Status: optional / advanced.** Use only when the wizard needs to be
re-entrant across HPC and VM sessions without operator intervention. For
human-driven one-shot setups, the plain-text S3.5 handoff (printed SSH command
+ slug + bucket URIs + sbatch jobid) is sufficient — skip this file entirely.

When used, the state file lives at
`gs://${BRIEFLOW_BUCKET_OUTPUT}/<screen>/setup_state.json`. Written by both
the HPC- and VM-side halves of the wizard. Read at the start of every
invocation to enable re-entrance.

## Schema (v1)

Remote login uses a separate local `connection.json` record (schema
`brieflow-connection/v1`), generated and verified by `scripts/brieflow_connection.py`.
Keep it with the setup notes even when this optional GCS state is not used. It records
transport, explicit remote user/workdir, GCP account/project/zone/instance ID where
applicable, verification status and observed identity/access. It contains no keys or tokens.
An optional `connection_record` string may point to its local filename in this v1 state.
Do not treat a machine name, setup stage or copied verification from another client as
proof that the current client can connect. See [connection_handoff.md](connection_handoff.md).

```json
{
  "schema_version": 1,
  "screen": "damavand",
  "compute_target": "cloud-gcp",
  "operator_email": "<your-email>",
  "repo_url": "https://github.com/<org>/<screen>-analysis.git",

  "source": {
    "sbs_dir": "/path/to/raw/<screen>/sbs/",
    "phenotype_dir": "/path/to/raw/<screen>/phenotype/",
    "expected_size_tb": 3.2
  },

  "gcs": {
    "input_bucket": "<your-input-bucket>",
    "brieflow_bucket": "<your-brieflow-bucket>",
    "sbs_uri": "gs://<your-input-bucket>/damavand/sbs/",
    "phenotype_uri": "gs://<your-input-bucket>/damavand/phenotype/"
  },

  "vm": {
    "name": "brieflow-damavand-20260520",
    "zone": "us-central1-a",
    "data_disk_tb": 5,
    "data_mount": "/data/damavand/"
  },

  "transfer": {
    "slurm_jobid": 12345,
    "submitted_at": "2026-05-20T14:30:00Z"
  },

  "stage": "S3-complete",
  "history": [
    {"stage": "S1", "host": "hpc", "ts": "2026-05-20T14:00:00Z"},
    {"stage": "S2", "host": "hpc", "ts": "2026-05-20T14:15:00Z"},
    {"stage": "S3", "host": "hpc", "ts": "2026-05-20T14:30:00Z"}
  ]
}
```

## Field reference

### Top-level

| Field | Type | Set at | Purpose |
|---|---|---|---|
| `schema_version` | int | S1 | Bumped on incompatible schema changes; wizard refuses unknown versions. |
| `screen` | string | S1 | Slug, used in all path conventions (`<screen>-analysis`, `gs://input/<screen>/`, `/data/<screen>/`). |
| `compute_target` | enum | S1 | `cloud-gcp` (the only target that writes this file). |
| `operator_email` | string | S1 | Slurm `--mail-user`; future hook for non-Slurm notifications. |
| `repo_url` | string | S1 | Operator-pre-created analysis repo. Wizard `git clone`s at S6. |

### `source`

Original on-prem locations. Preserved for provenance and re-transfer.

| Field | Type |
|---|---|
| `sbs_dir` | string (abs path on HPC) |
| `phenotype_dir` | string (abs path on HPC) |
| `expected_size_tb` | number (operator estimate; drives data disk sizing) |

### `gcs`

Staging bucket coordinates. Derived deterministically from `screen` + the
operator's `BRIEFLOW_BUCKET_INPUT` + `BRIEFLOW_BUCKET_OUTPUT` env vars
at S3.

| Field | Type |
|---|---|
| `input_bucket` | string — value of `BRIEFLOW_BUCKET_INPUT` |
| `brieflow_bucket` | string — value of `BRIEFLOW_BUCKET_OUTPUT` |
| `sbs_uri` | string (full `gs://…/sbs/` URI) |
| `phenotype_uri` | string |

### `vm`

VM coordinates. Written at S2.

| Field | Type |
|---|---|
| `name` | string (`brieflow-<screen>-<YYYYMMDD>`) |
| `zone` | string (`us-central1-a` etc.) |
| `data_disk_tb` | int (`ceil(3 × expected_size_tb)` — input + output both resident) |
| `data_mount` | string (`/data/<screen>/`) |

### `transfer`

HPC→GCS Slurm transfer. Written at S3.

| Field | Type |
|---|---|
| `slurm_jobid` | int |
| `submitted_at` | ISO 8601 UTC timestamp |

### `stage` + `history`

| Field | Type | Purpose |
|---|---|---|
| `stage` | string (`S1`..`S8` or `S<N>-complete`) | Single source of truth for current state. |
| `history` | array of `{stage, host, ts}` | Audit trail. Append-only. |

## Write semantics

- **Atomic.** `write_setup_state` (in `cloud/_common.sh`) validates JSON, then
  uploads via `gcloud storage cp`. GCS object writes are atomic per object.
- **Append history.** Each stage transition appends an entry. Don't overwrite
  history.
- **One file per screen.** Don't shard or split.

## Read semantics

- **Re-entrant.** Every `brieflow-setup` invocation re-reads. No caching across
  invocations.
- **Schema check.** Refuse unknown `schema_version` values; bump version on
  any incompatible change.
- **Missing file = S0.** If the file doesn't exist, the screen hasn't started
  setup yet; wizard starts at S1.

## Manual recovery

If state is corrupt or you want to start over:

```bash
gcloud storage rm gs://${BRIEFLOW_BUCKET_OUTPUT}/<screen>/setup_state.json
```

Combined with `cloud/teardown_vm.sh <screen>` to drop the VM and
`gcloud compute disks delete <disk> --zone=<zone>` to drop the data disk,
this fully resets a screen's setup.

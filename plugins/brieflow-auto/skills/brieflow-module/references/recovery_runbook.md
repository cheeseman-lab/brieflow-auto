# Recovery Runbook (human-readable)

The recovery pattern that `scripts/brieflow_recover.py` automates. This
document describes the procedure as a human would follow it. The script and
this document should agree — if they diverge, the script is the source of
truth (it has been tested against real failure logs).

## Inputs

- An analysis dir with `.snakemake/log/`, `slurm/slurm_output/rule/`,
  `logs/efficiency_*/` populated by recent runs.
- The flow.sh aggregate log path (optional but recommended; provides the
  authoritative `Complete log(s):` link to the right snakemake master log).
- The list of modules in scope, in order. Last one is the terminal module.

## Step 1: Success check

Before assuming failure, search every snakemake master log under
`.snakemake/log/*.snakemake.log` for the marker:
```
Finished jobid: 0 (Rule: all_<terminal_module>)
```

If found: the run succeeded at some point. No action needed. Common with
a relaunch, where the marker is in an older log file from a prior attempt.

## Step 2: Locate the master log

If a flow.sh log was provided, parse it for `Complete log(s):` lines and use
the most recent one referenced. Otherwise take the most-recently-modified
`.snakemake/log/*.snakemake.log`.

## Step 3: Parse rule errors

Look for `Error in rule X:` blocks in the master log. Extract:
- `rule` name
- `jobid:` (snakemake jobid)
- `output:` (the first failing output filename — used to identify the right
  per-rule slurm log if the master log doesn't already carry it)
- `log:` (the slurm log path — most authoritative, but not always present)

Also extract `Job <snakemake_jobid> has been submitted with SLURM jobid
<slurm_jobid>` lines elsewhere in the master log. These map snakemake job IDs
to slurm job IDs.

## Step 4: For each errored rule

### 4a. Find the correct per-rule slurm log

Priority order:

1. The `log:` path from the Error block (master log records this for plain
   slurm jobs). This points at the SPECIFIC failing attempt.
2. Filename grep search: `slurm/slurm_output/rule/rule_<X>/.../<jobid>.log`
   that contains the failing output filename in its content.
3. Most-recent-mtime fallback (DANGEROUS — picks up successful retries; only
   use when 1 and 2 both fail).

### 4b. sacct lookup

If we have the slurm jobid (either from the master log or extracted from the
log filename `<jobid>.log`):
```bash
sacct -j <jobid> --format=JobID,State,ExitCode,MaxRSS,ReqMem,Elapsed --parsable2
```

Aggregate across the parent + .batch + .0 step rows. State priority:
`OUT_OF_MEMORY > FAILED > TIMEOUT > NODE_FAIL > anything else`. MaxRSS is the
maximum across all steps.

### 4c. Classify

Apply the failure taxonomy from `troubleshooting.md`:

- **recoverable_oom**: sacct State=OUT_OF_MEMORY OR ExitCode=137 OR slurm log
  contains `oom_kill event` / `OOMKilled` OR State=FAILED with MaxRSS near ReqMem.
- **config_mismatch**: log contains `ValueError("Unknown ...")` OR
  `KeyError: 'X'` from script execution.
- **nfs_latency**: log contains `MissingOutputException`.
- **recoverable_fd_limit**: log contains `Too many open files`.
- **unknown**: anything else.

## Step 5: Decide the action

### Single rule, recoverable_oom

Compute new_mem:
- observed_peak < 95% of cap: `new_mem = ceil(observed_peak × 4)` to nearest 1000 MB
- observed_peak ≥ 95% of cap: `new_mem = ceil(cap × 4)` to nearest 1000 MB
- Floor: `new_mem ≥ ceil(prev_rec × 1.5)` to never go backward

Cap at `MAX_REASONABLE_MEM_MB` (`scripts/brieflow_recover.py`). Above it, escalate
(cluster IT or rule rewrite). On `--backend local` an OOM escalates instead —
lower `--cores`.

Write to `mem_recommendations.json`:
```json
{
  "<rule>": {
    "WARNING": "manual_override_after_OOM_<date>_actual_peak_may_be_higher",
    "mem_mb_recommended": <new_mem>,
    "obs_cap_at_peak_mb": <cap>,
    "obs_n": <count + 1>,
    "obs_peak_rss_mb": <observed_peak>,
    "obs_source": "manual_override_after_OOM_<date>",
    "overhead": "explicit_post_OOM_bump",
    "tier": "<tile|well>"
  }
}
```

`obs_source` MUST start with `manual_override` so that
`brieflow_aggregate_efficiency.py` preserves this entry on subsequent
refreshes (otherwise the aggregator wipes it).

Action: `resume` — caller unlocks snakemake and relaunches (completed outputs are kept).

### Multiple rules, recoverable_oom

Every rule that OOM'd in the attempt is bumped once, using its worst
cap-at-peak.

### nfs_latency

Action: `resume` with `subaction=bump_latency_wait`. Caller bumps
`--latency-wait` per `LATENCY_BUMP_LADDER` and relaunches.

### recoverable_fd_limit

Action: `resume` with `subaction=raise_nofile`. `brieflow_run.py` raises the
open-file limit to the hard cap on relaunch.

### config_mismatch

Action: `escalate`. The fix is in the user's config; running
`/brieflow-preflight` will identify it. Auto-recovery is not appropriate
here — config edits should be deliberate.

### unknown

Action: `escalate`, set `should_file_issue=true`. After `--max-attempts` is
exhausted on unknown failures, this becomes the auto-issue path.

### Max attempts reached

After `attempt >= max_attempts`, action becomes `escalate` regardless of
classification. This caps cluster-time waste on bad recoveries.

## Step 6: Apply the action

The `brieflow_recover.py` script DOES NOT relaunch — it only emits the action
JSON. The caller (`/brieflow-module` in auto-recover mode) reads the JSON and
applies it:

- `success` → exit cleanly.
- `resume` → unlock snakemake, relaunch `brieflow_run.py`.
- `escalate` → `brieflow_notify.sh` with the reason, exit nonzero,
  optionally suggest `/brieflow-issue` if `should_file_issue`.

This separation lets the recover script be tested deterministically (give it
logs, check the action) without needing a live cluster.

## Manual application (when running this by hand)

If you're doing recovery without the auto-loop:

```bash
# 0. Resolve $SCRIPTS_DIR once (printed as "Base directory for this skill:"
#    at invocation):
SCRIPTS_DIR=$(realpath "<skill-base-dir>/../../scripts")

# 1. Run the classifier
$SCRIPTS_DIR/brieflow_recover.py \
  --analysis-dir analysis/ \
  --modules preprocess sbs phenotype \
  --attempt 1 --max-attempts 3

# 2. Read the action JSON. If action=resume:
#    mem_recommendations.json has been updated. Relaunch:
cd analysis/
snakemake --unlock --snakefile ../brieflow/workflow/Snakefile \
  --configfile config/config.yml
$SCRIPTS_DIR/brieflow_run.py \
  --analysis-dir analysis/ \
  --modules preprocess sbs phenotype

# 3. If action=escalate, read diagnostics and decide:
#    - config_mismatch: run preflight, fix config, retry
#    - unknown + should_file_issue: file via /brieflow-issue
#    - unknown without strong signal: read more carefully, may need
#      manual investigation
```

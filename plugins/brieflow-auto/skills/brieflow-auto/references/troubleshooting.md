# Brieflow Troubleshooting Reference

## Table of Contents
1. [Failure classification taxonomy](#failure-classification-taxonomy)
2. [Diagnostic discipline](#diagnostic-discipline)
3. [Installation Issues](#installation-issues)
4. [Submodule Issues](#submodule-issues)
5. [Snakemake Issues](#snakemake-issues)
6. [Slurm Issues](#slurm-issues)
7. [Data/IO Issues](#dataio-issues)
8. [GCS/Cloud Issues](#gcscloud-issues)
9. [Notebook Issues](#notebook-issues)
10. [Slurm Array Job Issues (snakemake-executor-plugin-slurm 2.6.0+)](#slurm-array-job-issues-snakemake-executor-plugin-slurm-260)

---

## Failure classification taxonomy

The auto-recover loop in `/brieflow-module` (powered by
`scripts/brieflow_recover.py`) classifies pipeline failures into five classes
(`classify_failure` in `scripts/brieflow_classify.py` holds the exact signatures),
each with a defined recovery action.

### `recoverable_oom`
**Detection signatures (any of)**:
- `sacct State=OUT_OF_MEMORY` for the failing slurm jobid
- `sacct ExitCode=137:0` (137 = SIGKILL, slurm's OOM signal)
- per-rule slurm log contains `slurmstepd: error: Detected N oom_kill event` or `OOMKilled`
- sacct `State=FAILED` with MaxRSS near the requested memory

**Recovery action**: bump that rule's `mem_mb_recommended` in
`mem_recommendations.json`, write a manual override entry (preserved on
`/brieflow-tune-memory` refresh), unlock snakemake, relaunch (completed outputs
are kept). On `--backend local` an OOM escalates instead — lower `--cores`.

**Bump heuristic**:
- If observed peak < 95% of cap: `new_mem = ceil(observed_peak × 4)` (ceiling at
  4× cap to handle peak-at-cap cases conservatively).
- If observed peak ≥ 95% of cap (peak-at-cap, true peak unknown):
  `new_mem = ceil(cap × 4)`.
- Never less than 1.5× the previous recommendation.
- Cap at `MAX_REASONABLE_MEM_MB` (`scripts/brieflow_recover.py`). Beyond that, escalate.

**Real example (2026-04-30 → 2026-05-01)**:
`merge_phenotype` OOM'd at the 3 GB default → 32 GB cap (peak-at-cap) → 128 GB
(succeeded at 83 GB peak). The recover loop applied: 3 → 12 GB rec, OOM →
32 GB rec (4× cap), OOM → 128 GB rec (4× cap).

### `nfs_latency`
**Detection signatures**:
- `MissingOutputException` in master log

**Recovery action**: bump `--latency-wait` per `LATENCY_BUMP_LADDER`
(`scripts/brieflow_recover.py`), unlock, relaunch.

### `recoverable_fd_limit`
**Detection signatures**: `Too many open files` (`OSError: [Errno 24]`) in the
logs.

**Recovery action**: resume with `subaction=raise_nofile`; `scripts/brieflow_run.py`
raises the open-file limit to the hard cap on relaunch.

### `config_mismatch`
**Detection signatures**:
- per-rule log traceback contains `ValueError("Unknown <key>: ... Choose ...")`
- per-rule log traceback contains `KeyError: '<config_key>'` originating from
  the script

**Recovery action**: escalate. The user must run `/brieflow-preflight` and
fix the config; this is not a memory issue and won't go away on retry. Should
have been caught by the preflight gate, so this firing means either the gate
was skipped or a new accepted-value pattern was introduced upstream.

### `unknown`
**Detection**: anything that doesn't match the above signatures.

**Recovery action**: escalate. After `--max-attempts` is reached for the same
unknown failure, the recover loop's output JSON sets `should_file_issue=true`
— a lead, not proof of a bug, for `/brieflow-issue`, which investigates and opens
the report against `cheeseman-lab/brieflow`.

---

## Diagnostic discipline

When investigating a failure manually (i.e. without `brieflow_recover.py`),
follow the same discipline the recover script does:

1. **Read the per-rule slurm log, not the parent's WorkflowError summary.**
   The master log says `Error in rule X: jobid: N log: <path>` — open that
   path. Do NOT pick the most-recent log for rule X by mtime; that often
   points at a later successful retry of the same rule, not the failing one.
   The master log's `log:` line is authoritative.

2. **Run sacct on that exact slurm jobid.**
   ```bash
   sacct -j <slurm_jobid> --format=JobID,State,ExitCode,MaxRSS,ReqMem,Elapsed --parsable2
   ```
   `State=OUT_OF_MEMORY` is the ground-truth OOM signal.

3. **Verify "missing" outputs are not actually on disk.**
   `MissingOutputException` at well/full scale is often an NFS visibility
   race, not a real failure. `ls` on the file path from a login node before
   trusting the exception.

4. **Check the snakemake master log for `Job <X> has been submitted with SLURM
   jobid <Y>`** lines to map snakemake jobids to slurm jobids. The plugin's
   recover script uses these directly.

The recover script encodes this discipline.

---

## Installation Issues

### Conda environment creation fails
```
# Remove and recreate
conda deactivate
conda env remove -n brieflow_SCREEN_NAME
conda create -n brieflow_SCREEN_NAME -c conda-forge python=3.11 uv pip -y
```

### uv pip install fails with dependency conflicts
```
# Try installing with pip directly as fallback
pip install -r pyproject.toml
pip install -e .
```

### micro-sam installation fails
micro-sam requires specific PyTorch versions. If conda install fails:
```
# Install PyTorch first
conda install pytorch torchvision -c pytorch -c conda-forge
conda install -c conda-forge micro_sam -y
```

### Python version mismatch
Brieflow requires Python 3.11. Check with:
```
python --version
# If wrong, recreate env with explicit version
conda create -n brieflow_SCREEN_NAME -c conda-forge python=3.11 uv pip -y
```

---

## Submodule Issues

### Brieflow submodule not initialized
```
git submodule update --init --recursive
```

### Submodule pointing to wrong branch
```
cd brieflow
git fetch origin
git checkout main
git pull
cd ..
git add brieflow
git commit -m "Update brieflow to main"
```

### Can't push submodule changes
You need push access to the brieflow fork. Check remotes:
```
cd brieflow
git remote -v
# origin should point to YOUR fork, upstream to cheeseman-lab/brieflow
```

### Submodule shows detached HEAD
This is normal for submodules. If you need to make changes:
```
cd brieflow
git checkout main
# make changes, commit, push to your fork
```

---

## Snakemake Issues

### "Directory cannot be locked" / Lock error
```
cd analysis
snakemake --unlock
# Then re-run your pipeline
```

### "Missing input files" error
Check that Step 0 (configure_preprocess_params) was run correctly and paths are valid:
```
ls -la /path/to/nd2/data/
# Verify the sample dataframes point to existing files
```

### DAG takes very long to build
For large screens (10k+ jobs), DAG building can be slow. Workarounds:
- Use `--batch` flag to process subsets
- Use `--notemp` to avoid temp file checks
- For GCS: ensure bulk inventory caching is enabled (see GCS section)

### Snakemake hangs without error
Check for zombie jobs:
```
squeue -u $USER
# Cancel stuck jobs
scancel JOB_ID
# Unlock and retry
snakemake --unlock
```

### "No rules to produce" target
Usually means the config notebooks weren't executed or output paths are wrong.
Re-run the relevant config notebook and verify output paths.

---

## Slurm Issues

### Jobs immediately fail
Check Slurm output logs:
```
ls slurm_logs/
cat slurm_logs/slurm-JOBID.out
```

Common causes:
- Conda env not found → add `conda activate brieflow_SCREEN_NAME` to sbatch script
- Module not loaded → check if your cluster needs `module load` commands
- Memory request exceeds partition limit → reduce memory in config

### OOM (Out of Memory) killed
```
# Check how much memory was actually used
sacct -j JOBID --format=MaxRSS,ReqMem
# Increase in slurm/config.yaml and re-run
```

### Timeout errors
```
# Check actual runtime
sacct -j JOBID --format=Elapsed,Timelimit
# Increase wall time in slurm/config.yaml
```

### Partition not available
```
# Check available partitions
sinfo
# Update partition in slurm/config.yaml
```

---

## Data/IO Issues

### ND2 files not found
```
# Verify path exists and is accessible
ls -la /path/to/nd2/data/
# Check file permissions
stat /path/to/nd2/data/PLATE_NAME/
```

### Disk space full
```
df -h .
du -sh analysis/brieflow_output/*/
# Clean intermediate files if safe
# WARNING: only clean files you can regenerate!
```

### Corrupted output files
```
# Remove the specific corrupt output and re-run that rule
rm analysis/brieflow_output/MODULE/CORRUPT_FILE
# Snakemake will regenerate only what's missing
```

---

## GCS/Cloud Issues

### GCS storage plugin slow (DAG builds taking 30+ minutes)
This is a known issue with the Snakemake GCS storage plugin for large screens.
Workaround: use bulk inventory caching:
```python
# In your Snakefile or config, enable inventory caching
# This has shown 40-90x speedup for DAG builds
```

### GCS authentication errors
```
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID
```

### Google Batch job submission failures
```
# Check quotas
gcloud compute regions describe REGION --format="value(quotas)"
# Verify service account permissions
```

---

## Notebook Issues

### Notebook kernel not found
```
# Register the conda env as a Jupyter kernel
conda activate brieflow_SCREEN_NAME
python -m ipykernel install --user --name brieflow_SCREEN_NAME
```

### Papermill execution fails
```
# Run with verbose output to see which cell fails
papermill input.ipynb output.ipynb --log-output --log-level DEBUG
```

### Notebook shows stale results
Always re-run from top (Kernel → Restart & Run All) when checking config notebooks.
Notebooks maintain state between cells — partial re-runs can show inconsistent results.

### Jupyter on Slurm (for interactive work)
```
# Use the provided sbatch script
sbatch jupyteronslurm.sbatch
# Check output for the SSH tunnel command and Jupyter URL
cat slurm_logs/jupyter-*.out
```

---

## Slurm Array Job Issues (snakemake-executor-plugin-slurm 2.6.0+)

### Snakemake hangs at "Select jobs to execute..." between rule phases

**Cause**: Snakemake's `update_input_sizes()` fires async `stat()` calls for all input files with no timeout. On NFS, this can hang indefinitely at phase transitions (e.g., after convert_sbs completes and extract_metadata_sbs is ready).

**Fix**: Kill snakemake (`kill -9 PID`), unlock, and relaunch. Usually resolves on second attempt as NFS cache is warm.
```bash
kill -9 <snakemake-pid>
snakemake --unlock --snakefile brieflow/workflow/Snakefile --configfile config/config.yml
# re-run your flow.sh command
```

### Array job deadlock with --slurm-array-limit
Keep `--slurm-array-limit` ≤ 10. Higher values with 6+ active rules can deadlock.

### "Only the first wildcard combination will be used" warning
Cosmetic warning when an array job has mixed wildcard values. Jobs still run correctly.

---
name: brieflow-status
description: Use to check the current state of a brieflow pipeline run — active jobs, snakemake progress, recent failures, completed phases. Read-only. Triggered by "check status", "what's running", "is the pipeline done", "any failed jobs", etc.
---

# Brieflow — Pipeline status

Read-only status check for a brieflow OPS pipeline run. Never modifies state.

## Inputs

- Current directory or `<analysis-dir>` if explicitly provided. Auto-detects
  screen context: looks for `screen.yaml`, `brieflow/` submodule, or
  `config/config.yml` in cwd and parents. Derives screen name and conda env
  (`brieflow_<screen>`).

## Output

A compact 5-line status report:

```
Screen: <screen-id> | Env: brieflow_<screen-id>
Completed: <phases that have output dirs>
Running:   <active phase + N/total + ~ETA from finished/total × elapsed>
Failed:    <recent failed jobs, or "none">
Notes:     <any relevant findings — stale lockfiles, NFS lag, etc.>
```

## Steps

1. **Detect screen repo.** Look for `screen.yaml`, `brieflow/` submodule, or
   `config/config.yml` in cwd + parents.
2. **Active jobs.** `squeue -u $USER` → filter to brieflow rule names.
3. **Snakemake progress.** Tail the most recent log under
   `analysis/.snakemake/log/*.log` for step count + active rule names.
4. **Recent failures.** Query from the current launch, not from midnight: an overnight
   run's failures before 00:00 fall outside `sacct --starttime=today`. OOM-killed jobs end in
   `OUT_OF_MEMORY`, not `FAILED`, so list every failure state:
   ```bash
   START=$(python <scripts-dir>/brieflow_manifest.py --analysis-dir <analysis> --sacct-start)
   sacct -u $USER --state=FAILED,OUT_OF_MEMORY,TIMEOUT,NODE_FAIL --starttime=$START \
     --format=JobName%40,State,Elapsed,MaxRSS,ReqMem,ExitCode -n 2>/dev/null | head -20
   ```
   `--sacct-start` prints the local time of `.brieflow/run_manifest.json`'s `written_at`
   (the last 48 h when there is no manifest).
5. **Completed phase inference.** Check
   `analysis/brieflow_output/{preprocess,sbs,phenotype,merge,aggregate,cluster}/`
   for non-empty content per phase.
6. **Present** the 5-line summary. If no screen repo: say so and ask the
   operator to navigate to their screen repo.

For a runner launch, `scripts/brieflow_watch.py --analysis-dir <analysis>` gives the same
picture in one call (state, new runner events, this launch's failed jobs) and is the watcher to
run on an interval; see `brieflow-run`.

## Notes

- `output_path` discovered from `config/config.yml` (key: `output_path`),
  defaulting to `analysis/brieflow_output/`.
- ETA = `(finished / total) × elapsed_wall_time` (best effort; rough).
- If snakemake is not running and output dirs are incomplete, say
  "No active run detected — some steps appear incomplete."
- Never modify state. This is a read-only skill.

## When NOT to use this skill

- If the operator says the pipeline failed and wants to fix it — that's
  `brieflow-module` (auto-recover mode) or `brieflow-issue` (file a bug).
- If the operator wants memory thresholds tuned — that's `brieflow-tune-memory`.

## Exit

Return to the router skill (`brieflow-auto`) when done.

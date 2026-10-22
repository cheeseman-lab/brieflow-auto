# Execution traces and agent rollouts

Every end-to-end runner invocation creates a unique `.brieflow/traces/<id>/` directory,
including early pauses and exceptions. It keeps before/after copies of the manifest,
interview, decisions, findings, progress and escalation record; `runner.log` contains
Python runner stdout/stderr. `trace.json` records the exit status, indexes existing
pipeline logs and, inside a Claude Code session, the agent session ID; an exception also
records its message and a traceback tail. Subprocess output is not universally intercepted,
and indexed logs must be retained separately. A run stopped by closing its console pipe or
by Ctrl-C ends as `interrupted`. Abrupt process death can leave a `running` record without an end
snapshot. This is evidence of an interrupted capture, not success.

For automatic **Claude Code transcripts**, `brieflow-setup` turns recording on for a new
screen after one disclosure that conversation/tool records can contain private inputs and
credentials. Enable recording in the intended session's working directory only, and name the
screen repo so its ignore rules are added:

```bash
python scripts/brieflow_rollout_hook.py --enable-workspace /path/to/session-workspace \
  --ignore-repo /path/to/screen-repo
```

With the plugin loaded, its Stop hook receives the runtime's transcript path and session
ID and copies a content-addressed snapshot after each completed turn. The working directory
must match the enabled workspace. The hook does nothing elsewhere. Snapshots live in
`.brieflow/rollouts/<session-hash>/`; unchanged transcripts are deduplicated. They contain
what the runtime exported, not guaranteed complete reasoning, failed-turn records, or
subagent transcripts. Set `enabled` to false in `.brieflow/record_rollouts.json` to stop.
Existing captures are retained; no automatic deletion or upload is performed. Enabling adds
`**/.brieflow/rollouts/` and `**/.brieflow/traces/` to the `.gitignore` of each named repo and
of the repo enclosing the workspace, if any. Directory permissions restrict access, but records are not sanitized.

For another runtime or an explicit export, attach the supplied file to a runner trace:

```bash
python scripts/brieflow_trace.py --trace-dir /path/to/trace --source /path/to/export.jsonl --runtime runtime-name --session-id session-id
```

Imports are bounded to 100 MB per file and record a hash, runtime, session ID and import
time. This does not discover missing historical rollouts. Improvement collection
(`brieflow_improve.py collect`) finds traces, captured rollouts and `*_LOG.md` operator logs
next to each `--analysis-dir` by itself; `--trace-dir` and `--rollout` add other selections.
Sources over the per-source limit are chunked (transcripts, ledgers, records) or excerpted
(pipeline logs), and anything not retained is listed with a reason. Never publish raw
records in an issue, release, paper supplement or model request without a disclosure review.

The hook interface follows https://code.claude.com/docs/en/hooks. It is tested using
synthetic runtime payloads; installation into an active Claude Code session is a separate
smoke test. No running session is modified by the release preparation.

# brieflow-auto

Public Claude Code marketplace for **brieflow-auto** — active pipeline management
for [brieflow](https://github.com/cheeseman-lab/brieflow) optical pooled screens.

## Connecting to the execution machine

Setup can begin on a laptop or cluster login host, but Claude Code, its shell and notebook
tools, and the Brieflow runner run on the execution machine. Remote setup records an explicit
connection command and verifies the remote Linux identity and analysis-directory access;
it never infers that identity from your laptop username or cloud email. The handoff includes
reconnecting to the same remote tmux session. See the
[connection guide](plugins/brieflow-auto/skills/brieflow-setup/references/connection_handoff.md)
for GCP and existing SSH hosts. This is remote access, not a remote pipeline dispatcher.

## Local records and harness development

Runner invocations retain execution traces. `/brieflow-setup` turns on Claude Code transcript
capture for the screen's session workspace after one privacy disclosure; it never scans
unrelated sessions or uploads records. See [trace recording](plugins/brieflow-auto/scripts/TRACE_RECORDS.md)
for setup, privacy, capture limits, and other-runtime imports. `/brieflow-improve` collects the
local evidence it finds next to an analysis dir (traces, transcripts, findings, operator logs)
to propose and test a bounded candidate for maintainer review. It does not update
the live plugin or pipeline automatically, and its effect on screen outcomes is untested.

## Reusing the pattern

The plugin drives a microscopy pipeline, but its control structure is
deliberately generic, so you can build a similar harness for another
long-running, multi-stage task. The end-to-end run is one per-stage primitive —
parametrize → validate → run-with-monitor/recover → gate — applied in a loop
over a plain list of stages. **The runner** is that same loop with the operator
decisions resolved up front, pausing only on a real blocker.

The engine is science-agnostic but stack-specific (marimo + Snakemake +
slurm/local). Porting it means re-implementing the stack-bound pieces and
keeping the principles: stages-as-data, one uniform per-stage primitive, a
failure taxonomy instead of blind retry, resolve-up-front-then-run-unattended,
and self-describing artifacts. [`HARNESS.md`](./HARNESS.md) walks those
principles, the state files, the agent boundary and a porting checklist.

## Install (from git)

In Claude Code:

```
/plugin marketplace add cheeseman-lab/brieflow-auto
/plugin install brieflow-auto@brieflow-auto
/reload-plugins
```

After `/reload-plugins`, the status line should show `1 plugin · 11 skills · 10 commands` added.

The install id is `brieflow-auto@brieflow-auto` — `<plugin>@<marketplace>`.
Both happen to share the same name.

## Update

```
/plugin update brieflow-auto@brieflow-auto
/reload-plugins
```

## Install (from a local tarball)

If you've been handed a `brieflow-auto-plugin.tgz` (e.g. offline install, VM
bootstrap, or pre-release testing), use the headless installer that ships in
the same tarball drop:

```bash
bash install_plugin.sh --tarball ~/brieflow-auto-plugin.tgz
```

The script extracts the tarball into `~/.claude/plugins/marketplaces/brieflow-auto/`,
synthesizes a `marketplace.json` wrapper, populates the plugin cache, and
registers entries in `installed_plugins.json` + `known_marketplaces.json`. Open
`claude` (or run `/reload-plugins` in an existing session) and the plugin is
live.

## Install (from any local marketplace directory)

```bash
bash install_plugin.sh --dir /path/to/checkout
```

## Uninstall

```
/plugin uninstall brieflow-auto
/plugin marketplace remove brieflow-auto
```

## What's in the plugin

See [`CLAUDE.md`](./CLAUDE.md) for plugin internals (commands, architecture,
auto-recover loop, preflight gate). Briefly:

| Command | Purpose |
|---|---|
| `/brieflow-setup` | First-time screen onboarding — host + raw data + analysis repo + conda env, stage the operator's `screen.yaml` |
| `/brieflow-notebook` | Configure + run one phase notebook in a live marimo kernel; sweep parameters; export HTML |
| `/brieflow-status` | Read-only run status — active jobs, snakemake progress, recent failures |
| `/brieflow-preflight` | Statically validate `config/config.yml` against the brieflow rule code before submit |
| `/brieflow-module` | Launch a pipeline run — single-shot (tmux) or auto-recover (classifies failures, bumps mem on OOM, resumes) |
| `/brieflow-run` | Run a screen through the per-phase cycle over any range of phases — whole screen, `--only` one, or `--from`/`--through` — pausing only on a classified blocker |
| `/brieflow-qc` | Consolidate + gate a phase's QC from its `eval/` outputs; verdict + optional HTML report |
| `/brieflow-tune-memory` | Refresh `slurm/mem_recommendations.json` from efficiency CSVs (preserves manual overrides) |
| `/brieflow-issue` | Automatically open a Brieflow defect report with failure-time SHAs, a backward-compatible fix proposal, and regression tests |
| `/brieflow-improve` | Propose and test one local harness candidate against a frozen baseline; maintainer review required |

The plugin also ships **11 skills** that auto-activate on relevant intent (screen
onboarding, single-phase notebook runs, run launching, the whole-screen runner,
QC, preflight, status, memory tuning, issue filing, prospective harness development, and a router) — you invoke
the commands above directly; the skills fire from natural-language context.

## License

MIT, the same license as [brieflow](https://github.com/cheeseman-lab/brieflow). See [`LICENSE`](./LICENSE).

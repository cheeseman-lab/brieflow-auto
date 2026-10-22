#!/usr/bin/env python3
"""
brieflow_run.py — Pipeline runner for brieflow analyses, called by the
/brieflow-module slash command. Invokes the screen's flow.sh, applies memory
recommendations to the slurm profile, success-gates on the terminal
module's marker (resume-aware), and records wall time to results.tsv.

Ported from the lab's earlier benchmarking harness
(harness.py:cmd_run_well_trial) with three changes:

  1. Resume-aware success gate. Looks for `Finished jobid: 0 (Rule:
     all_<terminal_module>)` across ALL flow.sh logs in the trial dir,
     not just the current run's log. Fixes the harness's bug where
     --resume mode reported "preprocess didn't complete" because
     preprocess wasn't re-run.

  2. Parameterized paths. Takes --analysis-dir, --config, --mem-recs,
     --output-dir as args; no hardcoded brieflow-speed paths.

  3. No "trial JSON" indirection. Args are passed directly. The plugin's
     /brieflow-module command builds them from user input.

This script does NOT do recovery. On flow.sh failure it returns nonzero
and the caller (/brieflow-module) invokes brieflow_recover.py to classify
+ relaunch.
"""

from __future__ import annotations

import argparse
import os
import json
import resource
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml


def _raise_nofile_limit() -> tuple[int, int] | None:
    """Raise the open-file-descriptor soft limit to the hard cap before launching
    snakemake. zarr stores each image as thousands of tiny files, so at high
    --cores the default soft nofile (1024 from the systemd/tmux inheritance)
    is exhausted → `[Errno 24] Too many open files` mid-phase (jannu preprocess
    died at 60% on exactly this, 2026-06-22). Idempotent; best-effort — returns
    (old_soft, new_soft) on a raise, None if unchanged/unavailable."""
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if hard != resource.RLIM_INFINITY and soft >= hard:
            return None
        target = hard
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        return (soft, target)
    except (ValueError, OSError):
        return None

# Runner conductor — imported at module level so tests can monkeypatch it.
# Wrapped in try/except so brieflow_run.py can be imported without brieflow_runner
# on sys.path (e.g. in test environments that load only this file).
try:
    _SCRIPT_DIR = Path(__file__).resolve().parent
    if str(_SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPT_DIR))
    from brieflow_runner import run_screen  # noqa: E402
    from brieflow_phases import PHASES  # noqa: E402
except Exception:
    def run_screen(args) -> int:  # type: ignore[misc]
        print("ERROR: brieflow_runner not available.", file=sys.stderr)
        return 1
    PHASES = []  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Slurm profile builder
# ---------------------------------------------------------------------------


def load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def base_profile_established(path: Path) -> bool:
    """True when the base profile exists and names a slurm_partition in default-resources."""
    if not path.exists():
        return False
    res = load_yaml(path).get("default-resources") or {}
    if isinstance(res, list):
        res = dict(str(r).split("=", 1) for r in res if "=" in str(r))
    return bool(res.get("slurm_partition"))


def write_slurm_profile(
    base_profile_path: Path,
    out_dir: Path,
    jobs: int = 400,
    cpus_per_task: int = 1,
    mem_recs: dict | None = None,
) -> Path:
    """Build a slurm profile yaml with mem-recs applied as set-resources."""
    profile = load_yaml(base_profile_path)
    profile["jobs"] = jobs
    # Snakemake sums numeric resources across a job group (partition 20 x 3 rules -> -p 60).
    res = profile.get("default-resources")
    if isinstance(res, dict):
        for key in ("slurm_partition", "slurm_account"):
            if res.get(key) is not None:
                res[key] = str(res[key])

    set_res = dict(profile.get("set-resources", {}))
    if mem_recs:
        for rule, data in mem_recs.items():
            if rule not in set_res:
                set_res[rule] = {}
            mem_mb = data.get("mem_mb_recommended")
            if mem_mb:
                set_res[rule]["mem_mb"] = mem_mb
    profile["set-resources"] = set_res

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "config.yaml"
    with open(out_path, "w") as f:
        yaml.dump(profile, f, default_flow_style=False)
    return out_dir


# ---------------------------------------------------------------------------
# Snakemake helpers
# ---------------------------------------------------------------------------


def unlock_snakemake(analysis_dir: Path, snakefile: str, configfile: str) -> None:
    """Run snakemake --unlock to clear stale locks."""
    proc = subprocess.run(
        ["snakemake", "--unlock", "--snakefile", snakefile, "--configfile", configfile],
        cwd=analysis_dir,
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        print("[brieflow_run] snakemake unlocked.", file=sys.stderr)


# ---------------------------------------------------------------------------
# flow.sh invoker
# ---------------------------------------------------------------------------


def run_flow(
    analysis_dir: Path,
    modules: list[str],
    configfile: str,
    backend: str,
    profile_dir: Path | None,
    array_limit: int,
    use_arrays: bool,
    latency_wait: int,
    cores: str,
    jobs: int,
    forcerun: list[str] | None = None,
    only_plate: int | None = None,
) -> tuple[Path, float, int]:
    """Run flow.sh in analysis_dir and return (log_path, wall_seconds, exit_code).

    log_path is the most-recent flow.sh aggregate log under <analysis_dir>/logs/
    matching the timestamp the run started.

    `only_plate`, when set, restricts a plate-sequential module to that single
    plate (flow.sh --only-plate N) — the branch half of the runner's
    branch-and-converge SBS, where each plate runs against its own configfile.
    """
    log_dir = analysis_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Raise nofile to the hard cap so the zarr job fan-out doesn't exhaust FDs;
    # the child snakemake (and its job subprocesses) inherit the raised limit.
    raised = _raise_nofile_limit()
    if raised:
        print(
            f"[brieflow_run] raised nofile soft limit {raised[0]}→{raised[1]} "
            f"(zarr FD fan-out)",
            file=sys.stderr,
        )

    cmd = [
        "bash",
        "flow.sh",
        *modules,
        "--backend",
        backend,
        "--profile",
        "--configfile",
        configfile,
    ]
    if only_plate is not None:
        cmd += ["--only-plate", str(only_plate)]

    if backend == "slurm":
        if profile_dir is not None:
            cmd += ["--slurm-profile", str(profile_dir)]
        cmd += ["--slurm-array-limit", str(array_limit)]
        cmd += ["--arrays" if use_arrays else "--no-arrays"]
        cmd += ["--latency-wait", str(latency_wait)]
        # Forward --jobs so flow.sh's explicit --jobs matches the profile's jobs
        # cap (both = this value), instead of flow.sh deriving a stale nproc value
        # that would override the profile on the snakemake CLI.
        cmd += ["--jobs", str(jobs)]
    else:
        cmd += ["--cores", str(cores)]

    if forcerun:
        cmd += ["--forcerun", ",".join(forcerun)]

    print(f"[brieflow_run] cwd={analysis_dir}", file=sys.stderr)
    print(f"[brieflow_run] cmd: {' '.join(cmd)}", file=sys.stderr)

    # flow.sh otherwise globs the plugin cache and can pick an older installed version.
    env = dict(os.environ)
    env.setdefault("BRIEFLOW_OPS_PLUGIN_DIR", str(Path(__file__).resolve().parent.parent))
    start = time.time()
    proc = subprocess.run(cmd, cwd=analysis_dir, env=env)
    elapsed = time.time() - start

    # Find the flow.sh aggregate log that was just written.
    candidates = sorted(
        [p for p in log_dir.glob("*.log") if ts[:13] in p.name],
        key=lambda p: p.stat().st_mtime,
    )
    log_path = candidates[-1] if candidates else Path("/dev/null")
    return log_path, elapsed, proc.returncode


# ---------------------------------------------------------------------------
# Resume-aware success gate
# ---------------------------------------------------------------------------


def check_terminal_marker(
    analysis_dir: Path,
    flow_log_dir: Path,
    terminal_module: str,
    current_flow_log: Path | None = None,
) -> tuple[bool, list[Path]]:
    """Search for `Finished jobid: 0 (Rule: all_<terminal_module>)` across
    all flow.sh aggregate logs AND all snakemake master logs (resume-aware:
    the marker may appear in an older log from a prior attempt).

    Also accepts snakemake's `Nothing to be done (all requested files are
    present and up to date).` line as a positive terminal state — but ONLY
    when it appears in the CURRENT flow.sh log (not historical logs). This
    matters because "Nothing to be done" is a generic snakemake message that
    appears in many historical runs across the analysis dir's lifetime. If
    we matched it anywhere, ANY phase that doesn't actually do its work but
    finds a stale historical "Nothing to be done" line would falsely report
    success — caught on a reference run (phase 2): brieflow_run.py reported
    terminal_marker_present=True via stale "Nothing to be done" while
    snakemake actually died with IncompleteFilesException at DAG-build.

    Returns (success, list_of_logs_with_marker_or_nothing_to_do).
    """
    marker = f"Finished jobid: 0 (Rule: all_{terminal_module})"
    nothing_to_do = (
        "Nothing to be done (all requested files are present and up to date)."
    )

    matched: list[Path] = []

    # The literal `Finished jobid: 0` marker is rule-specific and globally
    # unique-enough to scan all historical logs (resume-aware).
    snake_log_dir = analysis_dir / ".snakemake" / "log"
    if snake_log_dir.exists():
        for p in snake_log_dir.glob("*.snakemake.log"):
            try:
                with open(p) as f:
                    if marker in f.read():
                        matched.append(p)
            except OSError:
                continue
    if flow_log_dir.exists():
        for p in flow_log_dir.glob("*.log"):
            try:
                with open(p) as f:
                    if marker in f.read():
                        matched.append(p)
            except OSError:
                continue

    # The "Nothing to be done" line is generic — only match it in the CURRENT
    # invocation's flow.sh log (passed by caller). This prevents stale-log
    # false positives.
    if current_flow_log is not None and current_flow_log.exists():
        try:
            with open(current_flow_log) as f:
                if nothing_to_do in f.read():
                    matched.append(current_flow_log)
        except OSError:
            pass

    return bool(matched), matched


# ---------------------------------------------------------------------------
# Result recording
# ---------------------------------------------------------------------------


RESULTS_HEADER = (
    "tag\tbackend\tlatency_wait\tarray_limit\tmodules\twall_time_min\tnotes\n"
)


def record_result(
    results_path: Path,
    tag: str,
    backend: str,
    latency_wait: int,
    array_limit: int,
    modules: list[str],
    wall_seconds: float,
    notes: str = "",
) -> None:
    if not results_path.exists():
        results_path.parent.mkdir(parents=True, exist_ok=True)
        with open(results_path, "w") as f:
            f.write(RESULTS_HEADER)
    with open(results_path, "a") as f:
        f.write(
            "\t".join(
                [
                    tag,
                    backend,
                    str(latency_wait),
                    str(array_limit),
                    ",".join(modules),
                    f"{wall_seconds / 60:.1f}",
                    notes.replace("\t", " ").replace("\n", " "),
                ]
            )
            + "\n"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------



def _prepend_interpreter_bin_to_path() -> None:
    """Put this interpreter's bin dir first on PATH so `snakemake` (and flow.sh's
    snakemake) resolve even when the screen env was not `conda activate`d — the
    VM orchestrator launches the runner by the env's absolute python."""
    bindir = str(Path(sys.executable).resolve().parent)
    path = os.environ.get("PATH", "")
    if bindir not in path.split(os.pathsep):
        os.environ["PATH"] = bindir + os.pathsep + path


def main() -> int:
    _prepend_interpreter_bin_to_path()
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--analysis-dir",
        required=True,
        type=Path,
        help="Screen analysis dir (contains flow.sh, config/, slurm/).",
    )
    p.add_argument(
        "--modules",
        nargs="+",
        required=False,
        default=None,
        help="Modules to run, e.g.: preprocess sbs phenotype merge. "
        "Required unless --screen (which loops over all phases itself).",
    )
    p.add_argument(
        "--config",
        default="config/config.yml",
        help="Config file relative to --analysis-dir. Default: config/config.yml",
    )
    p.add_argument(
        "--only-plate",
        type=int,
        default=None,
        help="Restrict a plate-sequential module (preprocess/sbs/phenotype) to a "
        "single plate instead of flow.sh's all-plates loop. Used by the runner's "
        "branch-and-converge SBS to run each plate against its own config "
        "(per-plate skip_cycles / manual_background). Default: all plates.",
    )
    p.add_argument("--backend", default="slurm", choices=["local", "slurm"])
    p.add_argument(
        "--cores", default="all", help="Cores for local backend (default: all)"
    )
    p.add_argument("--jobs", type=int, default=400)
    p.add_argument("--array-limit", type=int, default=10)
    p.add_argument(
        "--use-arrays",
        action="store_true",
        help="Enable slurm array jobs. Off by default (the array "
        "wildcard-collision bug in snakemake-executor-plugin-slurm "
        "2.6.x makes arrays unsafe at well/full scale; see CLAUDE.md).",
    )
    p.add_argument(
        "--latency-wait",
        type=int,
        default=5,
        help="snakemake --latency-wait seconds. Default 5; brieflow_recover "
        "escalates to a hard cap of 10 on NFS-visibility failures.",
    )
    p.add_argument("--cpus-per-task", type=int, default=1)
    p.add_argument(
        "--mem-recs",
        type=Path,
        default=None,
        help="mem_recommendations.json path. Default: auto-discover "
        "(<analysis-dir>/cluster_optimization/results/, then "
        "<analysis-dir>/harness/results/, then "
        "<analysis-dir>/mem_recommendations.json)",
    )
    p.add_argument(
        "--base-profile",
        type=Path,
        default=None,
        help="Base slurm profile yaml. Default: <analysis-dir>/slurm/config.yaml",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="brieflow output dir. Default: "
        "<analysis-dir>/brieflow_output (auto-discovered from config)",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="Deprecated; default behavior is now to keep all existing "
        "outputs. Kept for backwards compat (no effect).",
    )
    p.add_argument(
        "--clean-modules",
        nargs="*",
        default=[],
        help="Modules whose subdirs to wipe before running, e.g. "
        "`--clean-modules preprocess sbs` would `rm -rf "
        "brieflow_output/preprocess brieflow_output/sbs`. Default: [] "
        "(no wipe). Use this only when you explicitly want to redo "
        "a module from scratch.",
    )
    p.add_argument(
        "--clean-all",
        action="store_true",
        help="Wipe the entire brieflow_output dir before running. "
        "DESTRUCTIVE — use only for cold restarts. Default: off.",
    )
    p.add_argument(
        "--results-tsv",
        type=Path,
        default=None,
        help="Where to record this run's wall time. Default: "
        "<analysis-dir>/brieflow_run_results.tsv",
    )
    p.add_argument(
        "--tag", default=None, help="Run tag for results.tsv. Default: timestamp-based."
    )
    p.add_argument(
        "--notes", default="", help="Free-text notes column for results.tsv."
    )
    p.add_argument(
        "--profile-out",
        type=Path,
        default=None,
        help="Where to write the per-trial slurm profile dir.",
    )
    p.add_argument(
        "--forcerun",
        nargs="*",
        default=[],
        help="Rules to force-rerun even if their outputs are "
        "up-to-date (snakemake --forcerun passthrough via "
        "flow.sh, HARDENING #7 layer 1). Use after editing a "
        "rule's script.",
    )
    p.add_argument(
        "--screen", action="store_true", dest="screen",
        help="Run the per-phase cycle (notebook -> preflight -> run -> QC) over the "
        "phase list, bounded by --from/--through/--only. Writes .brieflow/launch_review.html "
        "and stops (exit 8) until re-run with --approved.",
    )
    p.add_argument("--from", dest="from_phase", default=None, choices=PHASES,
                   help="First phase to run (earlier phases are skipped, not marked done).")
    p.add_argument("--through", dest="through", default=None, choices=PHASES,
                   help="Last phase to run. Falls back to BRIEFLOW_STOP_AFTER when unset.")
    p.add_argument("--only", dest="only", default=None, choices=PHASES,
                   help="Run exactly this phase (equivalent to --from X --through X).")
    p.add_argument("--lifecycle-hooks", dest="lifecycle_hooks", default="noop", choices=["noop", "gcs"],
                   help="Data movement around the run: noop (shared storage) or gcs (pull inputs "
                   "before the first phase, push brieflow_output after the last).")
    p.add_argument("--screen-id", dest="screen_id", default=None,
                   help="Screen id for lifecycle hooks. Default: analysis dir name.")
    p.add_argument("--approved", action="store_true", dest="approved",
                   help="Proceed past the launch review (requires a matching .brieflow/launch_review.json).")
    p.add_argument("--accept-qc", dest="accept_qc", default=None, choices=PHASES,
                   help="Accept this phase's failed QC gate: journal it and mark the phase done "
                   "without a rerun (needs a finished run of the phase; the launch review is "
                   "unchanged).")
    p.add_argument("--accept-layout", dest="accept_layout", action="store_true",
                   help="Run on input data whose layout check only warns (e.g. phenotype rounds "
                   "of different sizes), after the operator confirms it; journaled.")
    # Disk guards (OUTSTANDING #16) — overridable instead of hard-coded. Looser than
    # brieflow_runloop.py's standalone CLI (0.75 / 0.90); the runner also checks each
    # phase's disk need before launch.
    p.add_argument(
        "--disk-warn-pct", type=float, default=0.99, dest="disk_warn_pct",
        help="Warn when disk used-fraction exceeds this (default 0.99).",
    )
    p.add_argument(
        "--disk-abort-pct", type=float, default=0.995, dest="disk_abort_pct",
        help="Abort the run when disk used-fraction exceeds this (default 0.995).",
    )
    p.add_argument(
        "--disk-abort-min-free-gb", type=float, default=10.0, dest="disk_abort_min_free_gb",
        help="Also abort when free space drops below this many GB regardless of "
        "used-%% (absolute floor for small disks; default 10).",
    )
    # OUTSTANDING #19 — opt-in: free raw input after preprocess QC passes. Takes the
    # gs:// backup prefix; the input is deleted ONLY after gsutil confirms it exists
    # there. Default off (never deletes unless explicitly enabled + verified).
    p.add_argument(
        "--free-raw-input-after-preprocess", default=None, dest="free_raw_input_gcs",
        metavar="GS_PREFIX",
        help="After preprocess passes QC, delete the local raw input dirs IFF they "
        "are confirmed present under this gs:// prefix. Opt-in; default off.",
    )
    args = p.parse_args()

    # --lifecycle-hooks and --screen-id are read by run_screen and by nothing else, so
    # a non-default selection outside --screen is a request that would be ignored.
    screen_only = (
        args.from_phase
        or args.through
        or args.only
        or args.approved
        or args.lifecycle_hooks != "noop"
        or args.screen_id
    )
    if screen_only and not args.screen:
        p.error("--from/--through/--only/--approved/--lifecycle-hooks/--screen-id require --screen")
    if args.only and (args.from_phase or args.through):
        p.error("--only cannot be combined with --from/--through")
    if args.from_phase and args.through and PHASES.index(args.through) < PHASES.index(args.from_phase):
        p.error(f"--through {args.through} comes before --from {args.from_phase}")
    if not args.screen and not args.modules:
        p.error("--modules is required unless --screen")

    analysis_dir = args.analysis_dir.resolve()
    if not analysis_dir.exists():
        print(f"ERROR: --analysis-dir {analysis_dir} not found.", file=sys.stderr)
        return 2

    # --screen dispatch: route to the conductor before the single-phase path.
    if args.screen:
        args.analysis_dir = analysis_dir
        if not hasattr(args, "tag") or args.tag is None:
            args.tag = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        # Supply defaults for fields run_screen/run_one_phase expect but
        # brieflow_run.py may not have set yet at this point. The OOM-cascade pair
        # (3 / 30s) is tighter than brieflow_runloop.py's standalone CLI (100 / 60s);
        # the disk entries only apply to callers that build args without the parser.
        for _field, _default in [
            ("max_attempts", 3),
            ("no_aggregate_efficiency", False),
            ("require_rerun", None),
            ("oom_cascade_threshold", 3),
            ("oom_poll_seconds", 30),
            ("disk_warn_pct", 0.99),
            ("disk_abort_pct", 0.995),
            ("disk_abort_min_free_gb", 10.0),
            ("free_raw_input_gcs", None),
            ("lifecycle_hooks", "noop"),
            ("screen_id", None),
            ("approved", False),
            ("accept_qc", None),
            ("accept_layout", False),
            ("from_phase", None),
            ("through", None),
            ("only", None),
        ]:
            if not hasattr(args, _field):
                setattr(args, _field, _default)
        return run_screen(args)

    flow_sh = analysis_dir / "flow.sh"
    if not flow_sh.exists():
        print(f"ERROR: flow.sh not found at {flow_sh}", file=sys.stderr)
        return 2

    config_full = analysis_dir / args.config
    if not config_full.exists():
        print(f"ERROR: config not found at {config_full}", file=sys.stderr)
        return 2

    modules = args.modules
    terminal_module = modules[-1]
    tag = args.tag or f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    # Output dir auto-discovery: read from config's `output_path` if present,
    # else assume <analysis-dir>/brieflow_output.
    if args.output_dir:
        output_dir = args.output_dir.resolve()
    else:
        config_data = load_yaml(config_full)
        op = config_data.get("output_path")
        if op:
            output_dir = (analysis_dir / op).resolve()
        else:
            output_dir = analysis_dir / "brieflow_output"

    # Mem recommendations: auto-discover in conventional locations, then
    # fall through to the plugin's bundled baseline so a fresh screen never
    # starts from snakemake's 3 GB default for every rule.
    PLUGIN_BASELINE = (
        Path(__file__).resolve().parent.parent
        / "data"
        / "mem_recommendations_baseline.json"
    )

    def _discover_mem_recs(adir: Path) -> Path:
        # Canonical: <analysis-dir>/slurm/mem_recommendations.json (next to
        # slurm/config.yaml). Legacy locations kept for backwards compat.
        candidates = [
            adir / "slurm" / "mem_recommendations.json",
            adir / "cluster_optimization" / "results" / "mem_recommendations.json",
            adir / "harness" / "results" / "mem_recommendations.json",
            adir / "mem_recommendations.json",
        ]
        for c in candidates:
            if c.exists():
                return c
        if PLUGIN_BASELINE.exists():
            return PLUGIN_BASELINE
        return candidates[0]

    mem_recs_path = args.mem_recs or _discover_mem_recs(analysis_dir)
    mem_recs: dict = {}
    if mem_recs_path.exists():
        try:
            with open(mem_recs_path) as f:
                mem_recs = json.load(f)
            origin = " (plugin baseline)" if mem_recs_path == PLUGIN_BASELINE else ""
            print(
                f"[brieflow_run] loaded {len(mem_recs)} mem_recs from "
                f"{mem_recs_path}{origin}",
                file=sys.stderr,
            )
        except (json.JSONDecodeError, OSError) as e:
            print(
                f"[brieflow_run] WARN: could not parse {mem_recs_path}: {e}",
                file=sys.stderr,
            )
    else:
        print(
            f"[brieflow_run] no mem_recommendations.json found and no plugin "
            f"baseline at {PLUGIN_BASELINE} — using brieflow defaults (3GB)",
            file=sys.stderr,
        )

    # Slurm profile
    base_profile = args.base_profile or analysis_dir / "slurm" / "config.yaml"
    profile_dir: Path | None = None
    if args.backend == "slurm":
        if not base_profile_established(base_profile):
            print(
                f"ERROR: base slurm profile missing or without a slurm_partition at "
                f"{base_profile}. Generate it "
                f"from a probe: `brieflow_compute.py --probe` to see partitions, then "
                f"`brieflow_compute.py --write --analysis-dir {analysis_dir} "
                f"--backend slurm --partition <chosen> --account <chosen>`. "
                f"(Or run with `--backend local` if this host has no slurm.)",
                file=sys.stderr,
            )
            return 2
        profile_out = (
            args.profile_out or analysis_dir / ".brieflow_run" / tag / "slurm_profile"
        )
        profile_dir = write_slurm_profile(
            base_profile,
            profile_out,
            jobs=args.jobs,
            cpus_per_task=args.cpus_per_task,
            mem_recs=mem_recs,
        )

    # Output handling — default is KEEP. Wipes are explicit only.
    # Default: do nothing — snakemake will skip already-done jobs and rerun
    # only what's missing. This was changed from the prior default-wipe behavior
    # after that default destroyed 19+ hours of phase-1 work in a
    # reference zarr full run.
    if args.clean_all and output_dir.exists():
        print(f"[brieflow_run] --clean-all: wiping {output_dir}", file=sys.stderr)
        shutil.rmtree(output_dir)
    elif args.clean_modules:
        for m in args.clean_modules:
            sub = output_dir / m
            if sub.exists():
                print(f"[brieflow_run] --clean-modules: wiping {sub}", file=sys.stderr)
                shutil.rmtree(sub)
    elif output_dir.exists():
        print(
            f"[brieflow_run] keeping existing {output_dir} "
            f"(snakemake will skip done jobs)",
            file=sys.stderr,
        )

    unlock_snakemake(
        analysis_dir,
        snakefile="../brieflow/workflow/Snakefile",
        configfile=args.config,
    )

    log_path, wall, exit_code = run_flow(
        analysis_dir,
        modules,
        args.config,
        args.backend,
        profile_dir,
        args.array_limit,
        args.use_arrays,
        args.latency_wait,
        args.cores,
        args.jobs,
        forcerun=args.forcerun,
        only_plate=getattr(args, "only_plate", None),
    )

    # Resume-aware success gate. Pass log_path as current_flow_log so the
    # generic "Nothing to be done" line only matches in this run's log,
    # not stale historical logs.
    success, marker_logs = check_terminal_marker(
        analysis_dir, analysis_dir / "logs", terminal_module, current_flow_log=log_path
    )

    print("\n" + "=" * 60, file=sys.stderr)
    print(f"[brieflow_run] tag={tag}", file=sys.stderr)
    print(
        f"[brieflow_run] modules={modules} (terminal={terminal_module})",
        file=sys.stderr,
    )
    print(f"[brieflow_run] wall={wall / 60:.1f} min  exit={exit_code}", file=sys.stderr)
    print(f"[brieflow_run] log={log_path}", file=sys.stderr)
    print(f"[brieflow_run] terminal_marker_present={success}", file=sys.stderr)
    if marker_logs:
        print(
            f"[brieflow_run] marker_logs={[str(p) for p in marker_logs]}",
            file=sys.stderr,
        )
    print("=" * 60, file=sys.stderr)

    # Emit a structured JSON summary on stdout for the calling slash command.
    summary = {
        "tag": tag,
        "modules": modules,
        "terminal_module": terminal_module,
        "wall_seconds": wall,
        "wall_minutes": round(wall / 60, 1),
        "exit_code": exit_code,
        "log_path": str(log_path),
        "success": bool(success and exit_code == 0),
        "terminal_marker_present": bool(success),
        "marker_logs": [str(p) for p in marker_logs],
    }
    # Single-line JSON so the runloop's reversed-line fallback can find it.
    # flow.sh stdout is inherited (huge), so multi-line JSON gets buried.
    sys.stdout.write(json.dumps(summary, sort_keys=True) + "\n")
    sys.stdout.flush()

    if summary["success"]:
        results_tsv = args.results_tsv or analysis_dir / "brieflow_run_results.tsv"
        record_result(
            results_tsv,
            tag,
            args.backend,
            args.latency_wait,
            args.array_limit,
            modules,
            wall,
            args.notes,
        )
        print(f"[brieflow_run] recorded to {results_tsv}", file=sys.stderr)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())

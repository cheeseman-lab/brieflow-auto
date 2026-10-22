#!/usr/bin/env python3
"""
brieflow_dryrun_preview.py — DAG-state preview before a phase transition.

HARDENING_TODO #6: the terminal marker `Finished jobid: 0 (Rule: all_X)`
only tells you snakemake satisfied that rule's inputs — not that the
downstream phases will see what they need. This script runs
`snakemake --dry-run --until all_<terminal>` and reports what would
actually be scheduled, classified into:

  clean-resume   — only the terminal aggregator rule pending (no-op)
  partial-resume — residual jobs from this phase only (safe to retry)
  upstream-rerun — dry-run wants to rerun rules from a prior phase
                   (terminal marker lied about cross-phase state)
  full-restart   — large fraction of all rules pending (something wiped)

The runloop calls this as a gate between phases and after recover.py
bumps, so it can abort early instead of burning compute on a stale DAG.

It also serves the layer-2 piece of HARDENING_TODO #7 (modified-rule
confirmation): emit `silently-skipped-edited-rule` when an operator-
declared edited rule is NOT in the rebuild set.
"""

from __future__ import annotations

import argparse
import os
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from brieflow_phases import NOTEBOOK_ONLY_PHASES, PHASE_ORDER


# ---------------------------------------------------------------------------
# Phase → rule mapping for brieflow.
#
# Update as brieflow grows. The classifier needs this to decide whether a
# pending rule belongs to "this phase" (partial-resume), "upstream" (rerun
# = bad), or "downstream" (expected for terminal aggregator dry-runs).
# Source: walking workflow/rules/*.smk + aggregate_efficiency RULE_MEMORY_
# PROFILE keys + group memberships.
# ---------------------------------------------------------------------------


PHASE_RULES: dict[str, set[str]] = {
    "preprocess": {
        "convert_sbs",
        "convert_phenotype",
        "extract_metadata_sbs",
        "extract_metadata_phenotype",
        "calculate_ic_sbs",
        "calculate_ic_phenotype",
        "combine_metadata_sbs",
        "combine_metadata_phenotype",
        "all_preprocess",
    },
    "sbs": {
        "align_sbs",
        "log_filter",
        "compute_standard_deviation",
        "find_peaks",
        "max_filter",
        "apply_ic_field_sbs",
        "segment_sbs",
        "extract_bases",
        "call_reads",
        "call_cells",
        "extract_sbs_info",
        "combine_reads",
        "combine_cells",
        "combine_sbs_info",
        "eval_segmentation_sbs",
        "sbs_tile_group",
        "all_sbs",
    },
    "phenotype": {
        "apply_ic_field_phenotype",
        "align_phenotype",
        "segment_phenotype",
        "identify_cytoplasm",
        "extract_phenotype_info",
        "extract_phenotype",
        "extract_phenotype_cp",
        "combine_phenotype_info",
        "eval_segmentation_phenotype",
        "eval_features",
        "phenotype_tile_group",
        "all_phenotype",
    },
    "merge": {
        "fast_alignment",
        "merge",
        "merge_phenotype",
        "merge_phenotype_cp",
        "fast_merge",
        "align",
        "deduplicate_merge",
        "format_merge",
        "clean_merge",
        "final_merge",
        "eval_merge",
        "eval_mapping",
        "filter",
        "all_merge",
    },
    "aggregate": {
        "split_datasets",
        "aggregate",
        "clean_aggregate",
        "eval_aggregate",
        "prepare_bootstrap_data",
        "initiate_bootstrap",
        "bootstrap_gene",
        "combine_bootstrap",
        "generate_feature_table",
        "all_aggregate",
    },
    "cluster": {
        "phate_leiden_clustering",
        "benchmark_clusters",
        "initiate_montage",
        "prepare_montage_data",
        "generate_montage",
        "format_singlecell_anndata",
        "all_cluster",
    },
}


# Authoritative rule->phase, derived at runtime from the brieflow rules/ + targets/
# *.smk files. The static PHASE_RULES below drifts from the library (audited 2026-06-12:
# 7 rules mis-mapped, incl. aggregate's own `align`/`filter` tagged as 'merge'), which
# made the upstream-rerun guard FALSE-POSITIVE and block a legitimate aggregate run.
# Populated by load_smk_rule_phase(); falls back to PHASE_RULES when unavailable.
_SMK_RULE_PHASE: dict[str, str] = {}


def load_smk_rule_phase(brieflow_dir) -> int:
    """Parse `rule X:` from rules/<phase>.smk + targets/<phase>.smk into the
    authoritative rule->phase override. Returns the number of rules mapped
    (0 = dir unreadable → callers fall back to the static PHASE_RULES)."""
    mapping: dict[str, str] = {}
    for sub in ("rules", "targets"):
        d = Path(brieflow_dir) / "workflow" / sub
        if not d.is_dir():
            continue
        for smk in d.glob("*.smk"):
            phase = smk.stem
            if phase not in PHASE_ORDER:
                continue
            try:
                text = smk.read_text()
            except OSError:
                continue
            for m in re.finditer(r"^rule\s+([a-zA-Z0-9_]+):", text, re.M):
                mapping[m.group(1)] = phase
    _SMK_RULE_PHASE.clear()
    _SMK_RULE_PHASE.update(mapping)
    return len(mapping)


def rule_phase(rule: str) -> str | None:
    """Return the phase that owns this rule, or None if unrecognized. Prefers the
    authoritative .smk-derived map (load_smk_rule_phase) over the static PHASE_RULES."""
    if _SMK_RULE_PHASE:
        hit = _SMK_RULE_PHASE.get(rule)
        if hit is not None:
            return hit
    for phase, rules in PHASE_RULES.items():
        if rule in rules:
            return phase
    return None


def _phase_index(phase: str) -> int:
    try:
        return PHASE_ORDER.index(phase)
    except ValueError:
        return 99


# ---------------------------------------------------------------------------
# Dry-run output parsing
# ---------------------------------------------------------------------------


# Job stats table format:
#     Job stats:
#     job                           count
#     --------------------------  -------
#     convert_phenotype                10
#     ...
#     total                           150
_JOB_STATS_RE = re.compile(r"^([A-Za-z_][\w]*)\s+(\d+)\s*$")


def parse_job_stats(output: str) -> dict[str, int]:
    """Extract {rule: count} from snakemake --dry-run output. Excludes total."""
    counts: dict[str, int] = {}
    in_table = False
    for line in output.splitlines():
        if line.startswith("Job stats:"):
            in_table = True
            continue
        if not in_table:
            continue
        if line.startswith("---"):
            continue
        if line.startswith("Reasons:") or line.strip() == "":
            # Reasons block follows the table; stop here. A blank line
            # delimits the end too.
            if counts:
                break
            continue
        m = _JOB_STATS_RE.match(line)
        if not m:
            continue
        rule = m.group(1)
        n = int(m.group(2))
        if rule == "total":
            break
        counts[rule] = n
    return counts


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


@dataclass
class DryRunResult:
    terminal_module: str
    pending: dict[str, int]
    total_jobs: int
    classification: str
    by_phase: dict[str, dict[str, int]] = field(default_factory=dict)
    unrecognized_rules: list[str] = field(default_factory=list)
    silently_skipped_rules: list[str] = field(default_factory=list)
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "terminal_module": self.terminal_module,
            "pending": self.pending,
            "total_jobs": self.total_jobs,
            "classification": self.classification,
            "by_phase": self.by_phase,
            "unrecognized_rules": self.unrecognized_rules,
            "silently_skipped_rules": self.silently_skipped_rules,
            "detail": self.detail,
        }


def classify(
    pending: dict[str, int],
    terminal_module: str,
    require_rerun: list[str] | None = None,
    full_restart_threshold: int = 500,
) -> DryRunResult:
    """Classify a dry-run pending-job map for the given terminal_module.

    Heuristics (intentionally conservative — false-clean-resume is the
    operator-dangerous bias; false-partial-resume is fine since the runloop
    will see the work happen anyway):
      - clean-resume: only the all_<terminal> aggregator (or empty)
      - upstream-rerun: ANY pending rule belongs to a phase BEFORE
        terminal_module → ABORT
      - full-restart: total jobs ≥ full_restart_threshold (suggests state wipe)
      - partial-resume: pending rules all in current or downstream phases
    """
    total_jobs = sum(pending.values())
    terminal_phase_idx = _phase_index(terminal_module)
    by_phase: dict[str, dict[str, int]] = {}
    unrecognized: list[str] = []
    upstream_rules: list[str] = []
    aggregator_only = True
    aggregator_rule = f"all_{terminal_module}"

    for rule, count in pending.items():
        phase = rule_phase(rule)
        if phase is None:
            unrecognized.append(rule)
            aggregator_only = False
            continue
        by_phase.setdefault(phase, {})[rule] = count
        if rule != aggregator_rule:
            aggregator_only = False
            if _phase_index(phase) < terminal_phase_idx:
                upstream_rules.append(rule)

    # Modified-rule check (#7 layer 2): operator declared `require-rerun X`.
    # If X is not in pending, the DAG won't actually rebuild it — silent skip.
    silently_skipped: list[str] = []
    if require_rerun:
        for r in require_rerun:
            if r not in pending or pending[r] == 0:
                silently_skipped.append(r)

    if silently_skipped:
        return DryRunResult(
            terminal_module=terminal_module,
            pending=pending,
            total_jobs=total_jobs,
            classification="silently_skipped_edited_rule",
            by_phase=by_phase,
            unrecognized_rules=unrecognized,
            silently_skipped_rules=silently_skipped,
            detail=(
                f"--require-rerun named {silently_skipped} but "
                "none are in the rebuild set; the DAG would skip them "
                "due to mtime triggers. Force a rebuild via --forcerun "
                "or delete the outputs manually."
            ),
        )

    if not pending or aggregator_only:
        return DryRunResult(
            terminal_module=terminal_module,
            pending=pending,
            total_jobs=total_jobs,
            classification="clean_resume",
            by_phase=by_phase,
            unrecognized_rules=unrecognized,
            detail=f"only {aggregator_rule} pending — phase is done",
        )

    if upstream_rules:
        return DryRunResult(
            terminal_module=terminal_module,
            pending=pending,
            total_jobs=total_jobs,
            classification="upstream_rerun",
            by_phase=by_phase,
            unrecognized_rules=unrecognized,
            detail=(
                f"{len(upstream_rules)} pending rule(s) belong to "
                f"phases BEFORE {terminal_module}: "
                f"{sorted(set(upstream_rules))[:8]}"
            ),
        )

    if total_jobs >= full_restart_threshold:
        return DryRunResult(
            terminal_module=terminal_module,
            pending=pending,
            total_jobs=total_jobs,
            classification="full_restart",
            by_phase=by_phase,
            unrecognized_rules=unrecognized,
            detail=(
                f"total {total_jobs} jobs ≥ threshold "
                f"{full_restart_threshold} — DAG suggests a state wipe"
            ),
        )

    return DryRunResult(
        terminal_module=terminal_module,
        pending=pending,
        total_jobs=total_jobs,
        classification="partial_resume",
        by_phase=by_phase,
        unrecognized_rules=unrecognized,
        detail=f"{total_jobs} residual jobs in current/downstream phases",
    )


# ---------------------------------------------------------------------------
# Snakemake driver
# ---------------------------------------------------------------------------


def run_snakemake_dryrun(
    analysis_dir: Path,
    config_path: Path,
    brieflow_dir: Path,
    terminal_module: str,
    extra_args: list[str] | None = None,
    timeout: float = 300.0,
    conda_env: str | None = None,
) -> tuple[str, int]:
    snakefile = brieflow_dir / "workflow" / "Snakefile"
    cmd = [
        "snakemake",
        "--dry-run",
        "--nolock",
        "--snakefile",
        str(snakefile),
        "--configfile",
        str(config_path),
        "--until",
        f"all_{terminal_module}",
        "--rerun-incomplete",  # a killed run leaves incomplete outputs; the real run reruns them too
    ]
    if extra_args:
        cmd.extend(extra_args)
    if conda_env:
        # snakemake lives in the SCREEN env, not necessarily on the caller's PATH
        # (the runner's interpreter on the VM had none → FileNotFoundError)
        import shlex
        cmd = ["bash", "-lc", f'eval "$(conda shell.bash hook)" && conda activate {conda_env} && exec {shlex.join(cmd)}']
    try:
        proc = subprocess.run(
            cmd,
            cwd=analysis_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (
            f"[ERROR] snakemake --dry-run timed out after {timeout}s\n"
            f"command: {' '.join(cmd)}\n"
        ), 124
    return (proc.stdout or "") + (proc.stderr or ""), proc.returncode


def find_brieflow_dir(analysis_dir: Path) -> Path | None:
    """The brieflow submodule next to (or above) the analysis dir."""
    for c in (
        analysis_dir.parent / "brieflow",
        analysis_dir / "brieflow",
        analysis_dir.parent.parent / "brieflow",
    ):
        if (c / "workflow" / "Snakefile").exists():
            return c
    return None


def parse_reasons(output: str) -> dict[str, list[str]]:
    """The dry-run `Reasons:` block as {reason: [rules]}."""
    reasons: dict[str, list[str]] = {}
    current = None
    in_block = False
    for line in output.splitlines():
        if line.startswith("Reasons:"):
            in_block = True
            continue
        if not in_block:
            continue
        if not line.strip():
            if reasons:
                break
            continue
        text = line.strip()
        if text.startswith("("):
            continue
        if text.endswith(":"):
            current = text[:-1]
            reasons.setdefault(current, [])
        elif current is not None:
            reasons[current] += [r.strip() for r in text.split(",") if r.strip()]
    return reasons


LIST_CHANGES_KINDS = ("params", "code", "input")


def rerun_reasons(
    analysis_dir: Path,
    terminal_module: str,
    conda_env: str | None = None,
    config_path: Path | None = None,
    examples: int = 5,
) -> dict:
    """Why Snakemake would rerun `terminal_module`: pending jobs per rule, the dry-run
    reasons, and `--list-changes params|code|input` (count plus a few output files)."""
    analysis_dir = Path(analysis_dir)
    brieflow_dir = find_brieflow_dir(analysis_dir)
    if brieflow_dir is None:
        return {"error": "brieflow submodule not found"}
    config_path = config_path or analysis_dir / "config" / "config.yml"
    output, rc = run_snakemake_dryrun(
        analysis_dir, config_path, brieflow_dir, terminal_module, conda_env=conda_env
    )
    result = {"rc": rc, "pending": parse_job_stats(output), "reasons": parse_reasons(output)}
    changes = {}
    for kind in LIST_CHANGES_KINDS:
        listed, _ = run_snakemake_dryrun(
            analysis_dir, config_path, brieflow_dir, terminal_module,
            extra_args=["--list-changes", kind], conda_env=conda_env,
        )
        files = [ln.strip() for ln in listed.splitlines() if re.fullmatch(r"\S+/\S+", ln.strip())]
        changes[kind] = {"count": len(files), "examples": files[:examples]}
    result["list_changes"] = changes
    return result


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
    p.add_argument("--analysis-dir", required=True, type=Path)
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Config path. Default: <analysis-dir>/config/config.yml",
    )
    p.add_argument(
        "--brieflow-dir",
        type=Path,
        default=None,
        help="brieflow submodule path. Default: auto-discover.",
    )
    p.add_argument(
        "--terminal-module",
        required=True,
        choices=list(PHASE_ORDER),
        help="The phase whose all_<X> rule we want to evaluate.",
    )
    p.add_argument(
        "--require-rerun",
        nargs="*",
        default=[],
        help="Rule names that the operator EXPECTS to be in the "
        "rebuild set (HARDENING #7 layer 2). If any is "
        "missing from the pending list, classification "
        "becomes silently_skipped_edited_rule.",
    )
    p.add_argument(
        "--full-restart-threshold",
        type=int,
        default=500,
        help="Total pending jobs threshold above which we "
        "classify as full_restart. Default 500.",
    )
    p.add_argument("--timeout", type=float, default=300.0)
    p.add_argument("--conda-env", default=None, help="run snakemake inside this conda env (screen env)")
    p.add_argument(
        "--abort-on",
        nargs="*",
        default=["upstream_rerun", "full_restart", "silently_skipped_edited_rule"],
        help="Exit non-zero when classification is in this list. "
        "Default: upstream_rerun, full_restart, "
        "silently_skipped_edited_rule.",
    )
    p.add_argument(
        "--phase-gates",
        action="store_true",
        help="Pass <phase>_rules_enabled=false for every phase except "
        "--terminal-module (matching flow.sh's phase-gate generator), so the "
        "dry-run reflects the GATED run. Without this the dry-run sees the full "
        "upstream DAG and false-classifies upstream_rerun on every downstream phase.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output (default human-readable + JSON last line).",
    )
    args = p.parse_args()

    analysis_dir = args.analysis_dir.resolve()
    if not analysis_dir.exists():
        print(f"ERROR: --analysis-dir {analysis_dir} does not exist.", file=sys.stderr)
        return 2
    config_path = (args.config or analysis_dir / "config" / "config.yml").resolve()

    # Notebook-only phases have no all_<phase> rule to dry-run: the phase
    # notebook writes config and there is nothing to schedule. Report a
    # no-op instead of failing the DAG build.
    if args.terminal_module in NOTEBOOK_ONLY_PHASES:
        result = DryRunResult(
            terminal_module=args.terminal_module,
            pending={},
            total_jobs=0,
            classification="notebook_only",
            detail=f"{args.terminal_module} has no snakemake module; nothing to dry-run",
        )
        print(json.dumps(result.to_dict(), indent=2) if args.json else f"[JSON] {json.dumps(result.to_dict())}")
        return 1 if result.classification in args.abort_on else 0

    brieflow_dir = (
        args.brieflow_dir.resolve() if args.brieflow_dir else find_brieflow_dir(analysis_dir)
    )
    if brieflow_dir is None:
        print("ERROR: could not auto-discover brieflow submodule.", file=sys.stderr)
        return 2

    # Build the authoritative rule->phase map from the brieflow .smk files so the
    # upstream-rerun classification can't be fooled by PHASE_RULES drift.
    n_mapped = load_smk_rule_phase(brieflow_dir)
    if n_mapped:
        print(f"[dryrun] rule->phase map: {n_mapped} rules from {brieflow_dir}/workflow")

    extra_args = None
    if args.phase_gates:
        gates = [
            f"{ph}_rules_enabled=false"
            for ph in PHASE_ORDER
            if ph != args.terminal_module and ph not in NOTEBOOK_ONLY_PHASES
        ]
        if gates:
            extra_args = ["--config", *gates]

    output, rc = run_snakemake_dryrun(
        analysis_dir,
        config_path,
        brieflow_dir,
        args.terminal_module,
        extra_args=extra_args,
        timeout=args.timeout,
        conda_env=args.conda_env,
    )
    if rc != 0:
        # Dry-run failure (snakemake errored out building the DAG)
        result = DryRunResult(
            terminal_module=args.terminal_module,
            pending={},
            total_jobs=0,
            classification="dryrun_error",
            detail=(
                f"snakemake --dry-run exited {rc}; last 400 chars: "
                + output[-400:].replace("\n", " ")
            ),
        )
    else:
        pending = parse_job_stats(output)
        result = classify(
            pending,
            args.terminal_module,
            require_rerun=args.require_rerun,
            full_restart_threshold=args.full_restart_threshold,
        )

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(
            f"\n=== brieflow_dryrun_preview "
            f"({args.terminal_module}, {result.classification}) ==="
        )
        print(f"  total pending jobs: {result.total_jobs}")
        if result.by_phase:
            print("  by phase:")
            for ph in PHASE_ORDER:
                if ph in result.by_phase:
                    n = sum(result.by_phase[ph].values())
                    print(f"    {ph}: {n} ({len(result.by_phase[ph])} rules)")
        if result.unrecognized_rules:
            print(
                f"  unrecognized rules (update PHASE_RULES): "
                f"{result.unrecognized_rules[:8]}"
            )
        if result.silently_skipped_rules:
            print(f"  silently skipped rules: {result.silently_skipped_rules}")
        if result.detail:
            print(f"  detail: {result.detail}")
        print("[JSON]", json.dumps(result.to_dict()))

    return 1 if result.classification in args.abort_on else 0


if __name__ == "__main__":
    sys.exit(main())

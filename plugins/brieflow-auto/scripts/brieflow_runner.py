#!/usr/bin/env python3
"""brieflow_runner.py — Conductor for /brieflow-run: the per-phase cycle over PHASES.

Composes:
  - resolve_all_phases (brieflow_wizard) — up-front interview
  - sweep_headless / pick_sweep_winner (brieflow_wizard) — parameter tuning
  - patch_marker_block_value / record_tuned_confirmed (brieflow_wizard)
  - probe_geometry (brieflow_screen_geometry)
  - compute_disk_requirements / check_disk_available (brieflow_disk)
  - run_one_phase / phase_tag / notify (brieflow_runloop)
  - build_phase_qc / qc_gate_verdict / load_qc_gates / PHASES (brieflow_qc)

Entry: run_screen(args) -> int
  0 = all phases complete
  1 = trigger 1 (QC gate fail)
  2 = trigger 2 (run_one_phase failure, or a failing on_pre_phase_1 hook)
  3 = trigger 3 (template gap at headless launch)
  4 = trigger 4 (disk unavailable)
  5 = trigger 5 (notebook run failed — config not written)
  6 = trigger 6 (preflight failed — config↔rule-code mismatch)
  7 = trigger 7 (upstream-rerun guard)
  8 = awaiting launch (launch review written, --approved not given; or no gate)
  9 = raw input data not in the expected layout (or layout warnings not yet confirmed)
"""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import re
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR.parent / "data"
_DEFAULT_LAB_DEFAULTS = DATA_DIR / "lab_defaults.yaml"

import sys  # noqa: E402

sys.path.insert(0, str(SCRIPT_DIR))

from brieflow_trace import traced  # noqa: E402

# Lazy imports from sibling scripts — kept at module level so monkeypatch
# in tests can replace them.  Each import is wrapped in a try/except so that
# the module can be loaded even when the brieflow conda env isn't active.
try:
    from brieflow_wizard import (  # noqa: E402
        resolve_all_phases,
        # The headless fallback primes each tile once (brieflow_cell_runner).
        cached_sweep_headless as sweep_headless,
        adaptive_cached_sweep as adaptive_sweep_headless,
        adaptive_marimo_sweep_multitile,
        joint_marimo_sweep_multitile,
        pick_sweep_winner,
        sweep_evidence,
        patch_marker_block_value,
        record_tuned_confirmed,
        load_interview,
        save_interview,
        is_active,
        select_validation_tiles_from_metadata,
        select_tiles_one_per_well,
        select_test_tile_from_metadata,
        list_wells_from_metadata,
        parse_resolution_table,
        select_final_resolutions,
        cluster_target_miss,
        select_initial_sbs_tiles,
        probe_preprocess,
        probe_det_range,
        probe_phenotype_cells_per_tile,
        apply_merge_approach,
        fill_lab_defaults,
        load_notebook_defaults,
        apply_preprocess_probe,
        resolve_phase,
        _load_screen_yaml,
    )
except Exception:  # pragma: no cover
    resolve_all_phases = None  # type: ignore[assignment]
    probe_preprocess = None  # type: ignore[assignment]
    apply_preprocess_probe = None  # type: ignore[assignment]
    resolve_phase = None  # type: ignore[assignment]
    sweep_headless = None  # type: ignore[assignment]
    adaptive_sweep_headless = None  # type: ignore[assignment]
    adaptive_marimo_sweep_multitile = None  # type: ignore[assignment]
    joint_marimo_sweep_multitile = None  # type: ignore[assignment]
    pick_sweep_winner = None  # type: ignore[assignment]
    sweep_evidence = None  # type: ignore[assignment]
    patch_marker_block_value = None  # type: ignore[assignment]
    record_tuned_confirmed = None  # type: ignore[assignment]
    load_interview = None  # type: ignore[assignment]
    save_interview = None  # type: ignore[assignment]
    is_active = None  # type: ignore[assignment]
    select_validation_tiles_from_metadata = None  # type: ignore[assignment]
    select_tiles_one_per_well = None  # type: ignore[assignment]
    select_test_tile_from_metadata = None  # type: ignore[assignment]
    list_wells_from_metadata = None  # type: ignore[assignment]
    parse_resolution_table = None  # type: ignore[assignment]
    select_final_resolutions = None  # type: ignore[assignment]
    cluster_target_miss = None  # type: ignore[assignment]
    select_initial_sbs_tiles = None  # type: ignore[assignment]
    probe_phenotype_cells_per_tile = None  # type: ignore[assignment]
    apply_merge_approach = None  # type: ignore[assignment]

try:
    from brieflow_screen_geometry import probe_geometry  # noqa: E402
except Exception:  # pragma: no cover
    probe_geometry = None  # type: ignore[assignment]

try:
    from brieflow_disk import (  # noqa: E402
        compute_disk_requirements,
        check_disk_available,
    )
except Exception:  # pragma: no cover
    compute_disk_requirements = None  # type: ignore[assignment]
    check_disk_available = None  # type: ignore[assignment]

try:
    from brieflow_runloop import (  # noqa: E402
        run_one_phase,
        phase_tag,
        notify,
        LAST_ESCALATION_NAME,
    )
except Exception:  # pragma: no cover
    run_one_phase = None  # type: ignore[assignment]
    phase_tag = None  # type: ignore[assignment]
    notify = None  # type: ignore[assignment]
    LAST_ESCALATION_NAME = "last_escalation.json"  # type: ignore[assignment]

import atexit  # noqa: E402

try:
    from brieflow_hooks import get_hooks  # noqa: E402
except Exception:  # pragma: no cover
    get_hooks = None  # type: ignore[assignment]

from brieflow_phases import NOTEBOOK_ONLY_PHASES, PHASE_NOTEBOOKS, PHASES, phase_notebook  # noqa: E402
from brieflow_screen_yaml import check as screen_yaml_check  # noqa: E402
from brieflow_validate_tree import validate_screen_inputs  # noqa: E402

try:
    import brieflow_qc as _qc  # noqa: E402

    build_phase_qc = _qc.build_phase_qc
    qc_gate_verdict = _qc.qc_gate_verdict
    load_qc_gates = _qc.load_qc_gates
except Exception:  # pragma: no cover
    _qc = None  # type: ignore[assignment]
    build_phase_qc = None  # type: ignore[assignment]
    qc_gate_verdict = None  # type: ignore[assignment]
    load_qc_gates = None  # type: ignore[assignment]

try:
    from brieflow_decisions import record_decision  # noqa: E402
except Exception:  # pragma: no cover

    def record_decision(*a, **k):  # type: ignore[misc]
        return {}


try:
    from brieflow_manifest import plugin_health, write_run_manifest  # noqa: E402
except Exception:  # pragma: no cover
    write_run_manifest = None  # type: ignore[assignment]
    plugin_health = None  # type: ignore[assignment]

try:
    import brieflow_launch_review as _launch_review  # noqa: E402
    from brieflow_launch_review import (  # noqa: E402
        launch_approved,
        read_prior_review_hash,
        write_launch_review,
    )
except Exception as e:  # pragma: no cover
    # Loud: with the gate unimportable every run would launch unreviewed.
    print(
        f"brieflow_runner: launch review unavailable ({e}) — runs will be REFUSED (rc 8)",
        file=sys.stderr,
    )
    _launch_review = None  # type: ignore[assignment]
    launch_approved = None  # type: ignore[assignment]
    read_prior_review_hash = None  # type: ignore[assignment]
    write_launch_review = None  # type: ignore[assignment]

try:
    from brieflow_compute import probe_compute  # noqa: E402
except Exception:  # pragma: no cover
    probe_compute = None  # type: ignore[assignment]

try:
    from brieflow_sweep_alloc import allocation_needed, in_allocation  # noqa: E402
except Exception:  # pragma: no cover

    def allocation_needed(backend) -> bool:  # type: ignore[misc]
        return False

    in_allocation = None  # type: ignore[assignment]

try:
    from brieflow_issue_draft import issue_route, write_issue_draft  # noqa: E402
except Exception:  # pragma: no cover
    write_issue_draft = None  # type: ignore[assignment]
    issue_route = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Phase notebook paths
# ---------------------------------------------------------------------------

_PHASE_TO_FILE: dict[str, str] = dict(PHASE_NOTEBOOKS)


# ---------------------------------------------------------------------------
# T6: lab_defaults loader + helpers
# ---------------------------------------------------------------------------


def load_lab_defaults(path: str | Path | None = None) -> dict:
    """Load lab_defaults.yaml. Defaults to data/lab_defaults.yaml."""
    p = Path(path) if path else _DEFAULT_LAB_DEFAULTS
    with open(p) as f:
        return yaml.safe_load(f) or {}


def lab_sweep_spec(defaults: dict, phase: str, param: str):
    """Return the raw sweep spec for phase/param: a list (fixed sweep), a dict
    (adaptive coarse-to-fine), or None if absent."""
    bands = defaults.get("sweep_bands") or {}
    phase_bands = bands.get(phase) or {}
    return phase_bands.get(param)


def lab_sweep_band(defaults: dict, phase: str, param: str) -> list:
    """Return the sweep band (list of values) for phase/param.

    For an adaptive dict spec, returns its ``coarse`` list. Returns [] if the
    phase or param is absent from sweep_bands.
    """
    spec = lab_sweep_spec(defaults, phase, param)
    if isinstance(spec, dict):
        return list(spec.get("coarse") or [])
    return list(spec) if spec else []


# ---------------------------------------------------------------------------
# T7: progress.json helpers
# ---------------------------------------------------------------------------


PROGRESS_FILE = "progress.json"
LEGACY_PROGRESS_NAME = "autopilot_progress.json"  # pre-0.5.15 screens; migrated on first read


def progress_path(analysis_dir: Path | str) -> Path:
    """Return the canonical path for progress.json."""
    return Path(analysis_dir) / ".brieflow" / PROGRESS_FILE


def _read_progress(analysis_dir: Path | str) -> dict:
    """Read progress.json; migrate a pre-0.5.15 legacy progress file once."""
    p = progress_path(analysis_dir)
    legacy = p.with_name(LEGACY_PROGRESS_NAME)
    if legacy.exists():
        if p.exists():
            if notify:
                notify(
                    Path(analysis_dir),
                    "runner",
                    f"both {p.name} and {legacy.name} exist; using {p.name}",
                    priority="warning",
                )
        else:
            try:
                os.replace(legacy, p)
            except Exception as e:
                if notify:
                    notify(
                        Path(analysis_dir),
                        "runner",
                        f"progress-file migration failed ({type(e).__name__}: {e}); "
                        f"reading legacy {legacy.name} as-is",
                        priority="warning",
                    )
                p = legacy
            else:
                try:
                    record_decision(
                        analysis_dir,
                        kind="progress-file-migrated",
                        phase="pre-launch",
                        choice=p.name,
                        why=f"renamed legacy {legacy.name}",
                        source="auto",
                    )
                except Exception as e:
                    if notify:
                        notify(
                            Path(analysis_dir),
                            "runner",
                            f"progress-file-migrated decision not recorded "
                            f"({type(e).__name__}: {e})",
                            priority="warning",
                        )
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_progress(analysis_dir: Path | str, data: dict) -> None:
    """Atomic write via tmp + rename."""
    p = progress_path(analysis_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Write to a tmp file in the same directory, then rename atomically.
    tmp_fd, tmp_path = tempfile.mkstemp(dir=p.parent, prefix=".progress_tmp_")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, p)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def mark_phase_done(analysis_dir: Path | str, phase: str) -> None:
    """Record phase as completed in progress.json; a completed phase supersedes any pause."""
    data = _read_progress(analysis_dir)
    done = set(data.get("done_phases") or [])
    done.add(phase)
    data["done_phases"] = sorted(done)
    # The decision journal keeps the pause history; progress.json is current state only.
    data.pop("paused", None)
    _write_progress(analysis_dir, data)


def phase_done(analysis_dir: Path | str, phase: str) -> bool:
    """Return True if phase has been marked as complete."""
    data = _read_progress(analysis_dir)
    return phase in (data.get("done_phases") or [])


def config_digest(analysis_dir: Path | str, config: str | Path) -> str | None:
    """sha256 of the parsed config (key order and formatting do not count), or None."""
    try:
        doc = yaml.safe_load((Path(analysis_dir) / config).read_text())
    except (OSError, yaml.YAMLError):
        return None
    return hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()


def _marker_path(analysis_dir: Path | str, phase: str) -> Path:
    return Path(analysis_dir) / ".brieflow" / "phase_complete" / f"{phase}.json"


def write_phase_marker(analysis_dir: Path | str, phase: str, tag: str, config) -> dict:
    """Record that the phase's pipeline run finished, independent of its QC verdict."""
    marker = {
        "phase": phase,
        "tag": tag,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "config_sha256": config_digest(analysis_dir, config),
    }
    path = _marker_path(analysis_dir, phase)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(marker, indent=2) + "\n")
    return marker


def read_phase_marker(analysis_dir: Path | str, phase: str) -> dict | None:
    try:
        return json.loads(_marker_path(analysis_dir, phase).read_text())
    except (OSError, ValueError):
        return None


def ancient_input_rules(analysis_dir: Path, phase: str) -> list[str]:
    """Rules in the phase's .smk whose input is wrapped in ancient(), so a newer upstream never reruns them."""
    for root in (Path(analysis_dir).parent / "brieflow", Path(analysis_dir) / "brieflow"):
        smk = root / "workflow" / "rules" / f"{phase}.smk"
        if smk.is_file():
            blocks = re.split(r"^(?=rule \w+\s*:)", smk.read_text(), flags=re.MULTILINE)
            return [
                re.match(r"rule (\w+)", b).group(1)
                for b in blocks
                if b.startswith("rule ") and "ancient(" in b.split("output:")[0]
            ]
    return []


def phase_rerun_check(
    analysis_dir: Path, phase: str, config, conda_env: str | None, marker: dict
) -> tuple[bool, str, list[str]]:
    """(reuse, detail, force_rules) for a phase whose run already finished.

    Reuse when the config is unchanged in content and no upstream phase completed after
    it; the outputs are then gated as they are, never resubmitted. Otherwise report what
    Snakemake would rerun and why (dry-run reasons and --list-changes). When an upstream
    phase completed later but nothing is pending, the phase reads it through ancient()
    and force_rules names the rules to --forcerun."""
    stale_upstream = [
        p
        for p in PHASES[: PHASES.index(phase)]
        if (read_phase_marker(analysis_dir, p) or {}).get("completed_at", "")
        > marker.get("completed_at", "")
    ]
    same_config = marker.get("config_sha256") and marker["config_sha256"] == config_digest(
        analysis_dir, config
    )
    if same_config and not stale_upstream:
        return True, (
            f"{phase}: outputs complete from run {marker.get('tag')} with unchanged config — "
            "not resubmitting; gating QC on them"
        ), []
    why = f"upstream {stale_upstream} completed later" if stale_upstream else "config changed"
    try:
        from brieflow_dryrun_preview import rerun_reasons

        found = rerun_reasons(analysis_dir, phase, conda_env, analysis_dir / config)
    except Exception as e:  # reporting only; never blocks the rerun
        found = {"error": f"{type(e).__name__}: {e}"}
    pending = sum((found.get("pending") or {}).values())
    reasons = "; ".join(f"{r} ({len(rules)})" for r, rules in (found.get("reasons") or {}).items())
    changes = ", ".join(
        f"{k} {v['count']}" for k, v in (found.get("list_changes") or {}).items() if v["count"]
    )
    detail = (
        f"{phase}: rerunning a completed phase ({why} since run {marker.get('tag')}): "
        f"{pending} job(s) pending; reasons: {reasons or 'n/a'}; "
        f"--list-changes: {changes or 'none'}"
    )
    if found.get("error"):
        detail += f"; dry-run unavailable ({found['error']})"
    force = []
    if stale_upstream and not pending and not found.get("error"):
        force = ancient_input_rules(analysis_dir, phase)
        if force:
            detail += f"; inputs read through ancient() — forcing {force}"
    return False, detail, force


def record_qc_status(analysis_dir: Path | str, phase: str, summary=None, **flags) -> dict:
    """Keep each phase's QC outcome in .brieflow/qc_status.json: overall status, warn and
    fail metrics, and how a failure was let through (advisory / accepted)."""
    path = Path(analysis_dir) / ".brieflow" / "qc_status.json"
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        doc = {}
    entry = doc.get(phase) or {}
    if summary is not None:
        metrics = getattr(summary, "metrics", None) or []
        entry = {
            "overall": getattr(summary, "overall_status", None),
            "warn": [m.name for m in metrics if getattr(m, "status", None) == "warn"],
            "fail": [m.name for m in metrics if getattr(m, "status", None) == "fail"],
        }
    entry.update(flags, at=datetime.now(timezone.utc).isoformat())
    doc[phase] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n")
    return entry


def qc_review_note(analysis_dir: Path | str) -> str:
    """Phases whose QC was not a clean pass, for the end-of-run message."""
    try:
        doc = json.loads((Path(analysis_dir) / ".brieflow" / "qc_status.json").read_text())
    except (OSError, ValueError):
        return ""
    notes = []
    for phase in PHASES:
        e = doc.get(phase) or {}
        if e.get("fail"):
            how = "accepted" if e.get("accepted") else "advisory" if e.get("advisory") else ""
            notes.append(f"{phase} fail {how} ({', '.join(e['fail'])})".replace("  ", " "))
        elif e.get("warn"):
            notes.append(f"{phase} warn ({', '.join(e['warn'])})")
    return "; ".join(notes)


# ctx key -> brieflow_sweep function run inside the allocation
_SWEEP_FUNCTIONS = {
    "adaptive_sweep_headless": "adaptive_cached_sweep",
    "sweep_headless": "cached_sweep_headless",
    "adaptive_marimo_sweep_multitile": "adaptive_marimo_sweep_multitile",
    "joint_marimo_sweep_multitile": "joint_marimo_sweep_multitile",
}


def sweep_functions(args, analysis_dir: Path, lab: dict, conda_env: str | None, phase: str):
    """The sweep callables for a phase. On a Slurm backend, with lab
    `sweep_tiles.run_in_allocation`, and outside an allocation, each runs through srun on a
    compute node instead of on the host the runner orchestrates from."""
    local = {
        "adaptive_sweep_headless": adaptive_sweep_headless,
        "sweep_headless": sweep_headless,
        "adaptive_marimo_sweep_multitile": adaptive_marimo_sweep_multitile,
        "joint_marimo_sweep_multitile": joint_marimo_sweep_multitile,
    }
    cfg = (lab or {}).get("sweep_tiles") or {}
    if not cfg.get("run_in_allocation") or not allocation_needed(getattr(args, "backend", None)):
        return local
    profile = {}
    try:
        profile = (
            yaml.safe_load((Path(analysis_dir) / "slurm" / "config.yaml").read_text()) or {}
        ).get("default-resources") or {}
    except (OSError, yaml.YAMLError):
        pass
    kernels = int(cfg.get("max_parallel_kernels", 3))
    resources = {
        "job_name": f"brieflow_sweep_{phase}",
        "cpus": int(cfg.get("alloc_cpus_per_kernel", 4)) * kernels,
        "mem_gb": int(cfg.get("alloc_mem_gb_per_kernel", 24)) * kernels,
        "time": str(cfg.get("alloc_time", "8:00:00")),
        "partition": profile.get("slurm_partition"),
        "account": profile.get("slurm_account"),
    }
    return {
        key: in_allocation(_SWEEP_FUNCTIONS[key], resources, conda_env)
        for key, fn in local.items()
        if fn is not None
    }


def input_layout_check(analysis_dir: Path, tag: str, accept_warnings: bool = False) -> int:
    """Stop unless the raw inputs screen.yaml names are in the expected layout.

    Runs on every launch, whatever phases are already done, and again after a lifecycle
    hook pulls the inputs. A blocker or a pending reshape pauses with _RC_DATA_LAYOUT; a
    warning (e.g. rounds of different sizes) pauses too until the operator confirms it
    with --accept-layout, which is journaled.
    """
    try:
        plan = validate_screen_inputs(analysis_dir / "screen.yaml")
    except Exception as e:
        problems = [f"could not check the input layout: {e}"]
        plan = None
    else:
        problems = [f"[{b.code}] {b.message}" for b in plan.blockers]
        problems += [f"not in the expected layout: {o.op} {o.src} ({o.reason})" for o in plan.ops]
    warnings = list(plan.warnings) if plan else []
    if not problems and warnings and accept_warnings:
        record_decision(
            analysis_dir,
            kind="layout-accept",
            phase="pre-launch",
            choice="run on the input data as staged",
            why="operator passed --accept-layout",
            inputs={"warnings": warnings},
            source="operator",
        )
        return 0
    if not problems and not warnings:
        return 0
    lines = problems + [f"confirm: {w}" for w in warnings]
    msg = (
        "runner: the raw input data is not in the layout brieflow reads. "
        + " | ".join(lines)
        + " — stage it with /brieflow-setup (brieflow_validate_tree.py --inputs-from "
        "screen.yaml; --layout prints the expected structure)"
        + ("; if the warnings are expected, rerun with --accept-layout" if not problems else "")
    )
    notify(analysis_dir, tag, msg, priority="action_needed")
    set_paused(analysis_dir, "pre-launch", "input data layout")
    return _RC_DATA_LAYOUT


def accept_qc(analysis_dir: Path, tag: str, phase: str) -> int:
    """Operator accepts a failed QC gate: journal it and mark the phase done, no rerun.

    Requires a finished run of the phase (its phase-complete marker). Returns 0, or the
    template-gap code when there is nothing to accept."""
    marker = read_phase_marker(analysis_dir, phase)
    if phase_done(analysis_dir, phase):
        notify(analysis_dir, tag, f"--accept-qc {phase}: phase already done — nothing to accept")
        return 0
    if not marker:
        msg = (
            f"--accept-qc {phase}: no finished run of {phase} to accept "
            f"(.brieflow/phase_complete/{phase}.json missing) — run the phase first"
        )
        notify(analysis_dir, tag, msg, priority="action_needed")
        return _RC_TEMPLATE_GAP
    paused = _read_progress(analysis_dir).get("paused") or {}
    reason = paused.get("reason") if paused.get("phase") == phase else None
    mark_phase_done(analysis_dir, phase)
    record_qc_status(analysis_dir, phase, accepted=True)
    record_decision(
        analysis_dir,
        kind="qc-accept",
        phase=phase,
        choice="accept the QC result; phase marked done without a rerun",
        why=reason or "operator passed --accept-qc",
        inputs={"marker": marker},
        source="operator",
    )
    notify(
        analysis_dir,
        tag,
        f"{phase}: QC accepted by operator (run {marker.get('tag')}) — marked done, not rerun",
        priority="progress",
    )
    return 0


def _free_raw_input_after_preprocess(analysis_dir: Path, tag: str, gcs_prefix: str) -> None:
    """OUTSTANDING #19: delete the local raw input dirs after preprocess QC passes,
    IFF they are confirmed present under `gcs_prefix` (gs://...). Only preprocess
    reads the raw input; freeing it reclaims screen-size TB before aggregate.

    Opt-in (only called when --free-raw-input-after-preprocess is set) and verified
    (deletes nothing unless `gsutil ls <gcs_prefix>` succeeds). Never raises — a
    cleanup failure must not fail an otherwise-good run.
    """
    try:
        import shutil
        import subprocess
        import yaml

        screen_yaml = Path(analysis_dir) / "screen.yaml"
        screen = yaml.safe_load(screen_yaml.read_text()) if screen_yaml.exists() else {}
        data = (screen or {}).get("data") or {}
        raw_dirs = [data.get("input_sbs_dir"), data.get("input_phenotype_dir")]

        # GCS backup must be reachable before we delete anything local.
        chk = subprocess.run(
            ["gsutil", "ls", gcs_prefix], capture_output=True, text=True, timeout=120
        )
        if chk.returncode != 0:
            notify(
                analysis_dir,
                tag,
                f"free-input: GCS backup {gcs_prefix} not reachable — keeping local raw "
                f"input ({(chk.stderr or '').strip()[:120]})",
                priority="warning",
            )
            return

        freed = []
        for d in raw_dirs:
            if not d:
                continue
            p = Path(d)
            p = p if p.is_absolute() else Path(analysis_dir) / p
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)
                freed.append(str(p))
        if freed:
            notify(
                analysis_dir,
                tag,
                f"free-input: deleted local raw input after preprocess QC "
                f"(GCS-backed at {gcs_prefix}): {', '.join(freed)}",
                priority="progress",
            )
    except Exception as e:
        notify(
            analysis_dir,
            tag,
            f"free-input: skipped ({type(e).__name__}: {e}) — local raw input kept",
            priority="warning",
        )


def refresh_geometry(
    analysis_dir: Path | str, config_path: Path | str, geom: dict | None = None
) -> dict:
    """Persist the probed geometry to .brieflow/geometry.json, which the memory resolver reads.

    Overwrites only when the new probe resolves at least as many n_* fields as the file
    holds, so a partial probe (combo TSVs not written yet) never replaces a complete one."""
    if geom is None:
        if probe_geometry is None:
            return {}
        geom = probe_geometry(Path(analysis_dir), Path(config_path))
    path = Path(analysis_dir) / ".brieflow" / "geometry.json"

    def resolved(g: dict) -> int:
        return sum(1 for k, v in g.items() if k.startswith("n_") and v)

    try:
        old = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        old = {}
    if resolved(geom) and resolved(geom) >= resolved(old):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(geom, indent=2, default=str))
    return geom


def set_paused(analysis_dir: Path | str, phase: str, reason: str) -> None:
    """Record that the runner has paused at phase with the given reason."""
    data = _read_progress(analysis_dir)
    data["paused"] = {
        "phase": phase,
        "reason": reason,
        "paused_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_progress(analysis_dir, data)
    record_decision(
        analysis_dir, kind="blocker-pause", phase=phase, choice="paused", why=reason, source="auto"
    )


# ---------------------------------------------------------------------------
# T8: conductor loop
# ---------------------------------------------------------------------------

#: Return codes for each trigger type
_RC_QC_GATE_FAIL = 1
_RC_RUN_FAIL = 2
_RC_TEMPLATE_GAP = 3
_RC_DISK_UNAVAIL = 4
_RC_NOTEBOOK_FAIL = 5
_RC_PREFLIGHT_FAIL = 6
_RC_UPSTREAM_RERUN = 7
_RC_AWAITING_LAUNCH = 8
_RC_DATA_LAYOUT = 9

#: Static config validator — pure AST+regex+YAML, no brieflow env needed.
PREFLIGHT_PY = SCRIPT_DIR / "brieflow_preflight.py"
DRYRUN_PREVIEW_PY = SCRIPT_DIR / "brieflow_dryrun_preview.py"
BASE_ORDER_PROBE_PY = SCRIPT_DIR / "brieflow_base_order_probe.py"
DYE_BALANCE_PROBE_PY = SCRIPT_DIR / "brieflow_dye_balance_probe.py"


def _conda_env(analysis_dir: Path, screen_yaml_path: Path | None = None) -> str | None:
    """Resolve conda env name from screen.yaml pipeline.conda_env, or None if missing."""
    p = screen_yaml_path or (Path(analysis_dir) / "screen.yaml")
    if not p.exists():
        return None
    try:
        with open(p) as f:
            sy = yaml.safe_load(f) or {}
        return (sy.get("pipeline") or {}).get("conda_env") or None
    except Exception:
        return None


def run_preflight(analysis_dir: Path | str, backend: str = "slurm") -> int:
    """Run the static config preflight (brieflow_preflight.py) and return its
    exit code: 0 = clean (LOW only), nonzero = HIGH-severity config↔rule-code
    issues. Pure AST+regex+YAML, so it runs under the runner's own python
    (no brieflow env). Module-level so the loop test can stub it.

    `backend` is forwarded so the mem-cap check is severity-correct: on local,
    uncovered mem caps are advisory (LOW), not a HIGH submit-blocker."""
    return subprocess.run(
        [
            sys.executable,
            str(PREFLIGHT_PY),
            "--analysis-dir",
            str(analysis_dir),
            "--backend",
            backend,
        ]
    ).returncode


def run_base_order_probe(analysis_dir: Path | str, conda_env: str, nb_path: Path | str) -> dict:
    """Run `brieflow_base_order_probe.py` on the SBS notebook's TEST tile in the
    screen env, writing `.brieflow/base_order_probe.json` for preflight to gate on
    (HIGH on a channel↔base mismatch). Runs after the notebook has written the
    sbs config and before preflight. Lenient like the rerun guard: any failure
    to run returns {"ran": False, ...} and never blocks — the report file is
    what carries the verdict. Module-level so the loop test can stub it."""
    import ast

    if not Path(nb_path).exists():
        return {"ran": False, "detail": f"{Path(nb_path).name} not found"}
    nb_text = Path(nb_path).read_text()
    vals = {}
    for name in ("TEST_PLATE", "TEST_WELL", "TEST_TILE"):
        m = re.search(rf"^\s*{name}\s*=\s*(.+?)\s*(#.*)?$", nb_text, re.MULTILINE)
        if not m:
            return {"ran": False, "detail": f"{name} not found in {Path(nb_path).name}"}
        try:
            vals[name] = ast.literal_eval(m.group(1))
        except Exception:
            return {"ran": False, "detail": f"{name} is not a literal"}
    cmd = (
        f'eval "$(conda shell.bash hook)" && conda activate {conda_env} && '
        f"python {BASE_ORDER_PROBE_PY} --analysis-dir {analysis_dir} "
        f"--plate {vals['TEST_PLATE']} --well {vals['TEST_WELL']} --tile {vals['TEST_TILE']}"
    )
    log_path = Path(analysis_dir) / ".brieflow" / "base_order_probe.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(log_path, "w") as fh:
            proc = subprocess.run(
                ["bash", "-lc", cmd],
                cwd=str(analysis_dir),
                stdout=fh,
                stderr=subprocess.STDOUT,
                timeout=1800,
            )
    except Exception as e:
        return {"ran": False, "detail": f"probe did not complete: {type(e).__name__}"}
    report_path = Path(analysis_dir) / ".brieflow" / "base_order_probe.json"
    if not report_path.exists():
        return {
            "ran": False,
            "detail": f"probe exited {proc.returncode}, no report — see {log_path}",
        }
    report = json.loads(report_path.read_text())
    return {
        "ran": True,
        "verdict": report.get("verdict"),
        "best": report.get("best"),
        "configured": report.get("configured"),
        "suggested_bases": report.get("suggested_bases"),
    }


def run_dye_balance_probe(analysis_dir: Path | str, conda_env: str, nb_path: Path | str) -> dict:
    """Run `brieflow_dye_balance_probe.py` on the SBS notebook's TEST tile and, when a
    candidate wins, write its values back to the notebook markers and the config.

    The probe needs the sbs config the notebook just wrote, so it runs after it. Both
    values feed `call_reads`, which is downstream of everything the notebook computed,
    so applying them here changes no earlier result and the notebook is not re-run —
    but the marker block is patched so the operator-visible artifact matches the config
    that produced the numbers. Lenient like the base-order probe: any failure returns
    {"ran": False, ...} and never blocks.

    Safe to patch the notebook file here: the marimo kernel for this phase is stopped
    by the time the phase's notebook step has returned."""
    vals = _test_tile_markers(nb_path)
    if "error" in vals:
        return {"ran": False, "detail": vals["error"]}
    cmd = (
        f'eval "$(conda shell.bash hook)" && conda activate {conda_env} && '
        f"python {DYE_BALANCE_PROBE_PY} --analysis-dir {analysis_dir} "
        f"--plate {vals['TEST_PLATE']} --well {vals['TEST_WELL']} --tile {vals['TEST_TILE']}"
    )
    log_path = Path(analysis_dir) / ".brieflow" / "dye_balance_probe.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(log_path, "w") as fh:
            proc = subprocess.run(
                ["bash", "-lc", cmd],
                cwd=str(analysis_dir),
                stdout=fh,
                stderr=subprocess.STDOUT,
                timeout=1800,
            )
    except Exception as e:
        return {"ran": False, "detail": f"probe did not complete: {type(e).__name__}"}
    report_path = Path(analysis_dir) / ".brieflow" / "dye_balance_probe.json"
    if not report_path.exists():
        return {
            "ran": False,
            "detail": f"probe exited {proc.returncode}, no report — see {log_path}",
        }
    report = json.loads(report_path.read_text())
    out = {
        "ran": True,
        "verdict": report.get("verdict"),
        "dye_scale": report.get("dye_scale"),
        "brightness_regions": report.get("brightness_regions"),
        "gain": report.get("gain"),
        "applied": False,
    }
    if report.get("verdict") == "improved":
        out["applied"] = apply_dye_balance(analysis_dir, nb_path, report)
    return out


def apply_dye_balance(analysis_dir: Path | str, nb_path: Path | str, report: dict) -> bool:
    """Write the probe's winning values into the notebook markers, the sbs config and
    the interview (provenance `auto-probe`). Returns True when the config was updated."""
    import yaml

    from brieflow_interview import load_interview, save_interview, set_value
    from brieflow_markers import patch_marker_block_value

    analysis_dir = Path(analysis_dir)
    values = {
        "DYE_SCALE": report.get("dye_scale"),
        "BRIGHTNESS_REGIONS": report.get("brightness_regions"),
    }
    for name, value in values.items():
        try:
            patch_marker_block_value(nb_path, name, repr(value))
        except Exception:
            pass  # an older notebook without the marker still gets the config value
    cfg_path = analysis_dir / "config" / "config.yml"
    cfg = yaml.safe_load(cfg_path.read_text())
    combi = dict(cfg.get("sbs", {}).get("combinatorial") or {})
    for key, value in (
        ("dye_scale", values["DYE_SCALE"]),
        ("brightness_regions", values["BRIGHTNESS_REGIONS"]),
    ):
        if value:
            combi[key] = value
        else:
            combi.pop(key, None)
    if not combi:
        return False
    cfg["sbs"]["combinatorial"] = combi
    cfg_path.write_text(yaml.dump(cfg, sort_keys=False))
    iv_path = analysis_dir / ".brieflow" / "interview.json"
    if iv_path.exists():
        interview = load_interview(iv_path)
        for name, value in values.items():
            try:
                set_value(interview, name, value, "auto-probe", notebook="sbs")
            except Exception:
                pass  # a screen whose interview predates these rows keeps its config value
        save_interview(interview, iv_path)
    return True


def _test_tile_markers(nb_path: Path | str) -> dict:
    """TEST_PLATE / TEST_WELL / TEST_TILE literals from a notebook, or {"error": why}."""
    import ast

    if not Path(nb_path).exists():
        return {"error": f"{Path(nb_path).name} not found"}
    nb_text = Path(nb_path).read_text()
    vals = {}
    for name in ("TEST_PLATE", "TEST_WELL", "TEST_TILE"):
        m = re.search(rf"^\s*{name}\s*=\s*(.+?)\s*(#.*)?$", nb_text, re.MULTILINE)
        if not m:
            return {"error": f"{name} not found in {Path(nb_path).name}"}
        try:
            vals[name] = ast.literal_eval(m.group(1))
        except Exception:
            return {"error": f"{name} is not a literal"}
    return vals


def run_upstream_rerun_guard(
    analysis_dir: Path | str, phase: str, conda_env: str | None = None
) -> tuple[bool, str]:
    """Dry-run `phase` (with phase-gates, matching the real flow.sh run) and detect
    whether its DAG would re-run an already-completed UPSTREAM phase.

    Returns (abort, detail). abort=True only when the gated dry-run classifies as
    `upstream_rerun` — i.e. a pending rule belongs to a phase before `phase`, which
    means the phase-gates are missing/stale or upstream state is inconsistent (this
    is exactly the merge→phenotype rerun we hit the hard way).

    Lenient by design: any dry-run error/timeout returns (False, …) so a flaky or
    slow dry-run never blocks a run — the flow.sh phase-gates are the primary
    protection; this guard is defense-in-depth. Module-level so the loop test can stub it."""
    cmd = [
        sys.executable,
        str(DRYRUN_PREVIEW_PY),
        "--analysis-dir",
        str(analysis_dir),
        "--terminal-module",
        phase,
        "--phase-gates",
        "--abort-on",
        "upstream_rerun",
    ]
    if conda_env:
        cmd.extend(["--conda-env", conda_env])  # snakemake lives in the screen env
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception as e:  # TimeoutExpired or spawn failure
        return False, f"guard skipped (dry-run did not complete: {type(e).__name__})"
    result = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("[JSON] "):
            try:
                result = json.loads(line[len("[JSON] ") :])
            except Exception:
                pass
    if result is None:
        # No classification at all: the dry-run itself failed (missing env,
        # snakemake error). That is NOT an upstream rerun — stay lenient, but
        # say why so it is diagnosable (a 2026-08-27 run aborted on an empty detail).
        tail = (proc.stderr or proc.stdout or "").strip()[-300:]
        return (
            False,
            f"guard skipped (dry-run produced no classification, rc={proc.returncode}): {tail}",
        )
    # abort ONLY on the classification we asked for — never on the exit code alone
    return result.get("classification") == "upstream_rerun", result.get("detail", "")


def _webhook_configured() -> bool:
    """True if a Slack webhook is configured (env or ~/.brieflow_env), matching how
    brieflow_notify.sh itself resolves it (the notifier sources ~/.brieflow_env)."""
    if (os.environ.get("BRIEFLOW_SLACK_WEBHOOK") or "").strip():
        return True
    try:
        for line in (Path.home() / ".brieflow_env").read_text().splitlines():
            s = line.strip()
            if s.startswith("export BRIEFLOW_SLACK_WEBHOOK=") or s.startswith(
                "BRIEFLOW_SLACK_WEBHOOK="
            ):
                if s.split("=", 1)[1].strip().strip('"').strip("'"):
                    return True
    except OSError:
        pass
    return False


def set_slack_webhook(url: str, env_path: Path | None = None) -> Path:
    """Persist BRIEFLOW_SLACK_WEBHOOK into ~/.brieflow_env (sourced by
    brieflow_notify.sh) so paging works for this run. Replaces any existing
    line; validates an https:// URL. Returns the env-file path."""
    url = (url or "").strip()
    if not url.startswith("https://"):
        raise ValueError("Slack webhook must be an https:// URL")
    env_path = env_path or (Path.home() / ".brieflow_env")
    kept = []
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            bare = line.strip()
            if bare.startswith("export "):
                bare = bare[len("export ") :]
            if bare.startswith("BRIEFLOW_SLACK_WEBHOOK="):
                continue
            kept.append(line)
    kept.append(f'BRIEFLOW_SLACK_WEBHOOK="{url}"')
    env_path.write_text("\n".join(kept) + "\n")
    return env_path


def notify_selftest(analysis_dir: Path | str, tag: str) -> str:
    """Prove the page path works BEFORE the run depends on it — a dead webhook
    otherwise surfaces only when the first real action_needed page silently fails.

    Fires one progress-tier self-test and reads the notifier's audit log for the
    delivery verdict: 'delivered' / 'failed' / 'no_channel' / 'inconclusive'. Never
    blocks the run (paging may legitimately be off); warns loudly on failure."""
    if not _webhook_configured():
        print(
            "[runner] WARNING: no Slack webhook configured — paging is DISABLED "
            "for this run (set BRIEFLOW_SLACK_WEBHOOK in ~/.brieflow_env).",
            file=sys.stderr,
            flush=True,
        )
        notify(
            analysis_dir,
            tag,
            "paging DISABLED — no Slack webhook configured (set BRIEFLOW_SLACK_WEBHOOK)",
            priority="info",
        )
        return "no_channel"
    state_dir = Path(
        os.environ.get("BRIEFLOW_NOTIFY_STATE_DIR") or (Path.home() / ".cache" / "brieflow_notify")
    )
    audit = state_dir / "notify.log"
    before = audit.stat().st_size if audit.exists() else 0
    notify(
        analysis_dir,
        tag,
        "🔔 runner starting — notification self-test (confirming the page path works)",
        priority="progress",
    )
    verdict = "inconclusive"
    try:
        with open(audit) as f:
            f.seek(before)
            tail = f.read()
        if "sent slack" in tail:
            verdict = "delivered"
        elif "SLACK FAILED" in tail:
            verdict = "failed"
    except OSError:
        pass
    if verdict == "failed":
        print(
            "[runner] WARNING: notification self-test FAILED to deliver to Slack "
            "— the operator may NOT be reachable for action-needed pages.",
            file=sys.stderr,
            flush=True,
        )
    elif verdict == "delivered":
        print("[runner] notification self-test ✓ — Slack page path confirmed.", flush=True)
    return verdict


def run_phase_notebook(
    analysis_dir: Path,
    phase: str,
    conda_env: str,
    timeout_s: int = 3600,
) -> dict:
    """Run the phase notebook as a plain Python script to flush config to disk.

    Returns:
        {"ok": bool, "returncode": int, "log": Path}
    """
    nb_path = phase_notebook(analysis_dir, phase)
    log_dir = Path(analysis_dir) / ".brieflow"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{phase}.notebook.log"

    cmd = f'eval "$(conda shell.bash hook)" && conda activate {conda_env} && python {nb_path}'
    try:
        with open(log_path, "w") as log_fh:
            result = subprocess.run(
                ["bash", "-lc", cmd],
                cwd=str(analysis_dir),
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                timeout=timeout_s,
            )
        ok = result.returncode == 0
        # Best-effort HTML export (do not fail the phase on export error)
        if ok:
            try:
                html_path = log_dir / f"{phase}.final.html"
                html_cmd = (
                    f'eval "$(conda shell.bash hook)" && conda activate {conda_env} && '
                    f"marimo export html-wasm {nb_path} --output {html_path}"
                )
                subprocess.run(
                    ["bash", "-lc", html_cmd],
                    cwd=str(analysis_dir),
                    capture_output=True,
                    timeout=300,
                )
            except Exception:
                pass
        return {"ok": ok, "returncode": result.returncode, "log": log_path}
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": -1, "log": log_path}
    except Exception as exc:
        try:
            log_path.write_text(f"run_phase_notebook error: {exc}\n")
        except Exception:
            pass
        return {"ok": False, "returncode": -1, "log": log_path}


def resolve_cluster_final(analysis_dir, conda_env, lab, iv, iv_path) -> list:
    """Resolve FINAL_LEIDEN_RESOLUTIONS for the cluster phase (agent-resolved).

    Runs the cluster notebook once so its TEST_LEIDEN_RESOLUTIONS sweep prints
    the per-resolution table, parses it, keeps the resolutions whose median
    genes/cluster lands near cluster_selection.target_genes_per_cluster, patches
    the FINAL_LEIDEN_RESOLUTIONS marker, and records provenance. The caller then
    re-runs the notebook (N1) so the patched FINAL writes the cluster_combo.

    The notebook stays hand-craftable — selection lives in the agent, not the
    notebook. Returns the selected resolutions, or [] if no table was parsed.
    NOTE: this runs the test sweep once here and again in the N1 pass; that
    double cost is the price of keeping selection out of the notebook.
    """
    nb_path = phase_notebook(analysis_dir, "cluster")
    pre = run_phase_notebook(analysis_dir, "cluster", conda_env)
    log = pre.get("log")
    text = Path(log).read_text() if log and Path(log).exists() else ""
    rows = parse_resolution_table(text)
    sel = lab.get("cluster_selection") or {}
    target = int(sel.get("target_genes_per_cluster", 20))
    tol = int(sel.get("genes_per_cluster_tolerance", 5))
    final = select_final_resolutions(rows, target=target, tolerance=tol)
    if final:
        patch_marker_block_value(nb_path, "FINAL_LEIDEN_RESOLUTIONS", repr(final))
        record_tuned_confirmed(iv, "FINAL_LEIDEN_RESOLUTIONS", final, notebook="cluster")
        miss = cluster_target_miss(rows, target=target, tolerance=tol)
        if miss:
            # A small library cannot reach the target: say so instead of calling it a win.
            short = "smaller" if miss["largest_median"] < target - tol else "larger"
            why = (
                f"no tested resolution ({miss['tested'][0]}-{miss['tested'][1]}) reached "
                f"{target}±{tol} genes/cluster; closest: resolution "
                f"{miss['closest_resolution']} at a median of {miss['closest_median']} — every "
                f"tested resolution gives {short} clusters (library-limited); kept the "
                "resolutions around the closest. Set FINAL_LEIDEN_RESOLUTIONS or "
                "cluster_selection.target_genes_per_cluster to choose otherwise"
            )
            print(f"cluster: target not reached — {why}")
        record_decision(
            analysis_dir,
            kind="sweep-target-missed" if miss else "sweep-winner",
            phase="cluster",
            choice=f"FINAL_LEIDEN_RESOLUTIONS={final}",
            why=why if miss else f"median genes/cluster within target {target}±{tol}",
            inputs={"target": target, "tolerance": tol, **({"miss": miss} if miss else {})},
            source="auto",
        )
        save_interview(iv, iv_path)
    return final


def _find_iv_row(iv, param_name, notebook):
    target = param_name.lower()
    for r in iv.get("params", []):
        if r.get("notebook") == notebook and (r.get("param_name") or "").lower() == target:
            return r
    return None


def _merge_positions_on(iv) -> bool:
    """True when the merge interview selects the positions approach (no anchors)."""
    row = _find_iv_row(iv, "merge_approach", "merge")
    return bool(row and row.get("value") == "positions")


def _merge_anchors_unresolved(analysis_dir, tag) -> bool:
    """True when merge runs in auto mode with no INITIAL_SBS_TILES to iterate."""
    try:
        iv = load_interview(analysis_dir)
        row = _find_iv_row(iv, "initial_sbs_tiles", "merge")
    except Exception:
        return False
    if row is None or not is_active(row, iv) or _merge_positions_on(iv):
        return False
    return not row.get("value")


def resolve_merge_approach(analysis_dir, lab, iv, iv_path) -> dict | None:
    """MERGE_APPROACH from the measured phenotype cells per tile, which now exist.

    Re-derives the launch-time value (screen.yaml objective or default) from the
    phenotype outputs; an operator value is kept. When the approach flips, the rows it
    newly activates are lab-filled and the change is journaled. Returns the decision.
    """
    screen = _load_screen_yaml(Path(analysis_dir) / "screen.yaml")
    density = probe_phenotype_cells_per_tile(analysis_dir)
    decision = apply_merge_approach(
        iv, screen, density.get("median_cells_per_tile"), lab=lab or None
    )
    if decision is None:
        return None
    if decision["changed"]:
        fill_lab_defaults(iv, load_notebook_defaults())
        record_decision(
            analysis_dir,
            kind="merge-approach",
            phase="merge",
            choice=f"MERGE_APPROACH={decision['approach']}",
            why=decision["reason"],
            inputs={k: v for k, v in density.items() if k != "notes"},
            source="auto",
        )
    save_interview(iv, iv_path)
    return decision


def resolve_merge_initial_sbs_tiles(analysis_dir, lab, iv, iv_path) -> list:
    """Merge fast+auto: probe well-spread SBS tiles for INITIAL_SBS_TILES (the
    alignment anchors) from SBS tile geometry and patch the marker. The pipeline
    then auto-pairs each to its closest phenotype tile.

    Skips (returns []) when: the row is inactive (manual approach), the positions
    approach is on, or the operator already set a value (override wins). No metadata ->
    [] and the caller leaves the marker for the operator.
    """
    row = _find_iv_row(iv, "initial_sbs_tiles", "merge")
    if row is None or not is_active(row, iv):
        return []
    if row.get("value") is not None:  # operator override wins
        return list(row["value"])
    if _merge_positions_on(iv):
        return []
    n = int((lab.get("merge_alignment") or {}).get("n_initial_sbs_tiles", 20))
    res = select_initial_sbs_tiles(analysis_dir, n_tiles=n)
    tiles = res.get("tiles") or []
    if tiles:
        nb_path = phase_notebook(analysis_dir, "merge")
        patch_marker_block_value(nb_path, "INITIAL_SBS_TILES", repr(tiles))
        record_tuned_confirmed(iv, "INITIAL_SBS_TILES", tiles, notebook="merge")
        default_why = f"{len(tiles)} tiles farthest-point-sampled for max spatial spread"
        record_decision(
            analysis_dir,
            kind="sweep-winner",
            phase="merge",
            choice=f"INITIAL_SBS_TILES={tiles}",
            why=res.get("rationale") or default_why,
            inputs={"n_tiles": n, "plate": res.get("plate"), "well": res.get("well")},
            source="auto",
        )
        save_interview(iv, iv_path)
    return tiles


def resolve_merge_det_range(analysis_dir, lab, iv, iv_path) -> list:
    """Merge DET_RANGE: probe the ph/sbs pixel-size ratio and patch the marker.

    Returns [] when the row is inactive (positions approach) or the pixel sizes can't be
    read. On that second path the row is deliberately left unset, so it still counts
    as an operator gap, and the probe's reason is recorded on the row and in the
    decision journal rather than discarded.
    """
    row = _find_iv_row(iv, "DET_RANGE", "merge")
    if row is None or not is_active(row, iv):
        return []
    if row.get("value") is not None:  # operator override wins
        return list(row["value"])
    screen = _load_screen_yaml(Path(analysis_dir) / "screen.yaml")
    probe_result = probe_det_range(analysis_dir, screen) or {}
    det = probe_result.get("DET_RANGE")
    note = (probe_result.get("notes") or [None])[0]

    if not det:
        # The probe read no pixel size on one or both sides. Leave value and
        # value_source unset so the row still surfaces as an operator gap, but carry
        # the reason onto the row and into the journal. Without this the note is
        # authored above and dropped, and merge dies several cells deep on an unset
        # DET_RANGE with nothing in decisions.jsonl and no gap explaining why.
        why = note or "DET_RANGE probe could not read a pixel size on both sides"
        existing = (row.get("notes") or "").strip()
        if note and note not in existing:
            row["notes"] = f"{existing}\n{note}".strip() if existing else note
        record_decision(
            analysis_dir,
            kind="operator-gap",
            phase="merge",
            choice="DET_RANGE left for operator",
            why=why,
            inputs={"probe_notes": probe_result.get("notes") or []},
            source="auto",
        )
        save_interview(iv, iv_path)
        return []

    nb_path = phase_notebook(analysis_dir, "merge")
    patch_marker_block_value(nb_path, "DET_RANGE", repr(det))
    record_tuned_confirmed(iv, "DET_RANGE", det, notebook="merge")
    why = note or f"DET_RANGE={det} from the ph/sbs pixel-size ratio"
    record_decision(
        analysis_dir,
        kind="sweep-winner",
        phase="merge",
        choice=f"DET_RANGE={det}",
        why=why,
        inputs={"det_range": det},
        source="auto",
    )
    save_interview(iv, iv_path)
    return det


# ---------------------------------------------------------------------------
# Branch-and-converge SBS — per-plate config divergence
#
# Multi-plate screens where plates need DIFFERENT SBS config (e.g. jannu:
# plate_1 skips its failed cycle 1, plate_2 skips nothing) can't share one
# config — skip_cycles_indices / manual_background_cycle_index are global
# scalars. The manual procedure runs SBS once per plate against its own
# config_p<N>.yml, restricted via plate_filter, then converges at phenotype.
# These helpers let the runner drive that natively: declare per-plate overrides
# in screen.yaml `iss.plate_overrides`, and the SBS phase branches over plates.
# ---------------------------------------------------------------------------


def load_plate_overrides(analysis_dir: Path | str) -> dict[int, dict]:
    """Read `iss.plate_overrides` from screen.yaml → {plate_int: {override...}}.

    Each override may carry `skip_cycles` (list of cycle NUMBERS to drop for that
    plate) and/or `manual_background_cycle` (cycle NUMBER). Empty dict when the
    block is absent — i.e. no branching, the normal single-config SBS run.
    """
    sy = Path(analysis_dir) / "screen.yaml"
    if not sy.exists():
        return {}
    try:
        doc = yaml.safe_load(sy.read_text()) or {}
    except Exception:
        return {}
    raw = ((doc.get("iss") or {}).get("plate_overrides")) or {}
    out: dict[int, dict] = {}
    for k, v in raw.items():
        try:
            out[int(k)] = dict(v or {})
        except (TypeError, ValueError):
            continue
    return out


def _sbs_samples_fp(analysis_dir: Path, base_config_rel: str) -> Path:
    """Resolve the SBS samples TSV path (config.preprocess.sbs_samples_fp), with a
    config/sbs_samples.tsv fallback. Paths are relative to analysis_dir."""
    ad = Path(analysis_dir)
    cfg_fp = ad / base_config_rel
    rel = "config/sbs_samples.tsv"
    try:
        cfg = yaml.safe_load(cfg_fp.read_text()) or {}
        rel = (cfg.get("preprocess") or {}).get("sbs_samples_fp") or rel
    except Exception:
        pass
    p = Path(rel)
    return p if p.is_absolute() else ad / p


def _read_sbs_plate_cycles(samples_fp: Path) -> dict[int, list[int]]:
    """{plate: sorted distinct cycle numbers} from the SBS samples TSV."""
    out: dict[int, set] = {}
    if not Path(samples_fp).exists():
        return {}
    with open(samples_fp, newline="") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            try:
                out.setdefault(int(row["plate"]), set()).add(int(row["cycle"]))
            except (KeyError, TypeError, ValueError):
                continue
    return {p: sorted(cyc) for p, cyc in out.items()}


def _write_plate_barcode_library(
    analysis_dir: Path,
    base_sbs: dict,
    plate: int,
    cycles: list[int],
    skip_cycles: list[int] | None,
) -> str | None:
    """Write a per-plate barcode library trimmed to the cycles this plate sequenced.

    The design library's `prefix` column is the full-length mapping barcode — one
    base per design cycle, position ``i`` == cycle ``i+1``. A plate that skips a
    cycle (or sequences fewer cycles than the design) reads only a SUBSET of those
    positions, so its called barcodes are shorter and — for a front/interior skip —
    frame-shifted. ``call_cells`` matches the library ``prefix`` as-is (no
    re-truncation), so the library must be cut to exactly the positions this plate
    sequenced or **nothing maps** (0% — surfaces late as ``eval_mapping`` crashing
    on an empty summary).

    Selects positions ``[c-1 for c in sequenced_cycles]`` from the design prefix.
    Returns the relative path of the per-plate library, or ``None`` when no trim is
    needed (plate sequences the full design in order) or the library isn't
    prefix-based — in which case the base library stands.
    """
    design_rel = (base_sbs or {}).get("df_barcode_library_fp")
    if not design_rel:
        return None
    design_fp = analysis_dir / design_rel
    if not design_fp.exists():
        return None
    sequenced = [c for c in sorted(cycles) if c not in set(skip_cycles or [])]
    idx = [c - 1 for c in sequenced]
    with open(design_fp, newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        fields = reader.fieldnames or []
        rows = list(reader)
    if "prefix" not in fields or not rows:
        return None  # non-prefix (barcode_col) library — leave base untouched
    design_len = len(str(rows[0]["prefix"]))
    if any(i >= design_len for i in idx):
        raise ValueError(
            f"plate {plate}: sequenced cycle {max(sequenced)} exceeds the design "
            f"prefix length {design_len} (df_barcode_library_fp={design_rel}); the "
            f"library is too short for this plate's cycles"
        )
    if idx == list(range(design_len)):
        return None  # full design, in order — no per-plate trim needed
    for r in rows:
        p = str(r["prefix"])
        r["prefix"] = "".join(p[i] for i in idx if i < len(p))
    rel = f"config/barcode_library_p{plate}.tsv"
    with open(analysis_dir / rel, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter="\t")
        w.writeheader()
        w.writerows(rows)
    return rel


def generate_plate_configs(
    analysis_dir: Path | str,
    base_config_rel: str,
    plate_overrides: dict[int, dict],
    plate_cycles: dict[int, list[int]],
) -> dict[int, str]:
    """Write config/config_p<N>.yml for every plate in `plate_cycles`.

    Each variant is a deep copy of the base config with the SBS per-plate knobs
    set: `skip_cycles_indices` (indices INTO that plate's sorted cycle list,
    converted from the override's cycle NUMBERS) and, if given,
    `manual_background_cycle_index`. Plates with no override get
    `skip_cycles_indices=None` so every plate runs through the same single-plate
    path uniformly. Returns {plate: relative config path}.

    Raises ValueError if an override names a cycle the plate doesn't have — a
    misconfiguration we want surfaced loudly, not silently dropped.
    """
    ad = Path(analysis_dir)
    base = yaml.safe_load((ad / base_config_rel).read_text()) or {}
    out: dict[int, str] = {}
    for plate in sorted(plate_cycles):
        cycles = plate_cycles[plate]
        cfg = copy.deepcopy(base)
        sbs = cfg.setdefault("sbs", {})
        ov = plate_overrides.get(plate) or {}

        skip_cycles = ov.get("skip_cycles")
        if skip_cycles:
            missing = [c for c in skip_cycles if c not in cycles]
            if missing:
                raise ValueError(
                    f"plate {plate}: skip_cycles {missing} not present in this "
                    f"plate's cycles {cycles}"
                )
            sbs["skip_cycles_indices"] = [cycles.index(c) for c in skip_cycles]
        else:
            sbs["skip_cycles_indices"] = None

        mbc = ov.get("manual_background_cycle")
        if mbc is not None:
            if mbc not in cycles:
                raise ValueError(
                    f"plate {plate}: manual_background_cycle {mbc} not present in "
                    f"this plate's cycles {cycles}"
                )
            sbs["manual_background_cycle_index"] = cycles.index(mbc)

        # Branch-and-converge's other half: a plate that drops cycles also needs its
        # barcode library trimmed to the cycles it actually sequenced, or it maps 0%.
        # Wire a per-plate library when trimming is required (else the base stands).
        lib_rel = _write_plate_barcode_library(ad, base.get("sbs"), plate, cycles, skip_cycles)
        if lib_rel:
            sbs["df_barcode_library_fp"] = lib_rel

        rel = f"config/config_p{plate}.yml"
        (ad / rel).write_text(yaml.safe_dump(cfg, sort_keys=False))
        out[plate] = rel
    return out


def run_sbs_branched(
    args, analysis_dir, plate_overrides, tag, base_notes
) -> tuple[int, int | None]:
    """Run the SBS phase once per plate against per-plate configs (branch half of
    branch-and-converge). Returns (rc, failed_plate). rc=0 on all-plates success.

    Generates config_p<N>.yml from the base config (already written by the phase
    notebook), then for each plate runs `flow.sh sbs --only-plate N --configfile
    config/config_p<N>.yml` via run_one_phase. SBS outputs are {plate}-keyed so the
    plates coexist; phenotype/merge then converge over the union (base config).
    args.config / args.only_plate are saved and restored around the loop.
    """
    ad = Path(analysis_dir)
    base_config = args.config
    samples_fp = _sbs_samples_fp(ad, base_config)
    plate_cycles = _read_sbs_plate_cycles(samples_fp)
    if not plate_cycles:
        notify(ad, tag, f"sbs branch: no plates found in {samples_fp} — cannot branch")
        return _RC_NOTEBOOK_FAIL, None

    cfgs = generate_plate_configs(ad, base_config, plate_overrides, plate_cycles)
    saved_config = args.config
    saved_only = getattr(args, "only_plate", None)
    plates = sorted(plate_cycles)
    try:
        for idx, plate in enumerate(plates):
            ov = plate_overrides.get(plate) or {}
            args.config = cfgs[plate]
            args.only_plate = plate
            notify(
                ad,
                tag,
                f"sbs branch: plate {plate} ({idx + 1}/{len(plates)}) via {cfgs[plate]} "
                f"(skip_cycles={ov.get('skip_cycles') or 'none'})",
                priority="progress",
            )
            ptag = phase_tag(tag, idx, ["sbs"])
            rc = run_one_phase(args, ["sbs"], ptag, f"{base_notes} (plate {plate})")
            if rc != 0:
                return rc, plate
        return 0, None
    finally:
        args.config = saved_config
        args.only_plate = saved_only


_RANGE_SKIP_KIND = "range-skip"


def range_bound_source(args) -> str | None:
    """Name what bounds the range's last phase — "--only", "--through" or
    "BRIEFLOW_STOP_AFTER" — or None when nothing does (the range runs to the end)."""
    if getattr(args, "only", None):
        return "--only"
    if getattr(args, "through", None):
        return "--through"
    if (os.environ.get("BRIEFLOW_STOP_AFTER") or "").strip():
        return "BRIEFLOW_STOP_AFTER"
    return None


def resolve_phase_range(args) -> list[str]:
    """Slice PHASES by --only / --from / --through, falling back to BRIEFLOW_STOP_AFTER.

    Raises ValueError naming the offending flag for an unknown phase, or for a range
    that slices to nothing (--from after the bound). argparse already validates the
    flag values, so in practice only the env var reaches the membership check — but a
    bad value must surface as a named error, not as an IndexError on the empty slice.
    """
    only = getattr(args, "only", None)
    first = getattr(args, "from_phase", None)
    last = (
        only
        or getattr(args, "through", None)
        or (os.environ.get("BRIEFLOW_STOP_AFTER") or "").strip()
        or None
    )
    for value, source in ((only, "--only"), (first, "--from"), (last, range_bound_source(args))):
        if value and value not in PHASES:
            raise ValueError(f"{source}={value!r} is not a phase; expected one of {PHASES}")
    if only:
        return [only]
    lo = PHASES.index(first) if first else 0
    hi = PHASES.index(last) + 1 if last else len(PHASES)
    sliced = PHASES[lo:hi]
    if not sliced:
        raise ValueError(
            f"empty phase range: --from {first} is after {range_bound_source(args)} {last}"
        )
    return sliced


def _phase_range_or_all(args) -> list[str]:
    """The range this launch would run, or the whole pipeline when the flags do not
    resolve. The gap review is written before the range is validated, and a bad range
    is its own return further down — not this one's to raise."""
    try:
        return resolve_phase_range(args)
    except ValueError:
        return list(PHASES)


@traced
def run_screen(args) -> int:
    """Drive all phases autonomously.

    Args:
        args: argparse.Namespace with analysis_dir, tag, and all fields
              needed by run_one_phase (config, backend, jobs, array_limit,
              latency_wait, cpus_per_task, mem_recs, max_attempts,
              no_aggregate_efficiency, require_rerun, forcerun,
              oom_cascade_threshold, oom_poll_seconds, disk_warn_pct,
              disk_abort_pct).

    Returns:
        0 on full completion.
        1 (trigger 1) if QC gate fails for a phase.
        2 (trigger 2) if run_one_phase returns nonzero, or if the
          on_pre_phase_1 hook raises (inputs never arrived).
        3 (trigger 3) if the run cannot be launched as asked: template gaps
          from resolve_all_phases, a missing pipeline.conda_env, an
          unresolvable --from/--through/--only range, an unknown phase in
          BRIEFLOW_QC_ADVISORY, a phase resolver that paused, or merge anchors
          that stayed unresolved.
        4 (trigger 4) if disk availability check fails.
        5 (trigger 5) if a phase notebook run fails (config not written).
        6 (trigger 6) if preflight finds HIGH-severity config issues.
        7 (trigger 7) if the upstream-rerun guard detects a downstream phase
          would re-run a completed upstream phase.
        8 (awaiting launch) if the launch review was written and --approved was
          not given, or if the launch review is unimportable (the gate fails closed).
        9 (data layout) if the raw inputs screen.yaml names are not in the expected
          layout, checked before launch or, when a hook pulls them, after the pull.
    """
    analysis_dir = Path(args.analysis_dir)
    tag = args.tag or f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    # Lifecycle hooks — data movement around the run (no-ops on slurm/NFS, GCS
    # pull/push on a cloud VM). Registered before the pre-launch checks so teardown
    # runs whatever the run returns; the other three bracket the phase loop only, so
    # a pre-launch return never pushes outputs or archives a failure that never ran.
    hooks = get_hooks(getattr(args, "lifecycle_hooks", "noop") or "noop")
    screen_id = getattr(args, "screen_id", None) or analysis_dir.name
    atexit.register(lambda: _safe(hooks.on_teardown, analysis_dir, screen_id))

    # Page-path self-test: confirm the operator is actually reachable BEFORE the run
    # starts depending on it. A broken/unset webhook otherwise surfaces only when the
    # first real action_needed page silently fails. Never blocks — warns loudly.
    notify_selftest(analysis_dir, tag)
    if plugin_health is not None:
        for problem in _safe_list(plugin_health):
            notify(analysis_dir, tag, f"plugin install: {problem}", priority="warning")

    # Operator accepts a failed QC gate: journaled + marked done before the launch gate,
    # which it does not change (no new review needed to continue).
    if getattr(args, "accept_qc", None):
        rc = accept_qc(analysis_dir, tag, args.accept_qc)
        if rc:
            return rc

    brieflow_output = analysis_dir / "brieflow_output"
    # Use brieflow_output for the disk check when it exists; fall back to
    # analysis_dir (same mount/filesystem) on fresh screens where brieflow_output
    # hasn't been created yet — shutil.disk_usage raises OSError on missing paths.
    disk_check_path = brieflow_output if brieflow_output.exists() else analysis_dir

    # --- Step -1: screen.yaml carries the operator's experimental facts ---
    sy_report = screen_yaml_check(analysis_dir)
    if not sy_report["parses"] or sy_report["missing"]:
        what = sy_report.get("error") or ", ".join(sy_report["missing"])
        msg = (
            f"runner: screen.yaml is not filled in ({what}). The operator writes screen.yaml; "
            "fill these fields (or stage your own with brieflow_screen_yaml.py stage) and rerun."
        )
        notify(analysis_dir, tag, msg, priority="action_needed")
        set_paused(analysis_dir, "pre-launch", f"screen.yaml incomplete: {what}")
        return _RC_TEMPLATE_GAP

    # --- Step -0.5: raw input data in the expected layout (inputs a hook pulls are
    # checked after the pull instead) ---
    if (getattr(args, "lifecycle_hooks", "noop") or "noop") == "noop":
        rc = input_layout_check(analysis_dir, tag, getattr(args, "accept_layout", False))
        if rc:
            return rc

    # --- Step 0: preprocess data probe (BEFORE the gap check) ---
    # Inspect the staged raw data and prefill the data-derivable preprocess rows
    # (format / organization / channel_order / ...) so they aren't flagged as
    # operator gaps below. Rows the probe can't resolve (tiff channels, no data)
    # stay unfilled -> surface as gaps -> operator. Best-effort; never blocks.
    iv_path = analysis_dir / ".brieflow" / "interview.json"
    if probe_preprocess and apply_preprocess_probe and iv_path.exists():
        try:
            screen_yaml = analysis_dir / "screen.yaml"
            screen = yaml.safe_load(screen_yaml.read_text()) if screen_yaml.exists() else {}
            if screen:
                iv0 = load_interview(iv_path)
                report = probe_preprocess(analysis_dir, screen)
                if apply_preprocess_probe(iv0, report):
                    save_interview(iv0, iv_path)
                for note in report.get("notes", []):
                    notify(analysis_dir, tag, f"preprocess probe: {note}")
        except Exception as e:  # never let the probe block a run
            notify(analysis_dir, tag, f"preprocess probe skipped ({e})")

    # --- Step 1: resolve all phases (up-front interview check) ---
    res = resolve_all_phases(analysis_dir)
    if res["gaps"]:
        gap_names = [g["param_name"] for g in res["gaps"]]
        msg = (
            f"runner: template gaps detected — operator input required for: "
            f"{', '.join(gap_names)}. "
            f"Run /brieflow-notebook to fill these before running."
        )
        notify(analysis_dir, tag, msg, priority="action_needed")
        set_paused(analysis_dir, "pre-launch", f"template gaps: {', '.join(gap_names)}")
        # The review is the only place the gap list is written down for the operator;
        # a notify scrolls away. Best-effort: the gap is the finding, and a review that
        # cannot be written must not turn it into a traceback or a different rc.
        if write_launch_review:
            try:
                write_launch_review(
                    analysis_dir,
                    args,
                    tag,
                    interview=(load_interview(iv_path) if iv_path.exists() else {"params": []}),
                    phases=_phase_range_or_all(args),
                    geometry={},
                    disk={},
                    disk_check_path=disk_check_path,
                    compute={"backend": args.backend},
                    lab=load_lab_defaults(),
                    gaps=res["gaps"],
                )
            except Exception as e:
                notify(analysis_dir, tag, f"launch review not written: {e}")
        return _RC_TEMPLATE_GAP

    # --- Step 2: load lab defaults, probe geometry, compute disk ---
    lab = load_lab_defaults()
    geom = probe_geometry(analysis_dir, analysis_dir / args.config)
    refresh_geometry(analysis_dir, analysis_dir / args.config, geom)
    disk = compute_disk_requirements(geom)

    # Load interview for record_tuned_confirmed calls
    iv_path = analysis_dir / ".brieflow" / "interview.json"
    iv = load_interview(iv_path) if iv_path.exists() else {"params": []}

    conda_env = _conda_env(analysis_dir)
    if conda_env is None:
        msg = (
            "runner: pipeline.conda_env not found in screen.yaml — "
            "operator must set this before the runner can run notebooks."
        )
        notify(analysis_dir, tag, msg, priority="action_needed")
        set_paused(analysis_dir, "pre-launch", "missing_conda_env")
        return _RC_TEMPLATE_GAP

    # --- Step 3: phase range ---
    # --only / --from / --through (BRIEFLOW_STOP_AFTER is the --through fallback)
    # bound the chain when the deliverable ends before the last phase (e.g. a QC
    # hand-back that needs merge but not aggregate/cluster). An unknown env name is
    # a hard error, not a silent full run.
    try:
        phases = resolve_phase_range(args)
    except ValueError as e:
        notify(analysis_dir, tag, str(e), priority="action_needed")
        set_paused(analysis_dir, "pre-launch", str(e))
        return _RC_TEMPLATE_GAP
    # BRIEFLOW_QC_ADVISORY lists phases whose gate failures are reported but do not pause.
    # For a gate measuring something the operator has decided to report rather than fix
    # (a density-limited retention, say) pausing only blocks the phases that come after it.
    # The QC summary is still computed, still written and still notified.
    advisory = {
        p.strip() for p in (os.environ.get("BRIEFLOW_QC_ADVISORY") or "").split(",") if p.strip()
    }
    unknown = advisory - set(PHASES)
    if unknown:
        msg = f"BRIEFLOW_QC_ADVISORY names unknown phase(s) {sorted(unknown)}; expected {PHASES}"
        notify(analysis_dir, tag, msg, priority="action_needed")
        set_paused(analysis_dir, "pre-launch", msg)
        return _RC_TEMPLATE_GAP

    # --- Step 4: run the range, bracketed by the pre/post hooks ---
    if write_run_manifest:
        write_run_manifest(analysis_dir, args, tag)
    # The launch gate: everything the run is about to do, written where the operator
    # can read it (launch_review.html) BEFORE any compute is spent. An unapproved run
    # stops here — not a failure, so no on_failure, no issue draft, teardown only.
    if write_launch_review:
        iv_now = load_interview(iv_path) if iv_path.exists() else {"params": []}
        # Read what a PREVIOUS invocation wrote before overwriting it — that file is
        # the only evidence the operator has seen this run's parameters.
        prior_hash = read_prior_review_hash(analysis_dir)
        review = write_launch_review(
            analysis_dir,
            args,
            tag,
            interview=iv_now,
            phases=phases,
            geometry=geom,
            disk=disk,
            disk_check_path=disk_check_path,
            compute=(probe_compute() if probe_compute else {"backend": args.backend}),
            lab=lab,
            gaps=[],
        )
        if not launch_approved(args, review, prior_hash):
            notify(
                analysis_dir,
                tag,
                f"LAUNCH REVIEW written: {analysis_dir / '.brieflow' / 'launch_review.html'} "
                f"— re-run with --approved to start",
                priority="action_needed",
            )
            return _RC_AWAITING_LAUNCH
        record_decision(
            analysis_dir,
            kind="launch-approved",
            phase=phases[0],
            choice="launch as reviewed",
            why="operator passed --approved against a matching launch_review.json",
            inputs={"review_sha256": review["review_sha256"]},
            source="operator",
        )
        # Journalled beside the approval, not before it: a launch that stopped at the
        # gate never started, so it skipped nothing.
        if phases[0] != PHASES[0]:
            skipped = PHASES[: PHASES.index(phases[0])]
            record_decision(
                analysis_dir,
                kind=_RANGE_SKIP_KIND,
                phase=phases[0],
                choice=f"start at {phases[0]}",
                why=f"--from/--only: not running {skipped}",
                inputs={"skipped": skipped},
                source="auto",
            )
    else:
        # No gate, no run: an unreviewable launch is refused, not waved through.
        notify(
            analysis_dir,
            tag,
            "runner: launch review unavailable; refusing to run ungated",
            priority="action_needed",
        )
        return _RC_AWAITING_LAUNCH
    try:
        hooks.on_pre_phase_1(analysis_dir, screen_id)
    except Exception as e:
        msg = f"on_pre_phase_1 hook FAILED: {e} — aborting (can't proceed without inputs)"
        notify(analysis_dir, tag, msg, priority="action_needed")
        set_paused(analysis_dir, "pre-launch", msg)
        # Unlike the operator gaps that return above this, inputs that never arrived
        # are a defect — draft it.
        _safe(_draft_issue, analysis_dir, tag, _RC_RUN_FAIL)
        return _RC_RUN_FAIL
    if (getattr(args, "lifecycle_hooks", "noop") or "noop") != "noop":
        rc = input_layout_check(analysis_dir, tag, getattr(args, "accept_layout", False))
        if rc:
            return rc

    rc = _run_phase_range(
        args,
        analysis_dir,
        tag,
        phases,
        advisory,
        disk,
        disk_check_path,
        iv,
        iv_path,
        lab,
        conda_env,
        brieflow_output,
    )
    if rc == 0:
        try:
            hooks.on_post_terminal_phase(analysis_dir, screen_id)
        except Exception as e:
            notify(
                analysis_dir,
                tag,
                f"on_post_terminal_phase hook FAILED: {e} — outputs may not be persisted off-host",
                priority="action_needed",
            )
    elif rc != _RC_AWAITING_LAUNCH:
        # RC8 is excluded: the gate's exit is a run that never started, not a failure.
        _safe(_draft_issue, analysis_dir, tag, rc)
        _safe(
            lambda: hooks.on_failure(
                analysis_dir, screen_id, _latest_diag_bundle(analysis_dir, tag)
            )
        )
    return rc


def _safe_list(fn, *a) -> list:
    """A best-effort check's findings; [] when the check itself fails."""
    try:
        return list(fn(*a) or [])
    except Exception:
        return []


def _safe(fn, *a) -> None:
    """Call a best-effort hook; never raise."""
    try:
        fn(*a)
    except Exception:
        pass


def _draft_issue(analysis_dir: Path, tag: str, rc: int) -> None:
    """Leave .brieflow/issue_draft.{md,json} behind on an escalating exit.

    Called through _safe: the run has already failed and been notified, so a draft
    that cannot be assembled must not replace the real failure with a traceback.
    """
    if not write_issue_draft:
        return
    esc = _read_last_escalation(analysis_dir, tag)
    phase = esc.get("phase") or _paused_phase(analysis_dir) or "unknown"
    path = write_issue_draft(
        analysis_dir,
        tag,
        rc,
        phase,
        action=esc.get("action"),
        diag_bundle=esc.get("diag_bundle") or _latest_diag_bundle(analysis_dir, tag),
    )
    record_decision(
        analysis_dir,
        kind="escalate-draft",
        phase=phase,
        choice="issue_draft written",
        why=(esc.get("action") or {}).get("reason") or f"rc={rc}",
        inputs={"path": str(path)},
        source="auto",
    )
    route, next_action = issue_route(rc, esc.get("action"))
    notify(
        analysis_dir,
        tag,
        f"Escalation evidence: {path} (route: {route}). Agent: {next_action}",
        priority="action_needed",
    )


def _read_last_escalation(analysis_dir: Path, tag: str) -> dict:
    """The recover action brieflow_runloop stashed for THIS run's failure, consumed on read.

    {} when the failure was never classified (a QC gate, a preflight), and {} when the
    stash belongs to someone else — a direct /brieflow-module run writes one too, and a
    draft built from it would name a rule this run never touched. Stash tags are
    phase_tag(tag, ...) = f"{tag}__p{i}_{phase}", so this run's start with this run's tag
    AND the separator: run_10__p0_sbs is not run_1's.

    Removed whatever the verdict: read, rejected or unparseable, it must not outlive the
    run that found it.
    """
    p = Path(analysis_dir) / ".brieflow" / LAST_ESCALATION_NAME
    try:
        doc = json.loads(p.read_text())
    except (OSError, ValueError):
        doc = None
    finally:
        try:
            p.unlink()
        except OSError:
            pass
    if not isinstance(doc, dict) or not str(doc.get("tag", "")).startswith(f"{tag}__p"):
        return {}
    return doc


def _paused_phase(analysis_dir: Path) -> str | None:
    """The phase set_paused last recorded, for the failures that carry no classification."""
    return (_read_progress(analysis_dir).get("paused") or {}).get("phase") or None


def _latest_diag_bundle(analysis_dir: Path, tag: str) -> Path | None:
    """Newest diag bundle THIS run wrote, or None when there is none to archive.

    Scoped to `tag` first — diag/ accumulates across runs, and handing the failure
    hook another run's bundle is worse than handing it nothing — and ordered by
    mtime, since the bundle names sort by tag, not by time.
    """
    d = Path(analysis_dir) / "diag"
    if not d.exists():
        return None
    files = sorted(d.glob(f"{tag}_*.txt")) or sorted(d.glob("*.txt"))
    return max(files, key=lambda f: f.stat().st_mtime) if files else None


def _run_phase_range(
    args,
    analysis_dir: Path,
    tag: str,
    phases: list[str],
    advisory: set[str],
    disk: dict,
    disk_check_path: Path,
    iv: dict,
    iv_path: Path,
    lab: dict,
    conda_env: str,
    brieflow_output: Path,
) -> int:
    """Run the per-phase cycle (notebook -> preflight -> run -> QC) over `phases`.

    run_screen's loop body, split out so the lifecycle hooks bracket exactly the
    phases and nothing else. Returns the codes run_screen documents: 0 when the
    range completes, including a bounded range that stops before the last phase.
    """
    last_phase = phases[-1]
    bound_source = range_bound_source(args)

    for i, phase in enumerate(phases):
        if phase_done(analysis_dir, phase):
            notify(analysis_dir, tag, f"phase {phase}: already done — skipping")
            continue

        # RUN_CLASSIFY is the opt-in for the whole classify phase; the classify notebook ignores it.
        if (
            phase == "classify"
            and (_find_iv_row(iv, "RUN_CLASSIFY", "classify") or {}).get("value") is not True
        ):
            mark_phase_done(analysis_dir, phase)
            record_decision(
                analysis_dir,
                kind="skip-phase",
                phase=phase,
                choice="classify not run",
                why="RUN_CLASSIFY is not True; aggregate runs without a classifier",
                source="auto",
            )
            notify(analysis_dir, tag, "phase classify: RUN_CLASSIFY is off — skipped")
            continue

        # Disk check for this phase
        phase_gb = disk.get("phase_breakdown", {}).get(phase, 0)
        avail = check_disk_available(phase_gb, disk_check_path)
        if not avail["ok"]:
            msg = (
                f"{phase}: disk unavailable — need {avail.get('needed_with_headroom_gb', phase_gb):.0f} GB, "
                f"have {avail.get('available_gb', 0):.0f} GB."
            )
            # The requirement is a coarse per-tile heuristic (brieflow_disk) and can be
            # several-fold high (damavand preprocess: predicted ~11 TB, used 1.6 TB). So
            # allow an explicit operator override — a known-bad estimate must not hard-block
            # a run that actually fits. Runtime disk guards (disk_abort_pct) remain the real
            # safety net, aborting if the disk genuinely fills mid-phase.
            if os.environ.get("BRIEFLOW_SKIP_DISK_CHECK", "").lower() in ("1", "true", "yes"):
                notify(
                    analysis_dir,
                    tag,
                    msg
                    + " — overridden via BRIEFLOW_SKIP_DISK_CHECK; proceeding (runtime guards active)",
                    priority="warning",
                )
            else:
                notify(analysis_dir, tag, msg, priority="action_needed")
                set_paused(analysis_dir, phase, msg)
                return _RC_DISK_UNAVAIL

        # Per-phase agent resolution (sweep tiles + filled-value patching +
        # parameter sweeps + cluster/merge N0). Scoped in brieflow_resolvers as
        # resolve_phase(phase, ctx); ctx injects the runner's helpers so resolvers
        # stays cycle-free and these stay test-patchable via the runner module.
        nb_path = phase_notebook(analysis_dir, phase)
        ctx = {
            "analysis_dir": analysis_dir,
            "conda_env": conda_env,
            "lab": lab,
            "iv": iv,
            "iv_path": iv_path,
            "nb_path": nb_path,
            "tag": tag,
            "notify": notify,
            "select_validation_tiles_from_metadata": select_validation_tiles_from_metadata,
            "select_tiles_one_per_well": select_tiles_one_per_well,
            "select_test_tile_from_metadata": select_test_tile_from_metadata,
            "list_wells_from_metadata": list_wells_from_metadata,
            "is_active": is_active,
            "patch_marker_block_value": patch_marker_block_value,
            "lab_sweep_spec": lab_sweep_spec,
            "lab_sweep_band": lab_sweep_band,
            **sweep_functions(args, analysis_dir, lab, conda_env, phase),
            "pick_sweep_winner": pick_sweep_winner,
            "sweep_evidence": sweep_evidence,
            "record_tuned_confirmed": record_tuned_confirmed,
            "save_interview": save_interview,
            "resolve_cluster_final": resolve_cluster_final,
            "resolve_merge_initial_sbs_tiles": resolve_merge_initial_sbs_tiles,
            "resolve_merge_approach": resolve_merge_approach,
            "resolve_merge_det_range": resolve_merge_det_range,
        }
        rp = resolve_phase(phase, ctx)
        if rp.get("paused"):
            notify(analysis_dir, tag, rp["reason"], priority="action_needed")
            set_paused(analysis_dir, phase, rp["reason"])
            return _RC_TEMPLATE_GAP

        # merge's notebook iterates INITIAL_SBS_TILES directly, so an unresolved value
        # surfaces as `TypeError: 'NoneType' object is not iterable` 400 lines in
        # (seen on a sparse screen). Stop here with the reason instead.
        if phase == "merge" and _merge_anchors_unresolved(analysis_dir, tag):
            msg = (
                "merge: INITIAL_SBS_TILES is unset — the anchor probe found no tile with "
                "enough SBS cells across wells. Set it in the interview, or lower "
                "lab_defaults.merge_alignment.n_initial_sbs_tiles / the min_cells gate."
            )
            notify(analysis_dir, tag, msg, priority="action_needed")
            set_paused(analysis_dir, phase, msg)
            return _RC_TEMPLATE_GAP

        # N1: run the configured notebook to flush config/config.yml to disk
        nb_result = run_phase_notebook(analysis_dir, phase, conda_env)
        if not nb_result["ok"]:
            msg = f"{phase}: notebook run failed (config not written) — see {nb_result['log']}"
            notify(analysis_dir, tag, msg, priority="action_needed")
            set_paused(analysis_dir, phase, "notebook_run_failed")
            return _RC_NOTEBOOK_FAIL

        # Notebook-only phases (classify) end here: the notebook has written the
        # config section downstream phases read, and there is no snakemake module
        # to preflight, submit or QC.
        if phase in NOTEBOOK_ONLY_PHASES:
            mark_phase_done(analysis_dir, phase)
            notify(
                analysis_dir,
                tag,
                f"phase {phase}: config written (notebook-only, nothing to run) — "
                f"{i + 1}/{len(phases)} phases complete",
                priority="progress",
            )
            if phase == last_phase and last_phase != PHASES[-1]:
                notify(
                    analysis_dir,
                    tag,
                    f"STOPPING AFTER {phase} ({bound_source}) — "
                    f"{len(PHASES) - PHASES.index(phase) - 1} later phase(s) not run",
                    priority="action_needed",
                )
                return 0
            continue

        # Preflight: validate the just-written config against brieflow rule code
        # BEFORE submitting. Completes the canonical per-phase cycle
        # (notebook → preflight → run → QC); catches config↔rule mismatches in
        # seconds instead of after a phase of cluster time.
        if phase == "sbs":
            # Base-order probe: infer channel<->base from the data on the test tile
            # and let preflight gate on the written report (HIGH on mismatch).
            probe = run_base_order_probe(analysis_dir, conda_env, nb_path)
            if probe.get("ran"):
                notify(
                    analysis_dir,
                    tag,
                    f"sbs: base-order probe {probe['verdict']} — configured "
                    f"{probe['configured']}, best {probe['best']}"
                    + (
                        f", suggested bases {probe['suggested_bases']}"
                        if probe.get("suggested_bases")
                        else ""
                    ),
                    priority="warning" if probe["verdict"] != "ok" else "progress",
                )
            else:
                notify(
                    analysis_dir,
                    tag,
                    f"sbs: base-order probe skipped ({probe.get('detail')})",
                    priority="warning",
                )

            # Dye-balance probe: calibrate the frac caller's dye scale and brightness
            # reference on the same tile, and apply what wins against a decoy control.
            dye = run_dye_balance_probe(analysis_dir, conda_env, nb_path)
            if dye.get("ran") and dye.get("verdict") == "improved":
                notify(
                    analysis_dir,
                    tag,
                    f"sbs: dye-balance probe improved the call by "
                    f"{dye['gain']:+.0%} in exact-library reads — DYE_SCALE={dye['dye_scale']}, "
                    f"BRIGHTNESS_REGIONS={dye['brightness_regions']}"
                    + ("" if dye.get("applied") else " (NOT applied: config unchanged)"),
                    priority="progress" if dye.get("applied") else "warning",
                )
            elif not dye.get("ran"):
                notify(
                    analysis_dir,
                    tag,
                    f"sbs: dye-balance probe skipped ({dye.get('detail')})",
                    priority="warning",
                )
        # The phase notebook may have just written the combo TSVs the geometry probe reads.
        refresh_geometry(analysis_dir, analysis_dir / args.config)
        pf_rc = run_preflight(analysis_dir, getattr(args, "backend", "slurm"))
        if pf_rc != 0:
            msg = (
                f"{phase}: preflight found HIGH-severity config issues (rc={pf_rc}) "
                f"— refusing to submit. Run /brieflow-preflight for the report."
            )
            notify(analysis_dir, tag, msg, priority="action_needed")
            set_paused(analysis_dir, phase, "preflight_failed")
            return _RC_PREFLIGHT_FAIL

        # Upstream-rerun guard: dry-run the phase (gated) and ABORT if its DAG would
        # re-run an already-completed upstream phase. Caught the merge→phenotype
        # rerun the hard way (hours of wasted compute before the operator noticed);
        # this is defense-in-depth behind the flow.sh phase-gates.
        guard_abort, guard_detail = run_upstream_rerun_guard(
            analysis_dir, phase, conda_env=conda_env
        )
        if guard_abort:
            msg = (
                f"{phase}: UPSTREAM-RERUN GUARD aborted — the gated dry-run would "
                f"re-run a completed upstream phase ({guard_detail}). The flow.sh "
                f"phase-gates are likely missing/stale, or upstream state is "
                f"inconsistent. Refusing to submit so it doesn't silently redo hours "
                f"of work."
            )
            notify(analysis_dir, tag, msg, priority="action_needed")
            set_paused(analysis_dir, phase, "upstream_rerun_guard")
            return _RC_UPSTREAM_RERUN

        marker = read_phase_marker(analysis_dir, phase)
        reuse, force_rules = False, []
        if marker:
            reuse, detail, force_rules = phase_rerun_check(
                analysis_dir, phase, args.config, conda_env, marker
            )
            notify(analysis_dir, tag, detail, priority="progress" if reuse else "warning")
            if not reuse:
                record_decision(
                    analysis_dir, kind="phase-rerun", phase=phase, choice="rerun", why=detail
                )
        if not reuse:
            notify(
                analysis_dir,
                tag,
                f"{phase}: notebook config written + preflight ✓ + no upstream rerun — launching run",
                priority="progress",
            )

            # Run the phase. SBS branches per-plate when screen.yaml declares
            # iss.plate_overrides (branch-and-converge); every other phase — and SBS
            # without overrides — runs once over the union (base config).
            notes = f"runner phase {i + 1}/{len(phases)}: {phase}"
            plate_overrides = load_plate_overrides(analysis_dir) if phase == "sbs" else {}
            if plate_overrides:
                try:
                    rc, failed_plate = run_sbs_branched(
                        args, analysis_dir, plate_overrides, tag, notes
                    )
                except ValueError as e:  # bad override (cycle not present, etc.)
                    msg = f"sbs: branch-and-converge config error — {e}"
                    notify(analysis_dir, tag, msg, priority="action_needed")
                    set_paused(analysis_dir, phase, "sbs_plate_override_error")
                    return _RC_NOTEBOOK_FAIL
                if rc != 0:
                    msg = (
                        f"sbs: plate {failed_plate} run failed (rc={rc}) — branch-and-converge "
                        f"pipeline failure (recovery exhausted)"
                    )
                    notify(analysis_dir, tag, msg, priority="action_needed")
                    set_paused(analysis_dir, phase, msg)
                    return _RC_RUN_FAIL
            else:
                ptag = phase_tag(tag, i, [phase])
                phase_args = args
                if force_rules:
                    phase_args = copy.copy(args)
                    phase_args.forcerun = list(args.forcerun or []) + force_rules
                rc = run_one_phase(phase_args, [phase], ptag, notes)
                if rc != 0:
                    msg = f"{phase}: run_one_phase returned {rc} — pipeline failure (recovery exhausted)"
                    notify(analysis_dir, tag, msg, priority="action_needed")
                    set_paused(analysis_dir, phase, msg)
                    return _RC_RUN_FAIL
            write_phase_marker(analysis_dir, phase, tag, args.config)

        # QC gate
        generated_at = datetime.now(timezone.utc).isoformat()
        summary = build_phase_qc(
            brieflow_output, phase, generated_at=generated_at, lab_defaults=lab
        )
        verdict = qc_gate_verdict(summary)
        warned = [m.name for m in getattr(summary, "metrics", []) if m.status == "warn"]
        qc_note = f"QC warn: {', '.join(warned)}" if warned else "QC ok"
        advisory_pass = not verdict["pass"] and phase in advisory
        _safe(lambda: record_qc_status(analysis_dir, phase, summary, advisory=advisory_pass))
        if not verdict["pass"]:
            msg = (
                f"{phase}: QC gate failed — {verdict['rationale']}. "
                f"Failing metrics: {', '.join(verdict.get('failures', []))}"
            )
            if phase in advisory:
                notify(
                    analysis_dir,
                    tag,
                    f"{msg} — ADVISORY (BRIEFLOW_QC_ADVISORY), continuing",
                    priority="action_needed",
                )
                qc_note = "QC gate failed, accepted as advisory"
            else:
                notify(analysis_dir, tag, msg, priority="action_needed")
                set_paused(analysis_dir, phase, msg)
                return _RC_QC_GATE_FAIL

        mark_phase_done(analysis_dir, phase)
        notify(
            analysis_dir,
            tag,
            f"phase {phase}: PASS ({qc_note}) — {i + 1}/{len(phases)} phases complete",
            priority="progress",
        )

        # OUTSTANDING #19: opt-in — once preprocess passes QC the raw input is no
        # longer read by any phase, so free it (GCS-verified) to reclaim screen-size
        # TB before the disk-heavy aggregate stage.
        if phase == "preprocess" and getattr(args, "free_raw_input_gcs", None):
            _free_raw_input_after_preprocess(analysis_dir, tag, args.free_raw_input_gcs)

        if phase == last_phase and last_phase != PHASES[-1]:
            notify(
                analysis_dir,
                tag,
                f"STOPPING AFTER {phase} ({bound_source}) — "
                f"{len(PHASES) - PHASES.index(phase) - 1} later phase(s) not run",
                priority="action_needed",
            )
            return 0

    review = qc_review_note(analysis_dir)
    notify(
        analysis_dir,
        tag,
        "ALL PHASES COMPLETE — run complete, review results"
        + (f" | QC to review: {review}" if review else ""),
        priority="action_needed",
    )
    return 0

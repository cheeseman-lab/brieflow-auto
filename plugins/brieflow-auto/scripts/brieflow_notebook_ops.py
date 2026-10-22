#!/usr/bin/env python3
"""brieflow_notebook_ops.py — pre-edit static checks + notebook lifecycle ops.

Operator-facing notebook operations layered on the interview + marker engines:
preflight_notebook (the pre-edit gate combining marker-coverage + collision +
dry-export checks), dry_export_notebook, reset_phase, verify_phase_outputs,
audit_canonical_notebooks, and confirm_tuned_loop. Depends on brieflow_interview
(load/save + tuned helpers), brieflow_markers (coverage/collision/patch), and
brieflow_buckets (_NO_MARKER_BUCKETS). Re-exported from brieflow_wizard.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Iterable

from brieflow_phases import PHASE_NOTEBOOKS, phase_notebook
from brieflow_buckets import _NO_MARKER_BUCKETS
from brieflow_interview import (
    load_interview,
    pull_tuned_values,
    record_tuned_confirmed,
    save_interview,
    tuned_export_path,
)
from brieflow_markers import (
    patch_marker_block_value,
    scan_variable_collisions,
    verify_marker_coverage,
)

# ---------------------------------------------------------------------------
# Pre-edit gate: combine all static notebook checks into one call
# ---------------------------------------------------------------------------


# _NO_MARKER_BUCKETS is defined in brieflow_buckets and re-exported above.


def preflight_notebook(
    notebook_path: str | Path,
    expected_operator_params: Iterable[str] | dict,
    conda_env: str | None = None,
    run_dry_export: bool = True,
    param_buckets: dict | None = None,
) -> dict:
    """Run all static notebook checks the wizard relies on before editing.

    The wizard MUST call this BEFORE any Edit-tool call on a phase notebook,
    and must abort if `ok` is False. Every notebook-template defect found in
    the first reference pass would have been caught here (see W-003).

    `expected_operator_params` may be:
      - an iterable of param names (legacy; all missing entries BLOCK), or
      - a dict mapping param_name → bucket (preferred; missing entries whose
        bucket is in `_NO_MARKER_BUCKETS` are downgraded to WARN).

    A separate `param_buckets` dict can also be passed alongside an iterable
    of names to attach bucket metadata without changing call sites.

    Returns:
        {
          "ok": bool,                  # True iff no BLOCKing issues
          "notebook": <path>,
          "marker_coverage": {covered, outside, missing},
          "missing_warn": [param, ...],   # missing but bucket=archival/output/...
          "missing_block": [param, ...],  # missing AND bucket expects a marker
          "collisions": [...],         # from scan_variable_collisions
          "dry_export": {...} | None,  # from dry_export_notebook (skipped if run_dry_export=False)
          "warnings": [str, ...],      # non-blocking notes
          "summary": str,              # one-line human-readable
        }
    """
    notebook_path = Path(notebook_path)
    if isinstance(expected_operator_params, dict):
        buckets_map = dict(expected_operator_params)
        names = list(buckets_map.keys())
    else:
        names = list(expected_operator_params)
        buckets_map = dict(param_buckets or {})
    coverage = verify_marker_coverage(notebook_path, names)
    collisions = scan_variable_collisions(notebook_path)
    dry = (
        dry_export_notebook(notebook_path, conda_env=conda_env)
        if run_dry_export
        else None
    )

    missing_warn: list[str] = []
    missing_block: list[str] = []
    for param in coverage["missing"]:
        root = param.split(".", 1)[0]
        bucket = buckets_map.get(param) or buckets_map.get(root)
        if bucket in _NO_MARKER_BUCKETS:
            missing_warn.append(param)
        else:
            missing_block.append(param)

    # `outside` = params assigned outside marker blocks. Many phase notebook
    # params are legitimately derived in derivation cells from operator
    # inputs (e.g. `dapi_index = CHANNEL_NAMES.index("DAPI")`, `bases` from
    # CHANNEL_NAMES) — the wizard doesn't patch them. The actual safety net
    # for "wizard tried to write to a non-existent marker" is in
    # `patch_marker_block_value` itself, which refuses to write when no
    # marker exists for the target name. Preflight reports outside-marker
    # assignments as a WARN (informational), not a BLOCK.
    issues: list[str] = []
    warnings: list[str] = []
    if coverage["outside"]:
        warnings.append(
            f"{len(coverage['outside'])} param(s) assigned outside marker blocks "
            "(derivation cells / library defaults — wizard does not patch these)"
        )
    if missing_block:
        issues.append(f"{len(missing_block)} expected param(s) not found at all")
    if missing_warn:
        warnings.append(
            f"{len(missing_warn)} param(s) missing marker but bucket is "
            f"{sorted(_NO_MARKER_BUCKETS)} — no marker expected"
        )
    if collisions:
        issues.append(f"{len(collisions)} cross-cell name collision(s)")
    if dry is not None and not dry["ok"]:
        issues.append("marimo export failed (import drift / syntax / cell-graph)")

    ok = not issues
    summary_parts = issues + [f"WARN: {w}" for w in warnings]
    summary = (
        "preflight OK"
        if ok and not warnings
        else "; ".join(summary_parts) or "preflight OK"
    )
    return {
        "ok": ok,
        "notebook": str(notebook_path),
        "marker_coverage": coverage,
        "missing_warn": missing_warn,
        "missing_block": missing_block,
        "collisions": collisions,
        "dry_export": dry,
        "warnings": warnings,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Notebook export dry-run (catches import drift, syntax errors, cell graph)
# ---------------------------------------------------------------------------


def dry_export_notebook(
    notebook_path: str | Path,
    conda_env: str | None = None,
) -> dict:
    """Run `marimo export script` on the notebook and report the result.

    This is the cheapest end-to-end gate: catches import drift (e.g.,
    `lib.shared.io` → `lib.shared.image_io`), marimo cell-graph errors
    (duplicate definitions), and Python syntax errors — all without executing
    any cell body. If `conda_env` is provided, the export runs under that env;
    otherwise the current Python is used (mostly useful for tests).

    Returns:

        {"ok": bool, "stdout": str, "stderr": str, "returncode": int}
    """
    nb = Path(notebook_path).resolve()
    out_path = nb.parent / ".brieflow" / f"{nb.stem}.dry.script.py"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if conda_env:
        cmd = [
            "bash",
            "-c",
            f'eval "$(conda shell.bash hook)" && conda activate {conda_env} && '
            f'python -m marimo export script "{nb}" -o "{out_path}" --force',
        ]
    else:
        cmd = [
            "python",
            "-m",
            "marimo",
            "export",
            "script",
            str(nb),
            "-o",
            str(out_path),
            "--force",
        ]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=nb.parent)
    return {
        "ok": proc.returncode == 0,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "returncode": proc.returncode,
    }


def reset_phase(
    analysis_dir: str | Path,
    phase: str,
    notebook_file: str | None = None,
) -> dict:
    """Roll a phase back to a clean slate so the wizard can re-walk it:
    1. git-checkout the phase notebook (discards in-marker edits)
    2. Null out every interview row for that notebook (value/value_source/set_at)
    3. Delete .brieflow/tuned_<phase>.json if it exists

    Returns:
        {"notebook_reverted": bool, "interview_rows_cleared": int,
         "tuned_export_deleted": bool}

    Caller saves the interview separately after this returns. Does NOT touch
    config.yml or other outputs — operator can clean those manually if needed.
    """
    analysis_dir = Path(analysis_dir)
    if not notebook_file and phase not in PHASE_NOTEBOOKS:
        raise ValueError(f"unknown phase: {phase!r}")

    nb_path = analysis_dir / notebook_file if notebook_file else phase_notebook(analysis_dir, phase)
    # git checkout — only succeeds if file is git-tracked
    reverted = False
    try:
        proc = subprocess.run(
            ["git", "checkout", "--", str(nb_path)],
            cwd=analysis_dir.parent
            if (analysis_dir / ".git").exists() is False
            else analysis_dir,
            capture_output=True,
            text=True,
        )
        reverted = proc.returncode == 0
    except FileNotFoundError:
        pass  # no git binary

    # Clear interview rows
    interview_path = analysis_dir / ".brieflow" / "interview.json"
    cleared = 0
    if interview_path.exists():
        interview = load_interview(interview_path)
        for row in interview["params"]:
            if row["notebook"] == phase and row.get("value") is not None:
                row["value"] = None
                row["value_source"] = None
                row["set_at"] = None
                cleared += 1
        save_interview(interview, interview_path)

    # Delete tuned export
    tuned_path = tuned_export_path(analysis_dir, phase)
    deleted = False
    if tuned_path.exists():
        tuned_path.unlink()
        deleted = True

    return {
        "notebook_reverted": reverted,
        "notebook_path": str(nb_path),
        "interview_rows_cleared": cleared,
        "tuned_export_deleted": deleted,
    }


def verify_phase_outputs(
    analysis_dir: str | Path,
    phase: str,
) -> dict:
    """Post-run validation: assert the notebook produced the expected
    artifacts. Returns:

        {"ok": bool, "phase": str, "checks": [{"check": str, "ok": bool, "reason": str}]}

    Per-phase expectations live inline; extend as new phases are walked.
    """
    analysis_dir = Path(analysis_dir)
    checks: list[dict] = []

    def _exists(rel_path: str, why: str) -> dict:
        full = analysis_dir / rel_path
        return {"check": rel_path, "ok": full.exists(), "reason": why}

    if phase == "preprocess":
        for rel, why in [
            ("config/config.yml", "preprocess config-write cell output"),
            ("config/sbs_samples.tsv", "preprocess sample-discovery output (SBS)"),
            (
                "config/phenotype_samples.tsv",
                "preprocess sample-discovery output (phenotype)",
            ),
            ("config/sbs_combo.tsv", "preprocess combo-DF output (SBS)"),
            ("config/phenotype_combo.tsv", "preprocess combo-DF output (phenotype)"),
        ]:
            checks.append(_exists(rel, why))
        # config.yml must have a preprocess section
        cfg = analysis_dir / "config" / "config.yml"
        if cfg.exists():
            import yaml as _yaml

            try:
                cfg_d = _yaml.safe_load(cfg.read_text())
                has = isinstance(cfg_d, dict) and "preprocess" in (cfg_d or {})
                checks.append(
                    {
                        "check": "config.yml has preprocess section",
                        "ok": has,
                        "reason": "config-write cell of 0_preprocess.py",
                    }
                )
            except Exception as e:
                checks.append(
                    {"check": "config.yml parseable", "ok": False, "reason": str(e)}
                )
    elif phase == "sbs":
        for rel, why in [
            ("config/config.yml", "sbs config-write cell output"),
        ]:
            checks.append(_exists(rel, why))
        cfg = analysis_dir / "config" / "config.yml"
        if cfg.exists():
            import yaml as _yaml

            try:
                cfg_d = _yaml.safe_load(cfg.read_text())
                has = isinstance(cfg_d, dict) and "sbs" in (cfg_d or {})
                checks.append(
                    {
                        "check": "config.yml has sbs section",
                        "ok": has,
                        "reason": "config-write cell of 1_sbs.py",
                    }
                )
            except Exception:
                pass
    elif phase in ("phenotype", "merge", "classify", "aggregate", "cluster"):
        # Same shape as sbs — section existence check
        cfg = analysis_dir / "config" / "config.yml"
        if cfg.exists():
            import yaml as _yaml

            try:
                cfg_d = _yaml.safe_load(cfg.read_text())
                has = isinstance(cfg_d, dict) and phase in (cfg_d or {})
                checks.append(
                    {
                        "check": f"config.yml has {phase} section",
                        "ok": has,
                        "reason": f"config-write cell of {phase} notebook",
                    }
                )
            except Exception:
                pass
    elif phase == "analyze":
        # analyze writes the `mozzarellm` section, not an `analyze` one.
        cfg = analysis_dir / "config" / "config.yml"
        if cfg.exists():
            import yaml as _yaml

            try:
                cfg_d = _yaml.safe_load(cfg.read_text())
                has = isinstance(cfg_d, dict) and "mozzarellm" in (cfg_d or {})
                checks.append(
                    {
                        "check": "config.yml has mozzarellm section",
                        "ok": has,
                        "reason": "config-write cell of 7_analyze.py",
                    }
                )
            except Exception:
                pass
    else:
        return {
            "ok": False,
            "phase": phase,
            "checks": [
                {
                    "check": "phase recognized",
                    "ok": False,
                    "reason": f"unknown phase: {phase!r}",
                }
            ],
        }

    ok = all(c["ok"] for c in checks) and bool(checks)
    return {"ok": ok, "phase": phase, "checks": checks}


# ---------------------------------------------------------------------------
# Tier 2 — orchestrators over Tier 1 + tuned-loop + existing helpers
# ---------------------------------------------------------------------------


def audit_canonical_notebooks(
    canonical_dir: str | Path,
    interview_template_path: str | Path,
    conda_env: str | None = None,
) -> dict:
    """Preflight every canonical phase notebook in one call. Use at wizard
    activation: if any phase notebook is structurally broken, the wizard
    must stop and surface the report before bootstrapping interview.json.

    Returns:
        {
          "ok": bool,
          "phases": {<phase>: {"ok": bool, "preflight": <report>, "exists": bool}},
          "summary": str,
        }
    """
    canonical_dir = Path(canonical_dir)
    template = json.loads(Path(interview_template_path).read_text())
    # Map every CSV param to its bucket so preflight can warn-vs-block on
    # missing markers (no-marker buckets like archival/output/nonexistent
    # don't expect a notebook assignment).
    params_by_phase: dict[str, dict[str, str]] = {}
    for row in template.get("params", []):
        if row["bucket"] not in ("user", "user_tuned_seed"):
            continue
        params_by_phase.setdefault(row["notebook"], {})[row["param_name"]] = row[
            "bucket"
        ]

    phase_to_file = dict(PHASE_NOTEBOOKS)

    results: dict[str, dict] = {}
    all_ok = True
    for phase, fname in phase_to_file.items():
        nb_path = canonical_dir / fname
        if not nb_path.exists():
            results[phase] = {"ok": False, "exists": False, "preflight": None}
            all_ok = False
            continue
        params = params_by_phase.get(phase, {})
        try:
            report = preflight_notebook(
                nb_path,
                params,
                conda_env=conda_env,
                run_dry_export=conda_env is not None,
            )
        except Exception as e:
            results[phase] = {
                "ok": False,
                "exists": True,
                "preflight": {"error": str(e)},
            }
            all_ok = False
            continue
        results[phase] = {"ok": report["ok"], "exists": True, "preflight": report}
        if not report["ok"]:
            all_ok = False

    summary_parts = []
    for phase, r in results.items():
        if r["ok"]:
            summary_parts.append(f"{phase}=OK")
        elif not r["exists"]:
            summary_parts.append(f"{phase}=MISSING")
        else:
            p = r["preflight"] or {}
            mc = p.get("marker_coverage", {}) if isinstance(p, dict) else {}
            issues = []
            if mc.get("outside"):
                issues.append(f"{len(mc['outside'])} outside")
            if mc.get("missing"):
                issues.append(f"{len(mc['missing'])} missing")
            if p.get("collisions"):
                issues.append(f"{len(p['collisions'])} collisions")
            if (
                isinstance(p, dict)
                and p.get("dry_export")
                and not p["dry_export"].get("ok", True)
            ):
                issues.append("dry_export_fail")
            if isinstance(p, dict) and "error" in p:
                issues.append(f"err:{p['error'][:40]}")
            summary_parts.append(f"{phase}=[{', '.join(issues) or '?'}]")
    return {
        "ok": all_ok,
        "phases": results,
        "summary": "; ".join(summary_parts),
    }


def confirm_tuned_loop(
    interview: dict,
    analysis_dir: str | Path,
    phase: str,
    notebook_path: str | Path,
    confirmations: dict | None = None,
) -> dict:
    """R3 orchestrator: pull tuned exports, surface each derived value to the
    operator, write back to interview + marker block.

    The wizard agent calls this AFTER running the phase notebook. It returns
    a list of pending decisions for the agent to surface via AskUserQuestion;
    on the next call, the agent passes the operator's answers in `confirmations`
    and this commits them.

    Two-step pattern (because AskUserQuestion is agent-driven, not callable
    from helpers):

        # Step 1 — get pending decisions
        plan = confirm_tuned_loop(interview, analysis_dir, phase, notebook_path)
        # plan = {"tuned": {...}, "pending": [{param, derived, src}, ...]}

        # Step 2 — agent surfaces each via AskUserQuestion, then:
        confirm_tuned_loop(interview, analysis_dir, phase, notebook_path,
                           confirmations={"NUCLEI_DIAMETER": 11.5, "THRESHOLD_READS": 50})
    """
    analysis_dir = Path(analysis_dir)
    tuned = pull_tuned_values(analysis_dir, phase)

    if confirmations is None:
        pending = []
        for param, info in tuned.items():
            pending.append(
                {
                    "param": param,
                    "derived": info.get("derived"),
                    "src": info.get("src"),
                    "viz_path": info.get("viz_path"),
                }
            )
        return {
            "tuned": tuned,
            "pending": pending,
            "tuned_export_path": str(tuned_export_path(analysis_dir, phase)),
            "missing_export": not bool(tuned),
        }

    # Commit phase: record each confirmation + patch marker block
    committed = []
    for param, value in confirmations.items():
        try:
            record_tuned_confirmed(
                interview, param_name=param, confirmed_value=value, notebook=phase
            )
            patch_marker_block_value(notebook_path, param, repr(value))
            committed.append({"param": param, "value": value, "ok": True})
        except Exception as e:
            committed.append(
                {"param": param, "value": value, "ok": False, "error": str(e)}
            )
    save_interview(interview, analysis_dir / ".brieflow" / "interview.json")
    return {"committed": committed}

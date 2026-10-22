#!/usr/bin/env python3
"""brieflow_sweep.py — parameter-sweep + tile-validation orchestration.

The headless + live-kernel sweep machinery: run_marimo_sweep (live reactive
sweep over a marker via ctx.edit_cell/run_cell), the headless subprocess paths
(sweep_headless / adaptive_sweep_headless / validate_tiles_headless /
validate_across_tiles / _fan_out_notebook_runs), QC + mapping-rate + resolution
parsing, winner selection, and export_phase_html. Depends on brieflow_markers
(marker location + patch), brieflow_marimo (kernel settle + tee setup), and
brieflow_tiles (validation tiles); pandas is lazy. Re-exported from
brieflow_wizard. run_marimo_sweep + export_phase_html are exercised live by
tests/test_marimo_live.py.
"""

from __future__ import annotations

import os
import re
import sys
import subprocess
import threading
from pathlib import Path
from typing import Any, Iterable

from brieflow_markers import find_marker_cell, patch_marker_block_value
from brieflow_marimo import (
    _await_kernel_settle,
    start_marimo_kernel,
    stop_marimo_kernel,
)

# ---------------------------------------------------------------------------
# Marimo kernel — cell lookup, sweeps, tile validation, library print teeing
# ---------------------------------------------------------------------------




def _phase_from_nb_path(nb_path: "Path | str") -> str | None:
    """Derive the brieflow phase name from a notebook filename, or None."""
    from brieflow_phases import notebook_phase

    return notebook_phase(nb_path)


# Regexes the QC parser uses. Mirrors the metric names emitted by the brieflow
# library's per-cell prints. Add new metrics here as they're introduced; the
# parser is purely additive — unknown metrics are simply not reported.
_QC_PATTERNS: dict[str, tuple[str, str, float | None]] = {
    # name: (regex, comparator, threshold). PASS = comparator-true.
    # threshold=None → metric is parsed/reported but never gates qc_pass (INFO-ONLY).
    "cycle_dapi_shift_residual_max_px": (
        r"cycle_dapi_shift_residual_max_px:\s+([\d.]+)",
        "<",
        None,  # gated via sweep_qc in qc_gates.yaml (max: 10.0); no hardcoded default
    ),
    "intra_cycle_channel_shift_residual_max_px": (
        r"intra_cycle_channel_shift_residual_max_px:\s+([\d.]+)",
        "<",
        None,  # INFO-ONLY: F02 diagnostic of optics, never gates qc_pass
    ),
    "cells_with_1_nucleus_frac": (
        r"cells_with_1_nucleus_frac:\s+([\d.]+)",
        ">",
        0.95,
    ),
    "cell_area_cv": (
        r"cell_area_cv:\s+([\d.]+)",
        "<",
        0.6,
    ),
    "mean_nuclear_solidity": (
        r"mean_nuclear_solidity:\s+([\d.]+)",
        ">",
        0.9,
    ),
}


# Parsed and reported, never a sign that QC ran (an optics diagnostic, see _QC_PATTERNS).
_QC_INFO_ONLY = {"intra_cycle_channel_shift_residual_max_px"}


def parse_qc_block(
    stdout: str,
    baseline: dict | None = None,
    gates: dict | None = None,
    phase: str | None = None,
) -> dict:
    """Parse Alignment QC + Segmentation QC metric prints from kernel stdout.

    `baseline` is the previous iteration's parsed QC dict; any metric absent
    from the current `stdout` is inherited from `baseline` (because reactive
    recompute did NOT invalidate the upstream cell that prints that metric —
    the value still holds). Only label `FAIL` when a metric is PRESENT in
    `stdout` and below its threshold.

    Fails closed: when no gating metric is present in `stdout` or inherited from
    `baseline`, nothing was checked (for example a notebook that no longer passes
    `verbose=True`, so the library prints no QC block), and `qc_pass` is False with
    `qc_missing` True.

    `gates` and `phase` enable tunable thresholds: when both are provided, the
    effective threshold for each metric is taken from `gates`'s `sweep_qc`
    section (whichever of min/max is set) when the metric is present there;
    otherwise the hardcoded default in `_QC_PATTERNS` is used. The op/direction
    (< vs >) always comes from `_QC_PATTERNS`. Callers that don't pass
    gates/phase keep today's behavior exactly (backward-compatible).

    Returns:
        {
          "metrics": {name: float, ...},        # numeric values (current or inherited)
          "inherited": [name, ...],             # metrics taken from baseline
          "failed": [name, ...],                # metrics PRESENT and failing threshold
          "qc_pass": bool,                      # all present metrics pass
          "qc_inherited": bool,                 # qc_pass derived entirely from baseline
          "qc_missing": bool,                   # no gating metric present or inherited
        }
    """
    baseline = baseline or {}
    base_metrics = baseline.get("metrics", {}) if isinstance(baseline, dict) else {}

    # Build sweep_qc override map: metric -> threshold float from gates.
    sweep_overrides: dict[str, float] = {}
    if gates is not None and phase is not None:
        try:
            from brieflow_qc import sweep_gates as _sweep_gates  # type: ignore[import]
            sq = _sweep_gates(gates, phase)
        except ImportError:
            sq = (gates.get(phase, {}) or {}).get("sweep_qc", {}) or {}
        for metric, gate in sq.items():
            if gate is None:
                continue
            if "min" in gate:
                sweep_overrides[metric] = float(gate["min"])
            elif "max" in gate:
                sweep_overrides[metric] = float(gate["max"])

    metrics: dict[str, float] = {}
    inherited: list[str] = []
    failed: list[str] = []
    any_present = False
    gating_seen = False
    for name, (pattern, op, default_thresh) in _QC_PATTERNS.items():
        thresh = sweep_overrides.get(name, default_thresh)
        m = re.search(pattern, stdout)
        if m is None:
            if name in base_metrics:
                metrics[name] = base_metrics[name]
                inherited.append(name)
                gating_seen |= name not in _QC_INFO_ONLY
            continue
        any_present = True
        gating_seen |= name not in _QC_INFO_ONLY
        val = float(m.group(1))
        metrics[name] = val
        if thresh is None:
            # INFO-ONLY metric: parsed and stored but never gates qc_pass.
            continue
        passes = (val < thresh) if op == "<" else (val > thresh)
        if not passes:
            failed.append(name)

    qc_missing = not gating_seen
    qc_pass = not failed and not qc_missing
    return {
        "metrics": metrics,
        "inherited": inherited,
        "failed": failed,
        "qc_pass": qc_pass,
        "qc_inherited": qc_pass and not any_present,
        "qc_missing": qc_missing,
    }


TIMEOUT_MARK = "SWEEP_TIMEOUT"


class SweepTimeout(RuntimeError):
    """A sweep stopped on a time limit, not on missing data."""


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, (SweepTimeout, subprocess.TimeoutExpired, TimeoutError)):
        return True
    text = str(exc)
    return TIMEOUT_MARK in text or "TimeoutExpired" in text or "TimeoutError" in text


def _no_rate_error(fn: str, n_tiles: int, started: int, failures: list, limit_s: float):
    """The exception for a reactive sweep that produced no finite rate: a timeout is
    reported as a timeout, never as missing data."""
    kinds = [kind for _slug, kind, _msg in failures]
    if kinds and all(k == "timeout" for k in kinds):
        return SweepTimeout(
            f"{fn}: timed out on {len(kinds)}/{n_tiles} tile(s) (limit {limit_s:.0f} s for a "
            "value; the baseline limit scales with tile size) — a time limit, not missing "
            "data; run the sweep inside a Slurm allocation or raise sweep_tiles.kernel_timeout_s"
        )
    detail = ", ".join(f"{k} x{kinds.count(k)}" for k in sorted(set(kinds))) or "no tile failed"
    return RuntimeError(
        f"{fn}: no finite mapping rate across {n_tiles} tile(s) ({started} kernel(s) started; "
        f"{detail}; see <tile>.error.log) — live-kernel route unavailable"
    )


def _parse_mapping_rates(stdout: str) -> tuple[float | None, float | None]:
    s = re.search(r"SINGLE_MAP_RATE:\s+([\d.]+)", stdout)
    a = re.search(r"ANY_MAP_RATE:\s+([\d.]+)", stdout)
    return (
        float(s.group(1)) if s else None,
        float(a.group(1)) if a else None,
    )


# NOTE: marimo 0.25.0 mutation helpers (setup_kernel_tees, run_marimo_sweep,
# validate_across_tiles, export_phase_html) are exposed as ASYNC functions
# because they're invoked from inside the marimo kernel scratchpad, which
# has a running event loop. The agent calls them via `await helper(...)`.
# Inside each helper, `async with cm.get_context() as ctx:` works natively —
# no asyncio.run, no worker threads, no thread-local context propagation
# needed. Reads (ctx.cells, cell.code) can still be done on a bare
# `ctx_factory()` snapshot.


async def run_marimo_sweep(
    session,
    param_name: str,
    values: Iterable,
    out_log: str | Path | None = None,
    prints_log: str | Path | None = None,
    timeout_s: float = 600.0,
):
    """Run a reactive sweep of `param_name ∈ values` against a live marimo
    session. Returns a pandas DataFrame with columns
    `(threshold, single_rate, any_rate, qc_pass, elapsed_s)`.

    Generalizes the Pass-3 `cm_sweep_one.py` workaround. For each value:
      1. Locate the marker-block cell via `find_marker_cell`.
      2. Rewrite the assignment line via `ctx.edit_cell`.
      3. `ctx.run_cell` and wait for the DAG to settle.
      4. Parse `SINGLE_MAP_RATE` / `ANY_MAP_RATE` from `prints_log` (or
         kernel stdout if no log path is given).
      5. Parse QC with baseline inheritance.
      6. Append a row to `out_log` (TSV) if given.

    `session` is a dict with at least `{"ctx_factory": callable}` returning a
    fresh marimo context. Tests can pass a fake; production callers pass
    `{"ctx_factory": marimo._code_mode.get_context}`.
    """
    import time as _time

    try:
        import pandas as pd
    except ImportError as e:
        raise RuntimeError("run_marimo_sweep requires pandas") from e

    ctx_factory = session["ctx_factory"]
    prints_path = Path(prints_log) if prints_log else None

    rows: list[dict] = []
    baseline_qc: dict | None = None
    for value in values:
        if prints_path is not None:
            prints_path.write_text("")  # truncate to capture only this iter

        ctx = ctx_factory()
        cell_id = find_marker_cell(ctx, param_name)
        if cell_id is None:
            raise RuntimeError(f"could not locate marker cell for {param_name}")
        cell = ctx.cells[cell_id] if hasattr(ctx.cells, "__getitem__") else None
        code = (cell.code if cell is not None else "") or ""

        new_lines: list[str] = []
        replaced = False
        assign_re = re.compile(rf"^(\s*){re.escape(param_name)}\s*=", re.IGNORECASE)
        for line in code.split("\n"):
            if assign_re.match(line):
                indent = line[: len(line) - len(line.lstrip())]
                new_lines.append(f"{indent}{param_name} = {value!r}  # sweep iter")
                replaced = True
            else:
                new_lines.append(line)
        if not replaced:
            raise RuntimeError(f"{param_name} assignment not found in cell {cell_id}")
        new_code = "\n".join(new_lines)

        t0 = _time.time()
        try:
            async with ctx_factory() as ctx_w:
                ctx_w.edit_cell(cell_id, code=new_code)
                ctx_w.run_cell(cell_id)
        except Exception as e:
            # a downstream cell raising for THIS value (e.g. zero reads → call_cells
            # KeyError) is "no rate" for the value, not the end of the band
            print(f"sweep {param_name}={value!r}: cell error: {e}")
        if _await_kernel_settle(ctx_factory, timeout_s=timeout_s) is False:
            raise SweepTimeout(
                f"{TIMEOUT_MARK}: {param_name}={value!r} did not settle within {timeout_s:.0f} s"
            )
        elapsed = _time.time() - t0

        if prints_path is not None and prints_path.exists():
            stdout = prints_path.read_text()
        else:
            stdout = ""
        single, any_ = _parse_mapping_rates(stdout)
        qc = parse_qc_block(stdout, baseline=baseline_qc)
        if qc["metrics"] and not qc["qc_inherited"]:
            baseline_qc = (
                qc  # update baseline only when current iter re-emitted metrics
            )

        row = {
            "threshold": value,
            "single_rate": single,
            "any_rate": any_,
            "qc_pass": qc["qc_pass"],
            "elapsed_s": round(elapsed, 1),
        }
        rows.append(row)
        if out_log:
            out_path = Path(out_log)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            header = not out_path.exists()
            with open(out_path, "a") as f:
                if header:
                    f.write("\t".join(row.keys()) + "\n")
                f.write(
                    "\t".join("" if v is None else str(v) for v in row.values()) + "\n"
                )

    return pd.DataFrame(rows)


async def validate_across_tiles(
    session,
    param_dict: dict,
    tiles: Iterable[int],
    parallel: bool = False,
    test_tile_var: str = "TEST_TILE",
    prints_log: str | Path | None = None,
    timeout_s: float = 600.0,
):
    """Validate the winning `param_dict` across multiple `tiles`. For each
    tile: sets the `TEST_TILE` marker, triggers reactive recompute, captures
    SINGLE_MAP_RATE + QC.

    Sequential implementation lands first; the `parallel` flag is reserved
    for Phase-2 N-kernel fan-out (raises NotImplementedError today so callers
    can opt in once that lands).

    Returns a pandas DataFrame with columns
    `(tile, single_rate, any_rate, qc_pass, elapsed_s)`.
    """
    if parallel:
        raise NotImplementedError(
            "parallel multi-tile validation is not yet implemented; "
            "Phase 2 of the Pass-3 plan lands N-kernel fan-out."
        )

    import time as _time

    try:
        import pandas as pd
    except ImportError as e:
        raise RuntimeError("validate_across_tiles requires pandas") from e

    ctx_factory = session["ctx_factory"]
    prints_path = Path(prints_log) if prints_log else None

    # If the operator passed extra winning params (not just TEST_TILE), set
    # them once up-front via edit_cell on each marker cell. This mirrors the
    # contract of the sweep helper (one-time mutation, then reactive replay).
    ctx = ctx_factory()
    for name, value in param_dict.items():
        cell_id = find_marker_cell(ctx, name)
        if cell_id is None:
            continue
        cell = ctx.cells[cell_id]
        code = cell.code or ""
        assign_re = re.compile(rf"^(\s*){re.escape(name)}\s*=", re.IGNORECASE)
        out_lines: list[str] = []
        for line in code.split("\n"):
            if assign_re.match(line):
                indent = line[: len(line) - len(line.lstrip())]
                out_lines.append(f"{indent}{name} = {value!r}  # winner")
            else:
                out_lines.append(line)
        async with ctx_factory() as ctx_w:
            ctx_w.edit_cell(cell_id, code="\n".join(out_lines))

    rows: list[dict] = []
    baseline_qc: dict | None = None
    for tile in tiles:
        if prints_path is not None:
            prints_path.write_text("")
        ctx = ctx_factory()
        cell_id = find_marker_cell(ctx, test_tile_var)
        if cell_id is None:
            raise RuntimeError(f"could not locate marker cell for {test_tile_var}")
        cell = ctx.cells[cell_id]
        code = cell.code or ""
        assign_re = re.compile(rf"^(\s*){re.escape(test_tile_var)}\s*=", re.IGNORECASE)
        new_lines: list[str] = []
        for line in code.split("\n"):
            if assign_re.match(line):
                indent = line[: len(line) - len(line.lstrip())]
                new_lines.append(f"{indent}{test_tile_var} = {tile!r}  # validate iter")
            else:
                new_lines.append(line)
        t0 = _time.time()
        async with ctx_factory() as ctx_w:
            ctx_w.edit_cell(cell_id, code="\n".join(new_lines))
            ctx_w.run_cell(cell_id)
        _await_kernel_settle(ctx_factory, timeout_s=timeout_s)
        elapsed = _time.time() - t0

        stdout = (
            prints_path.read_text() if (prints_path and prints_path.exists()) else ""
        )
        single, any_ = _parse_mapping_rates(stdout)
        qc = parse_qc_block(stdout, baseline=baseline_qc)
        if qc["metrics"] and not qc["qc_inherited"]:
            baseline_qc = qc

        rows.append(
            {
                "tile": tile,
                "single_rate": single,
                "any_rate": any_,
                "qc_pass": qc["qc_pass"],
                "elapsed_s": round(elapsed, 1),
            }
        )

    return pd.DataFrame(rows)


async def _fan_out_notebook_runs(
    jobs: list[dict],
    work_dir: str | Path,
    conda_env: str,
    omp_threads: int = 4,
    max_parallel: int = 4,
    timeout_s: float = 900.0,
    export_html: bool = False,
) -> list[dict]:
    """Copy → patch → subprocess → parse loop for a list of notebook jobs.

    Each job is a dict with:
        nb_path          : Path | str   — the source notebook to copy (not modified).
        patches          : dict[str, str] — {var_name: value_literal} mandatory
                           patches; any failure here is a job-level error.
        optional_patches : dict[str, str] — {var_name: value_literal} best-effort
                           patches; KeyError/ValueError are silently skipped.
        label            : dict — arbitrary key/value metadata echoed into each
                           returned row (e.g., {"plate": 1, "well": "A1"}).

    Returns a list of result dicts (one per job):
        label, single_rate, any_rate, elapsed_s, exit_code, error, log_fp,
        plus a ``stdout`` key with the raw log text.

    Failures (timeout, nonzero exit, patch error) surface as rows with
    ``error`` set — the function itself never raises on job-level failures.

    Hard stops honored:
    - Patches go on COPIES under work_dir, never on the original nb_path.
    - All artifacts written under work_dir (caller supplies a subdirectory
      inside the analysis dir — never /tmp).
    """
    import asyncio as _asyncio
    import shutil as _shutil
    import time as _time

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    sem = _asyncio.Semaphore(max(1, max_parallel))

    async def _run_one(job: dict) -> dict:
        nb_path = Path(job["nb_path"]).resolve()
        label = dict(job.get("label") or {})
        patches = dict(job.get("patches") or {})
        optional_patches = dict(job.get("optional_patches") or {})

        # Derive a slug from the label keys for artifact naming.
        slug_parts = "_".join(f"{k}{v}" for k, v in sorted(label.items()))
        slug = slug_parts or "job"
        nb_copy = work_dir / f"{slug}.py"
        log_fp = work_dir / f"{slug}.log"

        # Step 1 — copy the notebook; patch markers on the COPY only.
        _shutil.copy(nb_path, nb_copy)
        try:
            for var, val_literal in patches.items():
                patch_marker_block_value(nb_copy, var, val_literal)
            for var, val_literal in optional_patches.items():
                try:
                    patch_marker_block_value(nb_copy, var, val_literal)
                except (ValueError, KeyError):
                    pass  # optional — skip if marker block doesn't have this var
        except Exception as e:
            result = dict(label)
            result.update(
                single_rate=None, any_rate=None,
                elapsed_s=0.0, exit_code=None,
                error=f"patch failed: {type(e).__name__}: {e}",
                log_fp=str(log_fp), stdout="", label=label,
            )
            return result

        # Step 2 — spawn subprocess under conda activation, thread-capped env.
        env = os.environ.copy()
        env["OMP_NUM_THREADS"] = str(omp_threads)
        env["MKL_NUM_THREADS"] = str(omp_threads)
        env["OPENBLAS_NUM_THREADS"] = str(omp_threads)
        env["NUMEXPR_NUM_THREADS"] = str(omp_threads)
        if export_html:
            html_fp = nb_copy.with_suffix(".html")
            inner_cmd = (
                f'exec python -m marimo export html "{nb_copy}" '
                f'-o "{html_fp}" --no-include-code'
            )
        else:
            inner_cmd = f'exec python "{nb_copy}"'
        cmd = [
            "bash", "-c",
            f'eval "$(conda shell.bash hook)" && conda activate {conda_env} && '
            + inner_cmd,
        ]

        async with sem:
            t0 = _time.time()
            log_file = open(log_fp, "wb")
            try:
                proc = await _asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=log_file,
                    stderr=_asyncio.subprocess.STDOUT,
                    env=env,
                    cwd=str(nb_path.parent),
                )
                try:
                    rc = await _asyncio.wait_for(proc.wait(), timeout=timeout_s)
                    error = None if rc == 0 else f"exit code {rc}"
                except _asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    rc = None
                    error = f"timeout after {timeout_s}s"
            finally:
                log_file.close()
            elapsed = _time.time() - t0

        # Step 3 — parse the log for mapping rates.
        try:
            log_text = log_fp.read_text(errors="replace")
        except Exception:
            log_text = ""
        single, any_ = _parse_mapping_rates(log_text)

        result = dict(label)
        result.update(
            single_rate=single, any_rate=any_,
            elapsed_s=round(elapsed, 1), exit_code=rc,
            error=error, log_fp=str(log_fp), stdout=log_text,
            label=label,
        )
        return result

    tasks = [_run_one(job) for job in jobs]
    rows = list(await _asyncio.gather(*tasks))
    return rows


async def validate_tiles_headless(
    nb_path: str | Path,
    winners: dict[str, Any],
    tiles: Iterable[tuple],
    work_dir: str | Path,
    conda_env: str,
    omp_threads: int = 4,
    max_parallel: int = 4,
    timeout_s: float = 900.0,
    keep_artifacts: bool = True,
    export_html: bool = False,
):
    """Validate `winners` marker values across `tiles` in parallel headless
    subprocesses. Each tile runs an isolated copy of the phase notebook
    (marimo `app.run()` script-mode) with TEST_PLATE/TEST_WELL/TEST_TILE
    + every (k, v) in `winners` patched into its marker blocks. Captures
    stdout per tile to a per-job slug derived from the job label
    (e.g. `plate{plate}_tile{tile}_well{well}.log` under `work_dir`)
    and parses QC + mapping rates with `parse_qc_block` + `_parse_mapping_rates`.

    Design rationale (vs. live-kernel `validate_across_tiles`):

    - **Failure isolation.** A crashed/timed-out subprocess can't poison
      the live marimo kernel or sibling validation runs.
    - **Parallelism.** N tiles run concurrently (throttled by
      `max_parallel`); wall-clock is ~max-tile-time, not sum-of-tile-times.
    - **Sampling locality.** Pairs naturally with
      `select_validation_tiles_from_metadata` for cross-well /
      cross-plate coverage — the live-kernel path can't easily flip
      TEST_PLATE / TEST_WELL between iters.

    Hard stops honored (CLAUDE.md):
    - Patches go on COPIES of the notebook under `work_dir`, never on the
      live `nb_path` (which may be open in a running marimo kernel).
    - All files in `work_dir` (caller passes `<analysis-dir>/.brieflow/parallel/`),
      never `/tmp`.

    Args:
        nb_path: the operator's phase notebook (template for copies; not modified).
        winners: marker-block values to bake in (e.g. {"THRESHOLD_READS": 700}).
            Case-insensitive on key names; matches notebook markers via
            `patch_marker_block_value`.
        tiles: iterable of (plate, well, tile) tuples. plate may be int or str;
            well is "A1" form; tile is int.
        work_dir: destination for per-tile `.py` + `.log` artifacts.
        conda_env: screen.yaml `pipeline.conda_env` — used to activate before
            subprocess `python` invocation.
        omp_threads: per-subprocess cap on OMP/MKL/OPENBLAS threads. Default 4.
        max_parallel: max concurrent subprocesses. Default 4. cellpose-CPU
            saturates ~6-8 cores per run; on shared HPC, keep low.
        timeout_s: per-subprocess timeout. Default 900s (15 min).
        keep_artifacts: if True (default), leave `.py` + `.log` files for
            debug. If False, remove them after parsing.
        export_html: if True (default False), each per-tile subprocess runs
            `marimo export html <copy>.py -o <copy>.html --no-include-code`
            instead of `python <copy>.py`. Same DAG run; HTML is a side
            effect. Useful when a validation tile flags a borderline QC
            value and the operator wants to inspect the viz cells
            (alignment overlays, segmentation masks) without re-running
            the DAG by hand. stdout still streams to `<copy>.log` so QC
            parsing is unchanged.

    Returns:
        pd.DataFrame with columns:
            plate, well, tile, single_rate, any_rate, qc_pass, qc_failed_keys,
            elapsed_s, exit_code, error, log_fp

        Subprocess failures (timeout, nonzero exit, missing metric) appear as
        rows with the relevant `error` set — caller's Step 7 acceptance logic
        classifies them. The helper itself never raises on tile-level failure.
    """
    try:
        import pandas as pd
    except ImportError as e:
        raise RuntimeError("validate_tiles_headless requires pandas") from e

    nb_path = Path(nb_path).resolve()
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    tiles_list = list(tiles)
    if not tiles_list:
        return pd.DataFrame(columns=[
            "plate", "well", "tile", "single_rate", "any_rate", "qc_pass",
            "qc_failed_keys", "elapsed_s", "exit_code", "error", "log_fp",
        ])

    # Build one job per tile — patches include tile coords + all winners.
    # Optional winner patches use the same silent-skip contract as before
    # (KeyError/ValueError on a missing marker block key → skip).
    # _fan_out_notebook_runs raises on the first bad patch; we need
    # winners to be silent-skip, so we split mandatory vs. optional patches.
    jobs = []
    for plate, well, tile in tiles_list:
        patches: dict[str, str] = {
            "TEST_PLATE": repr(plate),
            "TEST_WELL": repr(well),
            "TEST_TILE": repr(int(tile)),
        }
        # Winners are optional: if the marker block doesn't have the key we
        # skip. Encode this as a separate "optional_patches" field so the
        # primitive can handle them gracefully.
        optional = {k: repr(v) for k, v in winners.items()}
        jobs.append({
            "nb_path": nb_path,
            "patches": patches,
            "optional_patches": optional,
            "label": {"plate": plate, "well": well, "tile": tile},
        })

    rows = await _fan_out_notebook_runs(
        jobs,
        work_dir=work_dir,
        conda_env=conda_env,
        omp_threads=omp_threads,
        max_parallel=max_parallel,
        timeout_s=timeout_s,
        export_html=export_html,
    )

    # Augment rows: parse QC block from stdout, apply SBS-only metric check.
    expects_mapping_rate = nb_path.name.endswith("_sbs.py")
    _nb_phase = _phase_from_nb_path(nb_path)
    _nb_gates: dict | None = None
    if _nb_phase is not None:
        try:
            import brieflow_qc as _bqc  # type: ignore[import]
            _nb_gates = _bqc.load_qc_gates()
        except Exception:
            pass
    result_rows = []
    for r in rows:
        log_text = r.get("stdout", "")
        error = r.get("error")
        single = r.get("single_rate")
        any_ = r.get("any_rate")
        qc = parse_qc_block(log_text, gates=_nb_gates, phase=_nb_phase)
        if expects_mapping_rate and single is None and error is None:
            error = "SINGLE_MAP_RATE not found in subprocess stdout"
        label = r.get("label", {})
        result_rows.append({
            "plate": label.get("plate"),
            "well": label.get("well"),
            "tile": label.get("tile"),
            "single_rate": single,
            "any_rate": any_,
            "qc_pass": qc.get("qc_pass"),
            "qc_failed_keys": qc.get("failed", []),
            "elapsed_s": r.get("elapsed_s"),
            "exit_code": r.get("exit_code"),
            "error": error,
            "log_fp": r.get("log_fp"),
        })

    # Optional cleanup of patched .py + log (+ html if exported) files.
    if not keep_artifacts:
        for r in result_rows:
            try:
                log = Path(r["log_fp"])
                log.unlink(missing_ok=True)
                log.with_suffix(".py").unlink(missing_ok=True)
                if export_html:
                    log.with_suffix(".html").unlink(missing_ok=True)
            except Exception:
                pass

    return pd.DataFrame(result_rows)


async def export_phase_html(
    notebook_path: str | Path,
    out_path: str | Path,
    conda_env: str | None = None,
    session=None,
) -> dict:
    """Export a phase notebook to HTML-WASM for operator review.

    Runs `ctx.save()` first if a live `session` (with `ctx_factory`) is
    given — guarantees the in-kernel mutations (sweep winners, tile choices)
    are flushed to disk before the export reads the file. Then invokes
    `marimo export html-wasm`.

    Returns `{"ok": bool, "out_path": str, "stdout": str, "stderr": str}`.
    """
    nb = Path(notebook_path).resolve()
    out = Path(out_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    if session is not None:
        try:
            async with session["ctx_factory"]() as ctx:
                if callable(getattr(ctx, "save", None)):
                    ctx.save()
        except Exception:
            pass  # save failures are non-fatal — export still tries the on-disk copy

    if conda_env:
        cmd = [
            "bash",
            "-c",
            f'eval "$(conda shell.bash hook)" && conda activate {conda_env} && '
            f'python -m marimo export html-wasm "{nb}" -o "{out}" --force',
        ]
    else:
        cmd = [
            sys.executable,
            "-m",
            "marimo",
            "export",
            "html-wasm",
            str(nb),
            "-o",
            str(out),
            "--force",
        ]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=nb.parent)
    return {
        "ok": proc.returncode == 0,
        "out_path": str(out),
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


async def sweep_headless(
    nb_path: str | Path,
    param_name: str,
    values: Iterable,
    tiles: Iterable[tuple],
    work_dir: str | Path,
    conda_env: str,
    omp_threads: int = 4,
    max_parallel: int = 4,
    timeout_s: float = 900.0,
    export_html: bool = False,
):
    """Headless parameter sweep over `values` × `tiles`.

    For each (value, tile) pair a notebook copy is run with
    `param_name` patched to `repr(value)` plus the tile coordinates
    (TEST_PLATE / TEST_WELL / TEST_TILE) patched as mandatory patches.
    Results are aggregated by value to produce one summary row per value.

    Args:
        nb_path: phase notebook (.py) used as the template for copies.
        param_name: the operator-parameters variable to sweep.
        values: sequence of candidate values to test.
        tiles: iterable of (plate, well, tile) tuples.
        work_dir: directory for per-job artifacts (must be inside the
            analysis dir, never /tmp per CLAUDE.md).
        conda_env: conda env name to activate for subprocesses.
        omp_threads: per-subprocess OMP/MKL/OPENBLAS thread cap.
        max_parallel: max concurrent subprocesses.
        timeout_s: per-subprocess timeout in seconds.
        export_html: if True, export HTML as well as running the notebook.

    Returns:
        pd.DataFrame with columns:
            value, mean_single_rate, std_single_rate, n_tiles, qc_pass_frac
    """
    try:
        import pandas as pd
    except ImportError as e:
        raise RuntimeError("sweep_headless requires pandas") from e

    nb_path = Path(nb_path).resolve()
    values_list = list(values)
    tiles_list = list(tiles)

    if not values_list or not tiles_list:
        return pd.DataFrame(columns=[
            "value", "mean_single_rate", "std_single_rate", "n_tiles", "qc_pass_frac",
        ])

    # Build one job per (value, tile).
    jobs = []
    for value in values_list:
        for plate, well, tile in tiles_list:
            label = {
                "value": value,
                "plate": plate,
                "well": well,
                "tile": tile,
            }
            patches = {
                "TEST_PLATE": repr(plate),
                "TEST_WELL": repr(well),
                "TEST_TILE": repr(int(tile)),
                param_name: repr(value),
            }
            jobs.append({
                "nb_path": nb_path,
                "patches": patches,
                "label": label,
            })

    rows = await _fan_out_notebook_runs(
        jobs,
        work_dir=work_dir,
        conda_env=conda_env,
        omp_threads=omp_threads,
        max_parallel=max_parallel,
        timeout_s=timeout_s,
        export_html=export_html,
    )

    # Resolve tunable sweep thresholds once for this notebook/phase.
    _sw_phase = _phase_from_nb_path(nb_path)
    _sw_gates: dict | None = None
    if _sw_phase is not None:
        try:
            import brieflow_qc as _bqc  # type: ignore[import]
            _sw_gates = _bqc.load_qc_gates()
        except Exception:
            pass

    # Parse QC from stdout and build a per-job record.
    records = []
    for r in rows:
        log_text = r.get("stdout", "")
        qc = parse_qc_block(log_text, gates=_sw_gates, phase=_sw_phase)
        label = r.get("label", {})
        records.append({
            "value": label.get("value"),
            "single_rate": r.get("single_rate"),
            "qc_pass": bool(qc.get("qc_pass")),
        })

    df_jobs = pd.DataFrame(records)
    if df_jobs.empty:
        return pd.DataFrame(columns=[
            "value", "mean_single_rate", "std_single_rate", "n_tiles", "qc_pass_frac",
        ])

    def _agg(g):
        rates = g["single_rate"].dropna()
        return pd.Series({
            "mean_single_rate": rates.mean() if len(rates) > 0 else None,
            "std_single_rate": rates.std(ddof=0) if len(rates) > 1 else 0.0,
            "n_tiles": len(g),
            "qc_pass_frac": g["qc_pass"].mean(),
        })

    summary = df_jobs.groupby("value", sort=False).apply(_agg, include_groups=False).reset_index()
    summary.attrs["evidence"] = _finite_counts(df_jobs, ["value"])
    return summary


# A winner must rest on data: at least this share of trials produced a finite rate, and the
# winning value was finite on at least this many tiles. Otherwise the parameter is unresolved.
SWEEP_MIN_FINITE_FRAC = 0.5
SWEEP_MIN_FINITE_TILES = 2


def _finite_counts(rows, keys: list[str]) -> dict:
    """Per sweep point: trials run, finite rates, and distinct tiles with a finite rate."""
    tiles = "tile" if "tile" in rows.columns else None
    counts = {}
    for key, g in rows.groupby(keys, sort=False):
        finite = g[g["single_rate"].notna()]
        counts[key if isinstance(key, tuple) and len(keys) > 1 else _scalar(key)] = {
            "trials": len(g),
            "finite": len(finite),
            "tiles": finite[tiles].nunique() if tiles else len(finite),
        }
    return counts


def _scalar(key):
    return key[0] if isinstance(key, tuple) else key


def sweep_evidence(df, winner=None) -> tuple[bool, str]:
    """Check a sweep summary against the evidence floor; returns (ok, detail).

    `winner` is the chosen value (or an (outer, inner) pair for a joint sweep). The counts
    come from `df.attrs["evidence"]`; summaries without them (older callers) pass."""
    counts = getattr(df, "attrs", {}).get("evidence") if df is not None else None
    if not counts:
        return True, "no finite counts recorded"
    trials = sum(c["trials"] for c in counts.values())
    finite = sum(c["finite"] for c in counts.values())
    frac = finite / trials if trials else 0.0
    if winner is None:
        tiles = max(c["tiles"] for c in counts.values())
    else:
        tiles = (counts.get(winner) or {}).get("tiles", 0)
    detail = (
        f"{finite}/{trials} trials finite ({frac:.0%}); winner finite on {tiles} tile(s); "
        f"floor {SWEEP_MIN_FINITE_FRAC:.0%} and {SWEEP_MIN_FINITE_TILES} tiles"
    )
    return frac >= SWEEP_MIN_FINITE_FRAC and tiles >= SWEEP_MIN_FINITE_TILES, detail


def pick_sweep_winner(df, metric: str = "mean_single_rate"):
    """Return the value in `df` that maximizes `metric`.

    Tie-break: when multiple values share the maximum metric, return the
    smallest value (numerically stable, avoids platform-dependent order).

    Args:
        df: DataFrame returned by `sweep_headless`, with columns ``value``
            and at least the column named by `metric`.
        metric: column name to maximize. Default ``mean_single_rate``.

    Returns:
        The ``value`` entry (same type as in `df["value"]`) that maximizes
        the metric.
    """
    if df.empty:
        raise ValueError("pick_sweep_winner: DataFrame is empty")
    # Drop trials whose metric is NaN (a crashed sweep run produces no rate). An
    # all-NaN sweep must RAISE, not return NaN — otherwise the caller records nan
    # as tuned-confirmed and patches a bare `nan` literal into the notebook
    # (NameError next run; resume reuses the garbage). See OUTSTANDING #12.
    valid = df[df[metric].notna()]
    if valid.empty:
        raise ValueError(
            f"pick_sweep_winner: no finite {metric} — all sweep trials failed"
        )
    best_metric = valid[metric].max()
    candidates = valid[valid[metric] == best_metric]
    return candidates["value"].min()


# ---------------------------------------------------------------------------
# Cluster resolution selection — agent picks FINAL_LEIDEN_RESOLUTIONS from the
# resolution table printed by lib.cluster.benchmark_clusters.evaluate_resolution.
# The notebook is NOT auto-wired; the agent reads the output and patches the
# marker (or an operator hand-sets it). See brieflow-notebook.
# ---------------------------------------------------------------------------

_RESOLUTION_LINE_RE = re.compile(
    r"resolution\s+([\d.]+):\s+(\d+)\s+clusters,\s+median genes/cluster\s+([\d.]+)",
    re.IGNORECASE,
)


def parse_resolution_table(stdout: str) -> list[dict]:
    """Parse evaluate_resolution's printed per-resolution lines into rows.

    Each matched line ``resolution R: N clusters, median genes/cluster M``
    becomes ``{"resolution": R, "n_clusters": N, "median_genes_per_cluster": M}``.
    Resolutions are returned as int when integer-valued, else float.
    """
    rows: list[dict] = []
    for m in _RESOLUTION_LINE_RE.finditer(stdout or ""):
        res = float(m.group(1))
        if res.is_integer():
            res = int(res)
        rows.append({
            "resolution": res,
            "n_clusters": int(m.group(2)),
            "median_genes_per_cluster": float(m.group(3)),
        })
    return rows


def select_final_resolutions(
    rows: list[dict], target: int = 20, tolerance: int = 5
) -> list:
    """Pick FINAL_LEIDEN_RESOLUTIONS as a fine (step-1) sweep around the optimum.

    The coarse test sweep brackets widely (e.g. …,5,7,9,11,13,…); the resolutions
    whose ``median_genes_per_cluster`` lands in ``[target ± tolerance]`` (the
    "core") are usually just 1–2 — too few for the operator to compare granularity.
    So instead of returning only the core, expand to a **step-1 integer range
    around it** (the same coarse→fine idea the sbs/phenotype sweeps use):

      core = resolutions with median in [target±tolerance]  (or the single closest)
      return range(min(core) - pad, max(core) + pad)   step 1, clamped to > 0

    where ``pad`` is 1 when the core has ≥2 resolutions, else 2 — so the result is
    ~5 fine-grained resolutions. E.g. a core of ``[7, 9]`` → ``[6, 7, 8, 9, 10]``.
    """
    if not rows:
        return []
    lo_t, hi_t = target - tolerance, target + tolerance
    core = [
        r["resolution"]
        for r in rows
        if lo_t <= r["median_genes_per_cluster"] <= hi_t
    ]
    if not core:
        closest = min(rows, key=lambda r: abs(r["median_genes_per_cluster"] - target))
        core = [closest["resolution"]]
    pad = 1 if len(core) >= 2 else 2
    lo = int(round(min(core))) - pad
    hi = int(round(max(core))) + pad
    return sorted({res for res in range(lo, hi + 1) if res > 0})


def cluster_target_miss(rows: list[dict], target: int = 20, tolerance: int = 5) -> dict | None:
    """None when some tested resolution lands in ``target ± tolerance`` genes/cluster;
    otherwise what the closest one reached, and which way the tested range falls short."""
    if not rows:
        return None
    lo_t, hi_t = target - tolerance, target + tolerance
    if any(lo_t <= r["median_genes_per_cluster"] <= hi_t for r in rows):
        return None
    closest = min(rows, key=lambda r: abs(r["median_genes_per_cluster"] - target))
    medians = [r["median_genes_per_cluster"] for r in rows]
    return {
        "closest_resolution": closest["resolution"],
        "closest_median": closest["median_genes_per_cluster"],
        "tested": [min(r["resolution"] for r in rows), max(r["resolution"] for r in rows)],
        "largest_median": max(medians),
        "smallest_median": min(medians),
    }


def refine_points(winner, radius, step, lo, hi, exclude=()):
    """Candidate values within ``±radius`` of ``winner`` at ``step`` spacing.

    Generates ``winner + k*step`` for ``k`` in ``-n..n`` (``n = round(radius/
    step)``), clamps to ``[lo, hi]``, drops anything in ``exclude`` (and the
    winner itself, which the coarse pass already tested), and returns a sorted
    unique list. Integer-valued points are normalized to ``int`` so they match
    integer coarse-band entries during exclusion. Used by the coarse-to-fine
    layer in `adaptive_sweep_headless` to tile the gap around the coarse winner.
    """
    if step <= 0:
        return []
    n = int(round(radius / step))
    excl = set(exclude)
    excl.add(winner)
    seen: set = set()
    out = []
    for k in range(-n, n + 1):
        v = winner + k * step
        if float(v).is_integer():
            v = int(v)
        if v < lo or v > hi or v in excl or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return sorted(out)


async def adaptive_sweep_headless(
    nb_path: str | Path,
    param_name: str,
    spec: dict,
    tiles: Iterable[tuple],
    work_dir: str | Path,
    conda_env: str,
    **kwargs,
):
    """Coarse-to-fine headless sweep over an adaptive band ``spec``.

    ``spec`` is a dict with keys:
        coarse        : list of coarse candidate values (required)
        refine_radius : search radius around the coarse winner
        refine_step   : spacing of the refine pass
        min, max      : hard clamps for the refine pass (default: coarse range)

    Runs `sweep_headless` on the coarse band, picks the winner, computes
    `refine_points` around it, runs a second `sweep_headless` on those, and
    returns the concatenated summary DataFrame (one row per value). The caller
    runs `pick_sweep_winner` over the union exactly as for a fixed sweep.

    Coarse and refine values are disjoint (refine excludes coarse), so their
    per-job artifacts never collide in ``work_dir``. If the coarse pass yields
    no rows, or no refine points exist, the coarse DataFrame is returned as-is.
    """
    try:
        import pandas as pd
    except ImportError as e:
        raise RuntimeError("adaptive_sweep_headless requires pandas") from e

    coarse = list(spec.get("coarse") or [])
    df_coarse = await sweep_headless(
        nb_path, param_name, coarse, tiles,
        work_dir=work_dir, conda_env=conda_env, **kwargs,
    )
    radius = spec.get("refine_radius")
    step = spec.get("refine_step")
    if df_coarse.empty or radius is None or step is None or not coarse:
        return df_coarse

    winner = pick_sweep_winner(df_coarse)
    lo = spec.get("min", min(coarse))
    hi = spec.get("max", max(coarse))
    refine = refine_points(winner, radius, step, lo, hi, exclude=coarse)
    if not refine:
        return df_coarse

    df_refine = await sweep_headless(
        nb_path, param_name, refine, tiles,
        work_dir=work_dir, conda_env=conda_env, **kwargs,
    )
    combined = pd.concat([df_coarse, df_refine], ignore_index=True)
    return combined.drop_duplicates(subset="value", keep="first").reset_index(drop=True)


_CELL_RUNNER = Path(__file__).resolve().parent / "brieflow_cell_runner.py"


def _cached_tile_trials(nb_path, tile, trials, work_dir, conda_env, omp_threads, timeout_s,
                        label, failures):
    """One `brieflow_cell_runner` process for one tile: prime once, then every trial.
    Returns the TSV rows, or None (failure recorded) when the process does not finish."""
    import json as _json
    import shutil as _shutil

    import pandas as pd

    plate, well, t = tile
    slug = f"{label}_p{plate}_w{well}_t{t}"
    nb_copy = work_dir / f"{slug}.py"
    out = work_dir / f"{slug}.tsv"
    try:
        _shutil.copy(nb_path, nb_copy)
        for name, value in (("TEST_PLATE", plate), ("TEST_WELL", well), ("TEST_TILE", int(t))):
            patch_marker_block_value(nb_copy, name, repr(value))
    except Exception as e:
        failures.append((slug, "patch", str(e)))
        return None
    env = os.environ.copy()
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                "NUMEXPR_NUM_THREADS"):
        env[var] = str(omp_threads)
    inner = (
        f'exec python "{_CELL_RUNNER}" --notebook "{nb_copy}" --cwd "{nb_path.parent}" '
        f"--trials '{_json.dumps(trials)}' --out \"{out}\""
    )
    cmd = ["bash", "-c", f'eval "$(conda shell.bash hook)" && conda activate {conda_env} && '
           + inner]
    limit = baseline_timeout(timeout_s, tile_work_mpx(nb_path.parent, plate, well, t))
    limit += float(timeout_s) * len(trials)
    try:
        with open(work_dir / f"{slug}.log", "wb") as log:
            proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, env=env,
                                  cwd=str(nb_path.parent), timeout=limit)
    except subprocess.TimeoutExpired as e:
        failures.append((slug, "timeout", f"{TIMEOUT_MARK}: {e}"))
        return None
    if not out.exists():
        failures.append((slug, "error", f"exit code {proc.returncode}; see {slug}.log"))
        return None
    rows = pd.read_csv(out, sep="\t").to_dict("records")
    for row in rows:
        row["tile"] = slug
    return rows


async def cached_sweep_headless(
    nb_path: str | Path,
    param_name: str,
    values: Iterable,
    tiles: Iterable[tuple],
    work_dir: str | Path,
    conda_env: str,
    omp_threads: int = 4,
    max_parallel: int = 4,
    timeout_s: float = 900.0,
    **kwargs,
):
    """Headless band sweep that primes each tile once (`brieflow_cell_runner`): imports,
    alignment and segmentation run once per tile, then each value reruns only the cells
    downstream of the marker that the metric needs. Same return shape as `sweep_headless`.
    Raises SweepTimeout when every tile stopped on its time limit."""
    import asyncio as _asyncio

    import pandas as pd

    nb_path = Path(nb_path).resolve()
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    vals = [v.item() if hasattr(v, "item") else v for v in values]
    tiles_list = list(tiles)
    cols = ["value", "mean_single_rate", "std_single_rate", "n_tiles", "qc_pass_frac"]
    if not tiles_list or not vals:
        return pd.DataFrame(columns=cols)
    failures: list = []
    trials = [{param_name: v} for v in vals]
    label = f"cached_{param_name}_{abs(hash(tuple(vals))) % 10**6}"
    results = await _asyncio.to_thread(
        _run_tiles,
        lambda *t: _cached_tile_trials(nb_path, t, trials, work_dir, conda_env, omp_threads,
                                       timeout_s, label, failures),
        tiles_list,
        max_parallel,
    )
    rows = [r for res in results if res for r in res]
    if not rows:
        if failures and all(kind == "timeout" for _, kind, _ in failures):
            raise _no_rate_error("cached_sweep_headless", len(tiles_list), 0, failures, timeout_s)
        return pd.DataFrame(columns=cols)
    jobs = pd.DataFrame(rows).rename(columns={param_name: "value"})
    jobs["qc_pass"] = jobs["qc_pass"].astype(str).str.lower() == "true"

    def _agg(g):
        rates = g["single_rate"].dropna()
        return pd.Series({
            "mean_single_rate": rates.mean() if len(rates) > 0 else None,
            "std_single_rate": rates.std(ddof=0) if len(rates) > 1 else 0.0,
            "n_tiles": len(g),
            "qc_pass_frac": g["qc_pass"].mean(),
        })

    summary = jobs.groupby("value", sort=False).apply(_agg, include_groups=False).reset_index()
    summary.attrs["evidence"] = _finite_counts(jobs, ["value"])
    return summary


async def adaptive_cached_sweep(
    nb_path: str | Path,
    param_name: str,
    spec: dict,
    tiles: Iterable[tuple],
    work_dir: str | Path,
    conda_env: str,
    **kwargs,
):
    """Coarse band, then one refine band around the cross-tile coarse winner (the same
    points on every tile), each through `cached_sweep_headless`."""
    import pandas as pd

    coarse = list(spec.get("coarse") or [])
    df_coarse = await cached_sweep_headless(
        nb_path, param_name, coarse, tiles, work_dir=work_dir, conda_env=conda_env, **kwargs
    )
    radius, step = spec.get("refine_radius"), spec.get("refine_step")
    if radius is None or step is None or df_coarse.empty:
        return df_coarse
    try:
        winner = pick_sweep_winner(df_coarse)
    except ValueError:
        return df_coarse
    winner = winner.item() if hasattr(winner, "item") else winner
    refine = refine_points(winner, radius, step, spec.get("min", min(coarse)),
                           spec.get("max", max(coarse)), exclude=coarse)
    if not refine:
        return df_coarse
    df_refine = await cached_sweep_headless(
        nb_path, param_name, refine, tiles, work_dir=work_dir, conda_env=conda_env, **kwargs
    )
    evidence = {**df_coarse.attrs.get("evidence", {}), **df_refine.attrs.get("evidence", {})}
    out = pd.concat([df_coarse, df_refine], ignore_index=True).drop_duplicates(
        subset="value", keep="first"
    ).reset_index(drop=True)
    out.attrs["evidence"] = evidence
    return out


# ---------------------------------------------------------------------------
# Reactive (live-kernel) coarse-to-fine multi-tile sweep — segment once per
# tile, then mutate only the downstream marker. Drop-in for the headless path.
# ---------------------------------------------------------------------------


_SCRIPTS_DIR = Path(__file__).resolve().parent
_EXECUTE_CODE = _SCRIPTS_DIR / "marimo-pair" / "execute-code.sh"


def _execute_in_kernel(
    port: int, code: str, timeout_s: float, ok_marker: str | None = None
) -> str:
    """Run `code` INSIDE the live marimo kernel on `port` via the marimo-pair
    bridge (`execute-code.sh` → POST /api/kernel/execute) and return its stdout.

    ``ok_marker``: when given and present in stdout, the call is accepted even
    if the bridge exits non-zero. The bridge reports ``success: false`` when ANY
    notebook cell raised during the execution — e.g. a sweep value that leaves
    zero reads makes the downstream `call_cells` cell raise — but the injected
    sweep code itself completed (rows written, marker printed) and the kernel is
    healthy. The raised cell is simply "no rate" for that value, which is data.

    This is the seam that makes the reactive sweep work out-of-process:
    `marimo._code_mode.get_context()` only attaches from *inside* the kernel,
    so everything that needs a context (tees, baseline run, `run_marimo_sweep`)
    is injected through here rather than called from the runner process —
    the same route `tests/test_marimo_live.py` proves against a real kernel.
    Raises RuntimeError on a non-zero bridge exit.

    The server is addressed by ``--url`` (we own the port), never by the
    bridge's registry discovery: the registry lives in ``$HOME`` (shared NFS on
    a cluster) and its liveness check is a host-local ``kill -0``, so a bridge
    running on another node sees this kernel's pid as dead and drops its
    registration — surfacing here as "No running marimo instances found"
    while the kernel is alive.
    """
    import time as _time

    for attempt in range(3):
        res = subprocess.run(
            ["bash", str(_EXECUTE_CODE), "--url", f"http://127.0.0.1:{port}", "-c", code],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        # a session that is still attaching / a server still binding is transient
        transient = res.returncode != 0 and (
            "No active sessions" in res.stderr or "Failed to connect" in res.stderr
        )
        if not transient or attempt == 2:
            break
        _time.sleep(5.0 * (attempt + 1))
    if res.returncode != 0 and not (ok_marker and ok_marker in res.stdout):
        raise RuntimeError(
            f"in-kernel execution failed (rc={res.returncode}): "
            f"{res.stderr.strip()[-800:]}"
        )
    return res.stdout


def _kernel_preamble() -> str:
    """Preamble for injected code: make the plugin scripts importable in the
    kernel, allow `asyncio.run` inside its already-running loop, and expose the
    in-kernel context factory. nest_asyncio must exist in the screen env; a
    clear error here surfaces as a tile failure → headless fallback."""
    return (
        "import sys, asyncio\n"
        "try:\n"
        "    import nest_asyncio\n"
        "except ImportError as _e:\n"
        "    raise RuntimeError('nest_asyncio is required in the screen env "
        "for the reactive sweep') from _e\n"
        "nest_asyncio.apply()\n"
        f"sys.path.insert(0, {str(_SCRIPTS_DIR)!r})\n"
        "import marimo._code_mode as cm\n"
    )


def _kernel_baseline(port: int, prints_log: str | Path, timeout_s: float) -> None:
    """Install the library print tees, then run EVERY cell once and wait for
    the DAG to settle. The kernel boots STALE — `start_marimo_kernel` does not
    execute cells — so this baseline is the one segmentation paid per tile."""
    code = (
        _kernel_preamble()
        + "from brieflow_wizard import setup_kernel_tees, _await_kernel_settle\n"
        + "asyncio.run(setup_kernel_tees({'ctx_factory': cm.get_context}, "
        + f"{str(prints_log)!r}))\n"
        + "ids = [c.id for c in cm.get_context().cells]\n"
        + "async def _r():\n"
        + "    async with cm.get_context() as cw:\n"
        + "        for cid in ids:\n"
        + "            cw.run_cell(cid)\n"
        + "asyncio.run(_r())\n"
        + f"if not _await_kernel_settle(cm.get_context, timeout_s={float(timeout_s)}):\n"
        + f"    raise TimeoutError('{TIMEOUT_MARK}: baseline did not settle within "
        + f"{float(timeout_s):.0f} s')\n"
        + "print('BASELINE_OK', len(ids))\n"
    )
    out = _execute_in_kernel(port, code, timeout_s=timeout_s + 120.0, ok_marker="BASELINE_OK")
    if "BASELINE_OK" not in out:
        raise RuntimeError(f"baseline run did not complete: {out[-800:]}")


def _kernel_sweep(
    port: int,
    param_name: str,
    values: Iterable,
    out_log: str | Path,
    prints_log: str | Path,
    timeout_s: float,
):
    """Run `run_marimo_sweep` over `values` INSIDE the kernel (where its
    ctx_factory attaches) and read the per-value TSV it writes back into a
    DataFrame with the `run_marimo_sweep` columns
    (threshold, single_rate, any_rate, qc_pass, elapsed_s)."""
    import pandas as pd

    out_log = Path(out_log)
    if out_log.exists():
        out_log.unlink()  # one fresh TSV per band; run_marimo_sweep appends
    vals = [v.item() if hasattr(v, "item") else v for v in values]
    code = (
        _kernel_preamble()
        + "from brieflow_wizard import run_marimo_sweep\n"
        + "df = asyncio.run(run_marimo_sweep(\n"
        + "    session={'ctx_factory': cm.get_context},\n"
        + f"    param_name={param_name!r},\n"
        + f"    values={vals!r},\n"
        + f"    out_log={str(out_log)!r},\n"
        + f"    prints_log={str(prints_log)!r},\n"
        + f"    timeout_s={float(timeout_s)},\n"
        + "))\n"
        + "print('SWEEP_OK', len(df))\n"
    )
    out = _execute_in_kernel(
        port, code, timeout_s=timeout_s * (len(vals) + 1) + 120.0, ok_marker="SWEEP_OK"
    )
    if "SWEEP_OK" not in out:
        raise RuntimeError(f"in-kernel sweep did not complete: {out[-800:]}")
    if not out_log.exists():
        return pd.DataFrame(
            columns=["threshold", "single_rate", "any_rate", "qc_pass", "elapsed_s"]
        )
    return pd.read_csv(out_log, sep="\t")


_KERNEL_START_LOCK = threading.Lock()
DEFAULT_MAX_PARALLEL_KERNELS = 3


def _start_tile_kernel(nb_copy: Path, conda_env: str, cwd: Path):
    """Bring a sweep kernel up under a process-wide lock: `_free_port` picks a
    port before marimo binds it, so concurrent bring-ups could otherwise race
    for the same port. Bring-up is seconds; a tile's sweep is minutes."""
    with _KERNEL_START_LOCK:
        return start_marimo_kernel(nb_copy, conda_env=conda_env, watch=False, cwd=cwd)


def _log_tile_error(work_dir: Path, slug: str, stage: str) -> None:
    """Append the current exception's traceback to ``<slug>.error.log`` so an
    isolated tile/band failure is diagnosable, never silent."""
    import traceback

    with open(Path(work_dir) / f"{slug}.error.log", "a") as f:
        f.write(f"=== {stage} ===\n{traceback.format_exc()}\n")


# Seconds of baseline (imports, load, align, cellpose diameters + segmentation, calling) per
# megapixel of tile image data, measured on 4 cores: 1,130 s for a 77 Mpx 4-color SBS tile
# (data/lab_defaults.yaml sweep_tiles.kernel_timeout_s is the floor).
BASELINE_S_PER_MPX = 15.0
BASELINE_TIMEOUT_MARGIN = 3.0


def tile_work_mpx(analysis_dir, plate, well, tile, phase: str = "sbs") -> float | None:
    """Megapixels of preprocessed image data for one tile (all cycles x channels x planes),
    from the HCS zarr array metadata; None when the store is not there."""
    import json as _json

    row, col = re.match(r"([A-Za-z]+)(\d+)", str(well)).groups()
    root = Path(analysis_dir) / "brieflow_output" / "preprocess" / phase
    total = 0
    for meta in root.glob(f"image_{plate}.zarr/{row}/{col}/{tile}/*/0/zarr.json"):
        try:
            shape = _json.loads(meta.read_text()).get("shape") or []
        except (OSError, ValueError):
            continue
        n = 1
        for dim in shape:
            n *= int(dim)
        total += n
    return total / 1e6 if total else None


def baseline_timeout(timeout_s: float, mpx: float | None) -> float:
    """Baseline limit for a tile: the configured floor, or margin x the expected baseline
    time for this tile's image data, whichever is larger."""
    if not mpx:
        return float(timeout_s)
    return max(float(timeout_s), BASELINE_TIMEOUT_MARGIN * BASELINE_S_PER_MPX * mpx)


class _SharedRefine:
    """Refine points fixed once from the first batch of tiles' coarse rows, then probed on
    every tile, so refine rows are comparable across tiles (each tile used to refine around
    its own best value, leaving one-tile refine points to compete with the coarse grid).

    Every tile calls `arrive` once with its coarse rows ([] on failure). The plan is made
    from all rows so far once the first batch has arrived and some row has a rate;
    `plan_fn` returns None while no row is usable. First-batch tiles wait for the plan
    (they run concurrently, so the batch completes); a later tile never waits."""

    def __init__(self, n_first: int, plan_fn):
        self._cv = threading.Condition()
        self._rows: list = []
        self._first_left = max(1, int(n_first))
        self._plan = None
        self._plan_fn = plan_fn

    def arrive(self, rows: list, first: bool) -> None:
        with self._cv:
            self._rows.extend(rows or [])
            if first:
                self._first_left -= 1
            if self._plan is None and self._first_left <= 0:
                try:
                    self._plan = self._plan_fn(list(self._rows))
                except Exception:
                    self._plan = []
            self._cv.notify_all()

    def plan(self, first: bool) -> list:
        with self._cv:
            if first:
                self._cv.wait_for(lambda: self._plan is not None or self._first_left <= 0)
            return self._plan or []


def _run_tiles(tile_fn, tiles: list, max_parallel: int) -> list:
    """Run `tile_fn(plate, well, tile)` over `tiles` on up to `max_parallel`
    threads (each tile owns its own kernel/port/logs) and return the results in
    tile order. A tile whose fn raises yields None."""
    from concurrent.futures import ThreadPoolExecutor

    def _safe(t):
        try:
            return tile_fn(*t)
        except Exception:
            return None

    workers = max(1, int(max_parallel or 1))
    if workers == 1:
        return [_safe(t) for t in tiles]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_safe, tiles))


async def adaptive_marimo_sweep_multitile(
    nb_path: str | Path,
    param_name: str,
    spec: dict,
    tiles: Iterable[tuple],
    work_dir: str | Path,
    conda_env: str,
    timeout_s: float = 900.0,
    max_parallel_kernels: int = DEFAULT_MAX_PARALLEL_KERNELS,
    **kwargs,
):
    """Coarse-to-fine reactive sweep over ``spec`` across a bounded tile set.

    Drop-in for `adaptive_sweep_headless` (same call signature + return shape)
    but DAG-reactive: instead of a fresh ``python notebook.py`` subprocess per
    (value, tile) — which re-runs alignment + CellPose segmentation on every
    trial — each tile is segmented ONCE in a live marimo kernel and the swept
    marker is mutated in place, so only the cells downstream of the marker
    recompute. This is the whole point: segmentation cost is paid once per
    tile, not once per (value, tile).

    Per tile ``(plate, well, tile)``:
      1. Copy the notebook into ``work_dir`` and bake TEST_PLATE/TEST_WELL/
         TEST_TILE into the COPY *before* the kernel starts — the tile is
         upstream of segmentation, so it must be fixed pre-baseline, and editing
         the ``.py`` under a running kernel is a hard stop (CLAUDE.md).
      2. `start_marimo_kernel(copy, conda_env, watch=False)` — brings the server
         up. The kernel boots STALE (no cell runs). ``watch=False`` so the
         `ctx.edit_cell` mutations below don't re-fire the whole DAG.
      3. `_kernel_baseline` — injected INTO the kernel via the marimo-pair
         bridge: install the print tees, run every cell once, settle. This is
         the one segmentation paid per tile.
      4. `_kernel_sweep` over the COARSE band (again in-kernel, where
         `get_context` attaches), pick this tile's coarse winner, `refine_points`
         a FINE band around it, then `_kernel_sweep` the fine band on the SAME
         live kernel (segmentation still cached).
      5. `stop_marimo_kernel` in a ``finally`` that always runs.

    Everything context-bound runs in-kernel through `_execute_in_kernel`
    because `marimo._code_mode.get_context()` only attaches from inside the
    kernel — calling it from this (runner) process attaches to nothing and
    no cell ever executes.

    A tile whose kernel fails to come up is skipped (its failure isolated); the
    run still aggregates the survivors. If tiles were supplied but NO kernel
    came up, the function raises — so the resolver's safety net can fall back to
    `adaptive_sweep_headless` (the headless subprocess path) when the live-kernel
    route is unavailable in the operator's environment.

    Args:
        nb_path: phase notebook (.py) used as the per-tile copy template; the
            original is never modified.
        param_name: the operator-parameters variable to sweep (marker-block var).
        spec: coarse-to-fine band, matching `adaptive_sweep_headless`:
            ``coarse`` (list, required), ``refine_radius``, ``refine_step``,
            ``min``/``max`` (hard clamps for the refine pass; default coarse range).
        tiles: iterable of (plate, well, tile) tuples.
        work_dir: destination for per-tile notebook copies + sweep/print logs
            (must be inside the analysis dir, never /tmp per CLAUDE.md).
        conda_env: conda env to activate for the marimo kernel subprocess.
        timeout_s: per-sweep DAG-settle timeout handed to `run_marimo_sweep`.
        max_parallel_kernels: tiles run concurrently on up to this many live
            kernels (each tile owns its own copy/port/logs; only bring-up is
            serialized). 1 = sequential.
        **kwargs: headless-only knobs (omp_threads / max_parallel / export_html)
            are accepted and ignored so this stays call-compatible.

    Returns:
        pd.DataFrame with the SAME columns `sweep_headless` returns —
        ``value, mean_single_rate, std_single_rate, n_tiles, qc_pass_frac`` —
        one row per swept value aggregated across tiles, so `pick_sweep_winner`
        (and the resolver's winner coercion) consume it unchanged.
    """
    import shutil as _shutil

    try:
        import pandas as pd
    except ImportError as e:
        raise RuntimeError("adaptive_marimo_sweep_multitile requires pandas") from e

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    nb_path = Path(nb_path).resolve()

    coarse = list(spec.get("coarse") or [])
    radius = spec.get("refine_radius")
    step = spec.get("refine_step")
    lo = spec.get("min", min(coarse)) if coarse else None
    hi = spec.get("max", max(coarse)) if coarse else None

    _empty_cols = ["value", "mean_single_rate", "std_single_rate", "n_tiles", "qc_pass_frac"]
    tiles_list = list(tiles)
    if not tiles_list or not coarse:
        return pd.DataFrame(columns=_empty_cols)

    failures: list = []
    n_first = min(max(1, int(max_parallel_kernels or 1)), len(tiles_list))

    def _plan(rows):
        # Cross-tile coarse winner (mean over the tiles so far), then one refine band.
        if radius is None or step is None:
            return []
        coarse_df = pd.DataFrame(rows)
        if coarse_df.empty or coarse_df["single_rate"].dropna().empty:
            return None
        means = coarse_df.groupby("value", sort=False)["single_rate"].mean().reset_index()
        winner = pick_sweep_winner(means.rename(columns={"single_rate": "mean_single_rate"}))
        if hasattr(winner, "item"):
            winner = winner.item()
        return refine_points(winner, radius, step, lo, hi, exclude=coarse)

    shared = _SharedRefine(n_first, _plan)

    def _one_tile(index, plate, well, tile):
        slug = f"{param_name}_p{plate}_w{well}_t{tile}"
        nb_copy = work_dir / f"{slug}.py"
        prints_log = work_dir / f"{slug}.prints.log"
        out_log_coarse = work_dir / f"{slug}.coarse.tsv"
        out_log_fine = work_dir / f"{slug}.fine.tsv"
        records: list[dict] = []
        first = index < n_first
        arrived = False

        def arrive(rows):
            nonlocal arrived
            if not arrived:
                arrived = True
                shared.arrive(rows, first)

        def collect(df_t):
            return [
                {
                    "value": row.get("threshold"),
                    "single_rate": row.get("single_rate"),
                    "qc_pass": bool(row.get("qc_pass")),
                }
                for row in df_t.to_dict("records")
            ]

        try:
            # Step 1 — copy + bake the tile coords into the COPY (pre-kernel; the
            # tile is upstream of segmentation and the .py must not be edited while
            # a kernel runs on it).
            try:
                _shutil.copy(nb_path, nb_copy)
                patch_marker_block_value(nb_copy, "TEST_PLATE", repr(plate))
                patch_marker_block_value(nb_copy, "TEST_WELL", repr(well))
                patch_marker_block_value(nb_copy, "TEST_TILE", repr(int(tile)))
            except Exception as e:
                failures.append((slug, "patch", str(e)))
                return None  # patch failure → skip this tile (isolated)
            # Step 2 — bring the kernel up. cwd = the ANALYSIS dir (the original
            # notebook's parent): the notebook resolves config/ and brieflow_output/
            # relative to cwd. A bring-up failure is isolated.
            try:
                pid, port, _sid = _start_tile_kernel(nb_copy, conda_env, nb_path.parent)
            except Exception as e:
                failures.append((slug, "timeout" if _is_timeout(e) else "kernel", str(e)))
                return None
            try:
                # Step 3 — in-kernel baseline: the ONE segmentation for this tile; its
                # limit scales with the tile's image data.
                mpx = tile_work_mpx(nb_path.parent, plate, well, tile)
                _kernel_baseline(port, prints_log, baseline_timeout(timeout_s, mpx))
                # Step 4 — COARSE band, run_marimo_sweep injected into the kernel.
                df_coarse = _kernel_sweep(
                    port, param_name, coarse, out_log_coarse, prints_log, timeout_s
                )
                records += collect(df_coarse)
                arrive(records)
                # Fine band: the same points on every tile (shared plan), same kernel.
                refine = shared.plan(first)
                if refine:
                    df_fine = _kernel_sweep(
                        port, param_name, refine, out_log_fine, prints_log, timeout_s
                    )
                    records += collect(df_fine)
            except Exception as e:
                # tile-level failure isolated — survivors still aggregate; the
                # traceback lands in <slug>.error.log
                failures.append((slug, "timeout" if _is_timeout(e) else "error", str(e)[-300:]))
                _log_tile_error(work_dir, slug, "sweep")
            finally:
                # Step 5 — always stop the kernel (idempotent).
                stop_marimo_kernel(pid)
            return records
        finally:
            arrive([])

    indexed = [(i, *t) for i, t in enumerate(tiles_list)]
    results = _run_tiles(_one_tile, indexed, max_parallel_kernels)
    started = sum(r is not None for r in results)
    records = [row for r in results if r for row in r]

    # Fall back to the headless subprocess path if the reactive route produced
    # nothing usable — no kernel came up, every kernel's sweep raised, or no
    # finite mapping rate was captured (e.g. the ctx_factory can't attach to an
    # out-of-process kernel, or the tee missed the prints). Raising is the signal
    # the resolver catches; returning empty here would silently proceed with the
    # default value instead. Partial success (>=1 finite rate) is kept.
    df_jobs = pd.DataFrame(records)
    if df_jobs.empty or df_jobs["single_rate"].dropna().empty:
        raise _no_rate_error(
            "adaptive_marimo_sweep_multitile", len(tiles_list), started, failures, timeout_s
        )

    # Aggregate across tiles exactly as sweep_headless does (threshold→value).
    def _agg(g):
        rates = g["single_rate"].dropna()
        return pd.Series({
            "mean_single_rate": rates.mean() if len(rates) > 0 else None,
            "std_single_rate": rates.std(ddof=0) if len(rates) > 1 else 0.0,
            "n_tiles": len(g),
            "qc_pass_frac": g["qc_pass"].mean(),
        })

    summary = df_jobs.groupby("value", sort=False).apply(_agg, include_groups=False).reset_index()
    summary.attrs["evidence"] = _finite_counts(df_jobs, ["value"])
    return summary


# ---------------------------------------------------------------------------
# Joint reactive sweep — two marker params swept TOGETHER on one live kernel
# per tile (outer -> inner in DAG order), so interacting params (PEAK_WIDTH
# rescales the peak score THRESHOLD_READS gates on) are optimized as a pair.
# ---------------------------------------------------------------------------


def _geometric_refine(center, lo, hi, exclude=()):
    """Scale-free local refine around `center`: x·2^(±1/4), x·2^(±1/2), rounded to
    int, clamped to [lo, hi], deduplicated against `exclude`."""
    pts = []
    for f in (2 ** -0.5, 2 ** -0.25, 2 ** 0.25, 2 ** 0.5):
        v = int(round(center * f))
        if lo is not None:
            v = max(v, int(lo))
        if hi is not None:
            v = min(v, int(hi))
        if v not in pts and v not in exclude and v != center:
            pts.append(v)
    return pts


def _pick_joint_winner(rows, min_tiles: int = 1):
    """Argmax mean single_rate over (outer, inner) pairs; ties -> smaller inner,
    then smaller outer. `rows`: iterable of dicts with outer/inner/single_rate
    (+ `tile`). ``min_tiles``: only pairs measured on at least this many tiles
    compete — the cross-tile pick must not be won by a per-tile refine point
    that a single (lucky) tile probed, over the coarse grid every tile ran."""
    import pandas as pd

    df = pd.DataFrame(rows)
    df = df[df["single_rate"].notna()]
    if df.empty:
        return None, None
    if "tile" not in df.columns:
        df = df.assign(tile=0)
    agg = df.groupby(["outer", "inner"]).agg(single_rate=("single_rate", "mean"),
                                            n_tiles=("tile", "nunique")).reset_index()
    eligible = agg[agg["n_tiles"] >= min_tiles]
    if eligible.empty:
        eligible = agg[agg["n_tiles"] == agg["n_tiles"].max()]
    best = eligible["single_rate"].max()
    cand = eligible[eligible["single_rate"] == best].sort_values(["inner", "outer"])
    return cand.iloc[0]["outer"], cand.iloc[0]["inner"]


async def joint_marimo_sweep_multitile(
    nb_path: str | Path,
    specs: dict,
    tiles: Iterable[tuple],
    work_dir: str | Path,
    conda_env: str,
    timeout_s: float = 900.0,
    max_parallel_kernels: int = DEFAULT_MAX_PARALLEL_KERNELS,
    **kwargs,
):
    """Sweep two marker params jointly, per tile, on ONE live kernel each.

    `specs` is an ordered mapping ``{outer_param: spec, inner_param: spec}`` in
    DAG order (outer upstream of inner; e.g. PEAK_WIDTH then THRESHOLD_READS).
    Each spec is a lab sweep spec (``coarse`` list required; ``min``/``max``
    clamps and ``refine_step`` for the outer param optional).

    Per tile: bake the tile into a notebook copy, bring the kernel up, run the
    baseline ONCE (segmentation), then for each outer value set it and sweep the
    whole inner coarse band — only cells downstream of the mutated marker re-run.
    Then a local refine around this tile's best pair on the same kernel: inner
    geometric (x·2^(±1/4), x·2^(±1/2)) at the best outer, and outer ±refine_step
    at the best inner. Kernel is always stopped (finally). Tiles run concurrently
    on up to ``max_parallel_kernels`` kernels (1 = sequential).

    Returns ``(winners, summary)``: ``winners = {outer_param: v, inner_param: v}``
    = argmax of the cross-tile mean single_rate over pairs, and a DataFrame
    ``(outer, inner, mean_single_rate, std_single_rate, n_tiles, qc_pass_frac)``.
    Raises RuntimeError when no finite rate was produced (resolver falls back to
    the sequential per-param sweeps).
    """
    import shutil as _shutil

    import pandas as pd

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    nb_path = Path(nb_path).resolve()
    (outer_name, outer_spec), (inner_name, inner_spec) = list(specs.items())[:2]
    outer_vals = list(outer_spec.get("coarse") or [])
    inner_vals = list(inner_spec.get("coarse") or [])
    tiles_list = list(tiles)
    if not tiles_list or not outer_vals or not inner_vals:
        return {}, pd.DataFrame(columns=["outer", "inner", "mean_single_rate", "std_single_rate", "n_tiles", "qc_pass_frac"])
    o_lo, o_hi = outer_spec.get("min"), outer_spec.get("max")
    i_lo, i_hi = inner_spec.get("min"), inner_spec.get("max")
    o_step = outer_spec.get("refine_step")

    failures: list = []
    n_first = min(max(1, int(max_parallel_kernels or 1)), len(tiles_list))

    def _plan(rows):
        # One refine plan from the cross-tile coarse winner (pairs every tile so far ran).
        if not rows or pd.DataFrame(rows)["single_rate"].dropna().empty:
            return None
        n = pd.DataFrame(rows)["tile"].nunique()
        best_o, best_i = _pick_joint_winner(rows, min_tiles=(n + 1) // 2)
        if best_o is None:
            return None
        best_o, best_i = int(best_o), int(best_i)
        bands = []
        fine_i = _geometric_refine(best_i, i_lo, i_hi, exclude=inner_vals)
        if fine_i:
            bands.append((best_o, fine_i))
        if o_step:
            for ov in (best_o - o_step, best_o + o_step):
                if ov in outer_vals or (o_lo is not None and ov < o_lo) or (
                    o_hi is not None and ov > o_hi
                ):
                    continue
                bands.append((ov, [best_i]))
        return bands

    shared = _SharedRefine(n_first, _plan)

    def _one_tile(index, plate, well, tile):
        slug = f"joint_p{plate}_w{well}_t{tile}"
        nb_copy = work_dir / f"{slug}.py"
        prints_log = work_dir / f"{slug}.prints.log"
        first = index < n_first
        tile_rows: list[dict] = []
        arrived = False

        def arrive():
            nonlocal arrived
            if not arrived:
                arrived = True
                shared.arrive(list(tile_rows), first)

        try:
            try:
                _shutil.copy(nb_path, nb_copy)
                patch_marker_block_value(nb_copy, "TEST_PLATE", repr(plate))
                patch_marker_block_value(nb_copy, "TEST_WELL", repr(well))
                patch_marker_block_value(nb_copy, "TEST_TILE", repr(int(tile)))
            except Exception as e:
                failures.append((slug, "patch", str(e)))
                return None
            try:
                pid, port, _sid = _start_tile_kernel(nb_copy, conda_env, nb_path.parent)
            except Exception as e:
                failures.append((slug, "timeout" if _is_timeout(e) else "kernel", str(e)))
                return None

            def band(outer_v, inner_list, label):
                # set the outer marker (runs its downstream once), then sweep the inner
                # band; a band failure is isolated (logged) so other bands still count
                try:
                    _kernel_sweep(port, outer_name, [outer_v],
                                  work_dir / f"{slug}.{label}.outer.tsv", prints_log, timeout_s)
                    df = _kernel_sweep(port, inner_name, inner_list,
                                       work_dir / f"{slug}.{label}.tsv", prints_log, timeout_s)
                except Exception as e:
                    failures.append((slug, "timeout" if _is_timeout(e) else "error", str(e)))
                    _log_tile_error(work_dir, slug, f"band {label}")
                    return
                for r in df.to_dict("records"):
                    tile_rows.append({"tile": slug, "outer": outer_v, "inner": r.get("threshold"),
                                      "single_rate": r.get("single_rate"),
                                      "qc_pass": bool(r.get("qc_pass"))})

            try:
                mpx = tile_work_mpx(nb_path.parent, plate, well, tile)
                _kernel_baseline(port, prints_log, baseline_timeout(timeout_s, mpx))
                for ov in outer_vals:
                    band(ov, inner_vals, f"o{ov}")
                arrive()
                # The same refine bands on every tile (shared plan), same kernel.
                for ov, inner_list in shared.plan(first):
                    band(ov, inner_list, f"o{ov}.refine")
            except Exception as e:
                failures.append((slug, "timeout" if _is_timeout(e) else "error", str(e)[-300:]))
                _log_tile_error(work_dir, slug, "tile")  # isolated; survivors aggregate
            finally:
                stop_marimo_kernel(pid)
            return tile_rows
        finally:
            arrive()

    indexed = [(i, *t) for i, t in enumerate(tiles_list)]
    results = _run_tiles(_one_tile, indexed, max_parallel_kernels)
    started = sum(r is not None for r in results)
    rows = [row for r in results if r for row in r]

    df = pd.DataFrame(rows)
    if df.empty or df["single_rate"].dropna().empty:
        raise _no_rate_error(
            "joint_marimo_sweep_multitile", len(tiles_list), started, failures, timeout_s
        )
    pd.DataFrame(rows).to_csv(work_dir / "joint_rows.tsv", sep="\t", index=False)

    def _agg(g):
        rates = g["single_rate"].dropna()
        return pd.Series({
            "mean_single_rate": rates.mean() if len(rates) > 0 else None,
            "std_single_rate": rates.std(ddof=0) if len(rates) > 1 else 0.0,
            "n_tiles": g["tile"].nunique(),
            "qc_pass_frac": g["qc_pass"].mean(),
        })

    summary = df.groupby(["outer", "inner"], sort=False).apply(_agg, include_groups=False).reset_index()
    summary.attrs["evidence"] = _finite_counts(df, ["outer", "inner"])
    # cross-tile pick: a pair must have been measured on a majority of the
    # surviving tiles (the coarse grid always qualifies; per-tile refine points
    # only when they recur)
    n_surviving = int(df["tile"].nunique())
    best_o, best_i = _pick_joint_winner(rows, min_tiles=(n_surviving + 1) // 2)
    winners = {outer_name: int(best_o), inner_name: int(best_i)}
    summary.to_csv(work_dir / "joint_summary.tsv", sep="\t", index=False)
    return winners, summary

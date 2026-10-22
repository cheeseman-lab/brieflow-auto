#!/usr/bin/env python3
"""brieflow_hooks.py — RunLifecycleHooks for data movement around a screen run.

Two impls:
  NoopHooks — slurm/NFS default; no data movement needed (everything's on
              shared storage). All hook methods are no-ops.
  GcsHooks  — cloud VM mode. Pulls inputs from GCS pre-phase-1, pushes
              outputs to GCS post-success, archives diag bundle on failure,
              cleans up local disk on teardown.

Selected by brieflow_run.py's --lifecycle-hooks {noop,gcs} flag and invoked from
run_screen in brieflow_runner.py, at four lifecycle points:
  on_pre_phase_1         — before any phase starts
  on_post_terminal_phase — after the last phase succeeds
  on_failure             — when the run escalates / aborts
  on_teardown            — atexit cleanup

brieflow_runloop.py carries the same flag for a direct single-phase launch.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Protocol


def _log(msg: str) -> None:
    """Stderr logger so hook output doesn't pollute JSON channels."""
    print(f"[brieflow_hooks] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class RunLifecycleHooks(Protocol):
    """Data-movement hooks called by the runloop at phase boundaries.

    All methods are best-effort: implementations log + continue on transient
    errors rather than aborting the run. Pre/post-phase failures DO propagate
    (the runloop can't proceed without inputs / can't claim success without
    pushing outputs); failure/teardown hooks swallow exceptions.
    """

    name: str

    def on_pre_phase_1(self, analysis_dir: Path, screen_id: str) -> None: ...

    def on_post_terminal_phase(self, analysis_dir: Path, screen_id: str) -> None: ...

    def on_failure(
        self, analysis_dir: Path, screen_id: str, diag_path: Path | None
    ) -> None: ...

    def on_teardown(self, analysis_dir: Path, screen_id: str) -> None: ...


# ---------------------------------------------------------------------------
# Noop (slurm/NFS default — everything already on shared storage)
# ---------------------------------------------------------------------------


class NoopHooks:
    name = "noop"

    def on_pre_phase_1(self, analysis_dir: Path, screen_id: str) -> None:
        return None

    def on_post_terminal_phase(self, analysis_dir: Path, screen_id: str) -> None:
        return None

    def on_failure(
        self, analysis_dir: Path, screen_id: str, diag_path: Path | None
    ) -> None:
        return None

    def on_teardown(self, analysis_dir: Path, screen_id: str) -> None:
        return None


# ---------------------------------------------------------------------------
# Gcs (cloud VM mode — rsync to/from gs:// buckets)
# ---------------------------------------------------------------------------


class GcsHooks:
    """Pull inputs from gs://${INPUT_BUCKET}/<screen-id>/ pre-phase-1,
    push outputs to gs://${OUTPUT_BUCKET}/<screen-id>/ post-success.

    Two buckets, both required, no defaults: BRIEFLOW_BUCKET_INPUT and
    BRIEFLOW_BUCKET_OUTPUT. The plugin is lab-agnostic — operators set
    their own bucket names per project.

    Uses gsutil -m rsync (parallel transfer). On failure, archives the
    runloop's diag bundle to gs://${OUTPUT_BUCKET}/<screen-id>/failures/.

    Raises RuntimeError if required bucket env vars are not set.
    """

    name = "gcs"

    def __init__(self) -> None:
        self.input_bucket = os.environ.get("BRIEFLOW_BUCKET_INPUT", "")
        self.output_bucket = os.environ.get("BRIEFLOW_BUCKET_OUTPUT", "")
        if not self.input_bucket or not self.output_bucket:
            raise RuntimeError(
                "GcsHooks requires BRIEFLOW_BUCKET_INPUT and "
                "BRIEFLOW_BUCKET_OUTPUT env vars to be set. See "
                "plugins/brieflow-auto/skills/brieflow-setup/references/gcp_bootstrap.md"
            )

    def _gsutil_rsync(
        self,
        src: str,
        dst: str,
        recursive: bool = True,
        check: bool = True,
    ) -> int:
        """Run `gsutil -m rsync [-r] <src> <dst>`. Returns exit code.

        If check=True, raises on non-zero. If check=False, just returns the
        code (used by failure/teardown hooks that swallow errors).
        """
        cmd = ["gsutil", "-m", "rsync"]
        if recursive:
            cmd.append("-r")
        cmd.extend([src, dst])
        _log(f"  {' '.join(cmd)}")
        try:
            proc = subprocess.run(
                cmd, check=check, capture_output=True, text=True, timeout=3600
            )
            if proc.returncode != 0:
                _log(f"  (exit {proc.returncode}) {proc.stderr.strip()[:300]}")
            return proc.returncode
        except FileNotFoundError:
            _log("ERROR: gsutil not installed. GcsHooks requires Google Cloud SDK.")
            if check:
                raise
            return 127

    def on_pre_phase_1(self, analysis_dir: Path, screen_id: str) -> None:
        """Pull raw inputs from gs://input/<screen-id>/ → <analysis_dir>/raw/."""
        src = f"gs://{self.input_bucket}/{screen_id}/"
        dst = str(analysis_dir / "raw") + "/"
        Path(dst).mkdir(parents=True, exist_ok=True)
        _log(f"on_pre_phase_1: pulling inputs {src} → {dst}")
        self._gsutil_rsync(src, dst, recursive=True, check=True)

    def on_post_terminal_phase(self, analysis_dir: Path, screen_id: str) -> None:
        """Push brieflow_output/ to gs://output/<screen-id>/brieflow_output/."""
        local = analysis_dir / "brieflow_output"
        if not local.exists():
            _log(f"on_post_terminal_phase: no brieflow_output/ at {local}; skip")
            return
        src = str(local) + "/"
        dst = f"gs://{self.output_bucket}/{screen_id}/brieflow_output/"
        _log(f"on_post_terminal_phase: pushing outputs {src} → {dst}")
        self._gsutil_rsync(src, dst, recursive=True, check=True)

    def on_failure(
        self, analysis_dir: Path, screen_id: str, diag_path: Path | None
    ) -> None:
        """Archive diag bundle (if any) + partial outputs to GCS failures dir.
        Swallows ALL errors — failure hook must not raise (called from the
        runloop's exception path, can't itself cause another failure)."""
        from datetime import datetime

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        failures_prefix = f"gs://{self.output_bucket}/{screen_id}/failures/{ts}/"
        _log(f"on_failure: archiving to {failures_prefix}")

        def _safe_rsync(src: str, dst: str, recursive: bool) -> None:
            try:
                self._gsutil_rsync(src, dst, recursive=recursive, check=False)
            except Exception as e:  # noqa: BLE001 (failure hook must not raise)
                _log(f"  WARN: rsync {src} → {dst} failed: {e}")

        # Diag bundle if recover.py / runloop wrote one.
        if diag_path and diag_path.exists():
            _safe_rsync(str(diag_path), failures_prefix, recursive=False)

        # Partial brieflow_output (small set of failure-context files only;
        # full output set may be huge and a partial-failure run shouldn't
        # waste bandwidth on it).
        for sub in (".brieflow_runloop", "logs"):
            p = analysis_dir / sub
            if p.exists():
                _safe_rsync(str(p) + "/", failures_prefix + sub + "/", recursive=True)

    def on_teardown(self, analysis_dir: Path, screen_id: str) -> None:
        """No-op — VM teardown via cloud/teardown_vm.sh handles disk reclaim
        more thoroughly than a process-scoped cleanup could. Leave files on
        disk for SSH-in diagnostic harvesting before operator runs teardown.
        """
        return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_hooks(name: str) -> RunLifecycleHooks:
    """Return RunLifecycleHooks for the given name."""
    if name == "noop":
        return NoopHooks()
    if name == "gcs":
        return GcsHooks()
    raise ValueError(f"unknown lifecycle hooks: {name!r} (choose: noop, gcs)")

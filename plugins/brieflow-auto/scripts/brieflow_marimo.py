#!/usr/bin/env python3
"""brieflow_marimo.py — marimo kernel lifecycle (CONTRACT-FROZEN).

Start/stop a headless marimo kernel, the WS-session holder it needs, and the
library-print tee setup. Signatures + side effects must not change — the
brieflow-notebook scratchpad imports these from brieflow_wizard and drives the
live-kernel sweep. Pure stdlib; no dependency on other plugin modules. The
sweep orchestration (run_marimo_sweep) lives in brieflow_sweep.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Marimo kernel session lifecycle (Pass-3 sweep helpers)
# ---------------------------------------------------------------------------
#
# Pass-3 of the speed bake (2026-05-20) used `marimo edit --no-token --headless`
# to keep a reactive DAG alive across a parameter sweep, then drove it via
# `marimo._code_mode.get_context()`. `--headless` registers the server in the
# local registry but doesn't instantiate a session until a client connects;
# `execute-code.sh` reports "No active sessions" without one. The helpers below
# fold the agent-written workarounds (.brieflow/ws_session_holder.py,
# cm_sweep_one.py, cm_setup_lib_tee.py) into first-class plugin entry points so
# future agents don't have to re-invent them.


def _free_port(default_start: int = 2718) -> int:
    """Return a free localhost TCP port at-or-above `default_start`."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def start_marimo_kernel(
    notebook_path: str | Path,
    port: int | None = None,
    conda_env: str | None = None,
    ready_timeout_s: float = 120.0,
    watch: bool = True,
    cwd: str | Path | None = None,
) -> tuple[int, int, str]:
    """Launch `marimo edit --no-token --headless` on `notebook_path`, then
    hold a WebSocket session open against it so an active session exists for
    `execute-code.sh` / `ctx` to attach to.

    `cwd`: working directory for the kernel; defaults to the notebook's own
    directory. Sweeps run notebook COPIES under a work_dir, but the phase
    notebook resolves `config/` and `brieflow_output/` relative to cwd, so a
    copy must be started in the analysis dir or it dies with "Cannot save
    file into a non-existent directory: 'config'".

    Returns `(pid, port, session_id)`. `pid` is the marimo server's PID; the
    session-holder subprocess is tracked internally and stopped by
    `stop_marimo_kernel`. Caller is responsible for invoking the cleanup
    partner when done.

    `watch`: pass `--watch` to marimo so file edits trigger reactive recompute.
    Default True (matches operator-interactive usage). Set False for sweep
    orchestration — `--watch` re-triggers the scratchpad cell when
    `run_marimo_sweep` does its `ctx.edit_cell` mutations, causing the sweep
    loop to re-fire (finding #8b).

    Raises if the server fails to come up within `ready_timeout_s`.
    """
    import time as _time
    import uuid

    nb = Path(notebook_path).resolve()
    port = port or _free_port()
    session_id = f"brieflow-{uuid.uuid4().hex[:8]}"

    watch_flag = "--watch" if watch else ""
    if conda_env:
        cmd = [
            "bash",
            "-c",
            f'eval "$(conda shell.bash hook)" && conda activate {conda_env} && '
            f'exec python -m marimo edit --no-token --no-skew-protection --headless {watch_flag} --port {port} "{nb}"',
        ]
    else:
        cmd = [
            sys.executable,
            "-m",
            "marimo",
            "edit",
            "--no-token",
            "--no-skew-protection",
            "--headless",
            *(["--watch"] if watch else []),
            "--port",
            str(port),
            str(nb),
        ]
    # Server stderr goes to a log so a kernel that dies mid-sweep is diagnosable.
    server_log = nb.parent / ".brieflow" / f"_marimo_server.{port}.log"
    server_log.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=open(server_log, "wb"),
        cwd=str(cwd) if cwd is not None else nb.parent,
    )

    # Poll for the server port to accept connections.
    import socket as _sock

    deadline = _time.time() + ready_timeout_s
    while _time.time() < deadline:
        s = _sock.socket()
        try:
            s.settimeout(0.5)
            s.connect(("127.0.0.1", port))
            s.close()
            break
        except OSError:
            _sock.socket()
        finally:
            try:
                s.close()
            except Exception:
                pass
        _time.sleep(0.25)
    else:
        proc.terminate()
        raise RuntimeError(
            f"marimo edit did not come up on port {port} within {ready_timeout_s}s"
        )

    # Spawn the WS session-holder. Source lives in a sibling file
    # `_ws_holder.py` (written at module import time) so we never have to
    # escape multi-line Python through bash -c — that was finding #5.
    holder_py = _ensure_ws_holder_script()
    if conda_env:
        holder_cmd = [
            "bash",
            "-c",
            f'eval "$(conda shell.bash hook)" && conda activate {conda_env} && '
            f'exec python "{holder_py}" {port} {session_id}',
        ]
    else:
        holder_cmd = [sys.executable, str(holder_py), str(port), session_id]
    # Capture stderr to a tmpfile so holder crashes surface in stop_marimo_kernel
    # / debugging instead of being silently dropped.
    holder_log = nb.parent / ".brieflow" / f"_ws_holder.{port}.log"
    holder_log.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        holder_cmd,
        stdout=subprocess.DEVNULL,
        stderr=open(holder_log, "wb"),
    )
    _MARIMO_HOLDERS[proc.pid] = holder

    # Wait until the holder's session is actually registered on the server
    # (poll /api/sessions) — a fixed grace period raced the first bridge call
    # when several kernels boot at once (conda activation alone takes seconds)
    # and surfaced as "No active sessions on the server". If the holder dies
    # meanwhile, surface that rather than handing back a broken triple.
    deadline = _time.time() + ready_timeout_s
    while _time.time() < deadline:
        if holder.poll() is not None:
            err = ""
            try:
                err = holder_log.read_text()[:500]
            except Exception:
                pass
            proc.terminate()
            raise RuntimeError(
                f"WS session holder died immediately (exit {holder.returncode}). "
                f"stderr: {err}"
            )
        if _session_registered(port):
            return proc.pid, port, session_id
        _time.sleep(0.5)
    proc.terminate()
    holder.terminate()
    raise RuntimeError(
        f"WS session holder did not register a session on port {port} within {ready_timeout_s}s"
    )


def _session_registered(port: int) -> bool:
    """True once the marimo server on `port` lists at least one session."""
    import json as _json
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/sessions", timeout=2) as r:
            return bool(_json.loads(r.read().decode() or "{}"))
    except Exception:
        return False


_WS_HOLDER_SRC = """\
import asyncio
import sys
import websockets


async def _hold(port, sid):
    uri = f"ws://localhost:{port}/ws?session_id={sid}"
    async with websockets.connect(
        uri, max_size=None, ping_interval=20
    ) as ws:
        try:
            while True:
                try:
                    await asyncio.wait_for(ws.recv(), timeout=60)
                except asyncio.TimeoutError:
                    continue
        except websockets.ConnectionClosed:
            return


asyncio.run(_hold(int(sys.argv[1]), sys.argv[2]))
"""


def _ensure_ws_holder_script() -> Path:
    """Write the WS-holder source to a sibling file next to this module and
    return its path. Idempotent — only writes when the file is missing or its
    content drifts from the embedded source.
    """
    p = Path(__file__).resolve().parent / "_ws_holder.py"
    if not p.exists() or p.read_text() != _WS_HOLDER_SRC:
        p.write_text(_WS_HOLDER_SRC)
    return p


# Track session-holder subprocesses by server PID so stop_marimo_kernel can
# clean them up without the caller juggling extra handles.
_MARIMO_HOLDERS: dict[int, "subprocess.Popen"] = {}


def stop_marimo_kernel(pid: int, timeout_s: float = 5.0) -> None:
    """Terminate the marimo server (by PID) and the WS session holder spawned
    alongside it. Idempotent — silently no-ops if the process is already gone."""
    holder = _MARIMO_HOLDERS.pop(pid, None)
    if holder is not None:
        try:
            holder.terminate()
            holder.wait(timeout=timeout_s)
        except Exception:
            try:
                holder.kill()
            except Exception:
                pass
    try:
        os.kill(pid, 15)
    except ProcessLookupError:
        return
    except PermissionError:
        return
    # Best-effort wait + kill if still alive.
    import time as _time

    deadline = _time.time() + timeout_s
    while _time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        _time.sleep(0.2)
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass


def _await_kernel_settle(
    ctx_factory, timeout_s: float = 600.0, poll_s: float = 2.0
) -> bool:
    """Poll the marimo context until no cell is `running`/`queued`/`stale`.
    `ctx_factory` is a zero-arg callable that returns a fresh context snapshot
    (typically `marimo._code_mode.get_context`). Returns True if settled,
    False on timeout."""
    import time as _time

    active = {"running", "queued", "stale"}
    deadline = _time.time() + timeout_s
    while _time.time() < deadline:
        ctx = ctx_factory()
        busy = []
        cells = getattr(ctx, "cells", None) or []
        iterable = cells.values() if hasattr(cells, "values") else cells
        for c in iterable:
            status = str(getattr(c, "status", "")).rsplit(".", 1)[-1]
            if status in active:
                busy.append(c)
        if not busy:
            return True
        _time.sleep(poll_s)
    return False


# Default brieflow library modules whose `print` should tee to disk during a
# sweep. Numba's `infer_global(print)` does an identity check inside
# `find_peaks`, so we MUST NOT patch `builtins.print` — only the per-module
# attribute. Callers can pass `extra_modules` to extend.
_DEFAULT_TEE_MODULES = (
    "lib.sbs.align_cycles",
    "lib.sbs.eval_mapping",
    "lib.sbs.call_reads",
    "lib.shared.segmentation_utils",
    "lib.phenotype.align_channels",
)


async def setup_kernel_tees(
    session,
    log_path: str | Path,
    extra_modules: Iterable[str] = (),
) -> dict:
    """Install per-module print tees in the marimo kernel so SBS/phenotype
    library prints (alignment QC, segmentation QC, mapping rates) are mirrored
    to `log_path` for the sweep parser to consume.

    Per-module attribute patch only — does NOT touch `builtins.print` because
    numba's `find_peaks` does `infer_global(print)` and breaks if the global
    binding identity changes. This matches the safe pattern from the Pass-3
    `cm_setup_lib_tee.py` workaround.

    `session` must expose `ctx_factory()`; the tee installs by running a
    one-shot cell in the kernel that imports the target modules and rebinds
    each module's `print` to a tee_print wrapper. Returns:

        {"ok": bool, "log_path": str, "modules": [...]}
    """
    ctx_factory = session["ctx_factory"]
    modules = list(_DEFAULT_TEE_MODULES) + list(extra_modules)
    log_path = str(Path(log_path).resolve())

    # Sentinel as the FIRST line of setup_code lets us scan ctx.cells for
    # any existing tee-setup cell on repeat calls and delete it before adding
    # a fresh one. marimo's create_cell signature has no `cell_id` parameter
    # — the prior anchor_id de-dup attempt never worked, leaving duplicate
    # cells that collided on identical name definitions (finding #8a).
    sentinel = "# __brieflow_tee_setup__"
    setup_code = f"""{sentinel}
import builtins, sys
{chr(10).join(f"import {m}" for m in modules)}
_log = open({log_path!r}, "a", buffering=1)
_real = builtins.print
def tee_print(*args, **kwargs):
    _real(*args, **kwargs)
    kw = {{k: v for k, v in kwargs.items() if k not in ("file", "flush")}}
    _real(*args, file=_log, **kw)
tee_print.__name__ = "print"
tee_print.__qualname__ = "print"
for _m in {modules!r}:
    sys.modules[_m].print = tee_print
builtins._brieflow_tee_log = _log
""".strip()

    try:
        new_cell_id: str | None = None
        async with ctx_factory() as ctx:
            # De-dup: delete any prior tee-setup cells (matched by sentinel).
            existing: list[str] = []
            try:
                cells_iter = ctx.cells.values() if hasattr(ctx.cells, "values") else ctx.cells
                for c in list(cells_iter):
                    code = getattr(c, "code", "") or ""
                    if code.lstrip().startswith(sentinel):
                        cid = getattr(c, "id", None) or getattr(c, "cell_id", None)
                        if cid:
                            existing.append(cid)
            except Exception:
                pass
            for cid in existing:
                try:
                    ctx.delete_cell(cid)
                except Exception:
                    pass

            add = getattr(ctx, "add_cell", None) or getattr(ctx, "create_cell", None)
            if add is not None:
                # marimo 0.25.0 create_cell returns the new cell_id.
                new_cell_id = add(setup_code)
                # Explicit run — create_cell adds the cell to the DAG but does
                # not always trigger execution synchronously. Without this,
                # downstream cells run with the original (un-tee'd) print bindings.
                if new_cell_id and hasattr(ctx, "run_cell"):
                    try:
                        ctx.run_cell(new_cell_id)
                    except Exception:
                        pass
            elif hasattr(ctx, "edit_cell"):
                ctx.edit_cell(sentinel, code=setup_code)
                ctx.run_cell(sentinel)

        # Wait for the tee setup cell to actually finish executing before we
        # return. Otherwise downstream callers race with the import chain in
        # the setup cell's body.
        _await_kernel_settle(ctx_factory, timeout_s=30.0, poll_s=0.5)
        return {
            "ok": True,
            "log_path": log_path,
            "modules": modules,
            "deleted_prior": len(existing),
            "cell_id": new_cell_id,
        }
    except Exception as e:
        return {
            "ok": False,
            "log_path": log_path,
            "modules": modules,
            "error": f"{type(e).__name__}: {e}",
        }


# ---------------------------------------------------------------------------
# Internal

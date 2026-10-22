"""Live-kernel marimo integration test — the marimo-pair reactive contract.

This is the test that makes the brieflow_marimo / brieflow_sweep extraction (and
the whole brieflow-notebook flow) trustworthy. It stands up a REAL
`marimo edit --headless` server and drives it through the production path
(`start_marimo_kernel` → cells run via `run_marimo_sweep` over the
`marimo-pair/execute-code.sh` bridge → `export_phase_html` → `stop_marimo_kernel`),
proving the reactive contract the sweep speed depends on:

  * the kernel stays live across many cell mutations;
  * mutating the marker cell reruns ONLY downstream cells — the upstream cell
    stays cached (the whole point of the reactive sweep);
  * `export_phase_html` flushes the in-kernel winner back to disk (`ctx.save()`)
    before exporting, and writes the HTML.

Needs a real marimo install + server + ports, so it is `importorskip`-guarded
and skips cleanly where marimo is absent (e.g. brieflow_test). Run it in the
canonical dev env: `make test` / `make marimo-test` (brieflow_auto_dev; marimo
pinned in requirements-dev.txt).

Notes learned from the live kernel (marimo 0.23.6, rechecked on 0.25.0):
  * `marimo edit --headless` boots every cell STALE — it does NOT auto-run.
    A baseline run is established explicitly by running the root cells.
  * The ctx API is `run_cell`/`edit_cell` (no run-all); `marimo._code_mode.
    get_context()` only attaches in-kernel, hence the execute-code.sh bridge.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("marimo")

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
EXECUTE_CODE = SCRIPTS / "marimo-pair" / "execute-code.sh"

# The bridge talks HTTP via curl + jq.
pytestmark = pytest.mark.skipif(
    shutil.which("curl") is None or shutil.which("jq") is None,
    reason="marimo-pair bridge needs curl + jq",
)

# Preamble for code injected into the kernel: make brieflow_wizard importable and
# allow asyncio.run() inside the kernel's already-running loop.
_KERNEL_PREAMBLE = (
    "import sys, asyncio\n"
    "import nest_asyncio; nest_asyncio.apply()\n"
    f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
    "import marimo._code_mode as cm\n"
)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bw():
    return _load("brieflow_wizard")


# A tiny reactive notebook: upstream (cached) -> marker (THRESHOLD) -> downstream.
# Each cell appends to a counter file when it runs, so we can prove which cells
# re-ran. Underscore names are cell-local in marimo, so the shared names
# (RUNS_DIR, UPSTREAM, THRESHOLD) are deliberately bare.
NOTEBOOK_SRC = '''\
import marimo

app = marimo.App()


@app.cell
def _():
    import os
    from pathlib import Path

    RUNS_DIR = Path(os.environ["BRIEFLOW_MARIMO_TEST_DIR"])
    return (RUNS_DIR,)


@app.cell
def _(RUNS_DIR):
    # UPSTREAM — does NOT depend on THRESHOLD, so must stay cached across the sweep.
    # `_f` is underscore-prefixed so marimo treats it as cell-local (a bare `f`
    # would collide with the downstream cell — MultipleDefinitionError).
    with open(RUNS_DIR / "upstream_runs.txt", "a") as _f:
        _f.write("u\\n")
    UPSTREAM = 100
    return (UPSTREAM,)


@app.cell
def _():
    # === OPERATOR PARAMETERS ===
    THRESHOLD = 1
    # === END OPERATOR PARAMETERS ===
    return (THRESHOLD,)


@app.cell
def _(RUNS_DIR, THRESHOLD, UPSTREAM):
    # DOWNSTREAM — depends on THRESHOLD, so must re-run on every marker mutation.
    with open(RUNS_DIR / "downstream_runs.txt", "a") as _f:
        _f.write(f"d {THRESHOLD}\\n")
    RESULT = UPSTREAM + THRESHOLD
    return (RESULT,)


if __name__ == "__main__":
    app.run()
'''


def _count_lines(p: Path) -> int:
    return len(p.read_text().splitlines()) if p.exists() else 0


def _wait_until(predicate, timeout_s=60.0, poll_s=0.5, what="condition"):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    raise AssertionError(f"timed out after {timeout_s}s waiting for {what}")


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _execute_in_kernel(port: int, code: str, timeout_s=180) -> subprocess.CompletedProcess:
    """Inject `code` into the live kernel via the marimo-pair bridge (runs
    in-kernel, so marimo._code_mode.get_context() attaches to this session)."""
    return subprocess.run(
        ["bash", str(EXECUTE_CODE), "--url", f"http://127.0.0.1:{port}", "-c", code],
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )


def test_live_reactive_sweep_and_html_export(bw, tmp_path):
    work = tmp_path
    runs_dir = work / "runs"
    runs_dir.mkdir()
    nb = work / "live_nb.py"
    nb.write_text(NOTEBOOK_SRC)

    # The kernel subprocess inherits this; the notebook cells read it.
    os.environ["BRIEFLOW_MARIMO_TEST_DIR"] = str(runs_dir)
    up = runs_dir / "upstream_runs.txt"
    down = runs_dir / "downstream_runs.txt"

    pid, port, session_id = bw.start_marimo_kernel(nb, watch=False, ready_timeout_s=60.0)
    try:
        # --- Baseline: run every cell once (kernel boots stale) ---
        baseline_code = (
            _KERNEL_PREAMBLE
            + "from brieflow_wizard import _await_kernel_settle\n"
            + "ctx = cm.get_context()\n"
            + "ids = [c.id for c in ctx.cells]\n"
            + "async def _r():\n"
            + "    async with cm.get_context() as cw:\n"
            + "        for cid in ids:\n"
            + "            cw.run_cell(cid)\n"
            + "asyncio.run(_r())\n"
            + "_await_kernel_settle(cm.get_context, timeout_s=120.0)\n"
            + "print('BASELINE_OK', len(ids))\n"
        )
        res0 = _execute_in_kernel(port, baseline_code)
        assert res0.returncode == 0, f"baseline failed:\nOUT:{res0.stdout}\nERR:{res0.stderr}"
        _wait_until(lambda: _count_lines(up) >= 1 and _count_lines(down) >= 1,
                    what="baseline DAG run")
        up0, down0 = _count_lines(up), _count_lines(down)
        assert up0 >= 1, "upstream never ran in baseline"

        # --- Drive the reactive sweep through the production function ---
        values = [2, 3, 4]
        sweep_code = (
            _KERNEL_PREAMBLE
            + "from brieflow_wizard import run_marimo_sweep\n"
            + "df = asyncio.run(run_marimo_sweep(\n"
            + "    session={'ctx_factory': cm.get_context},\n"
            + "    param_name='THRESHOLD',\n"
            + f"    values={values},\n"
            + f"    out_log={str(work / 'sweep.tsv')!r},\n"
            + "    timeout_s=120.0,\n"
            + "))\n"
            + "print('SWEEP_OK', len(df))\n"
        )
        res = _execute_in_kernel(port, sweep_code)
        assert res.returncode == 0, f"sweep failed:\nOUT:{res.stdout}\nERR:{res.stderr}"
        assert "SWEEP_OK" in res.stdout, res.stdout

        _wait_until(lambda: _count_lines(down) >= down0 + len(values),
                    what="downstream re-runs")
        up1, down1 = _count_lines(up), _count_lines(down)

        # THE REACTIVE CONTRACT:
        assert up1 == up0, (
            f"upstream re-ran ({up0}->{up1}) — reactive caching is broken; the "
            f"sweep would pay for upstream work every iteration"
        )
        assert down1 == down0 + len(values), (
            f"downstream re-ran {down1 - down0}x, expected {len(values)} (one per swept value)"
        )

        # A controlled handoff: edit the same live notebook directly, outside the
        # agent sweep. This tests the reactive API, not concurrent browser editing.
        direct_code = (
            _KERNEL_PREAMBLE
            + "from brieflow_wizard import _await_kernel_settle\n"
            + "cell = next(c for c in cm.get_context().cells if 'THRESHOLD = 4' in c.code)\n"
            + "async def _edit_directly():\n"
            + "    async with cm.get_context() as cw:\n"
            + "        cw.edit_cell(cell.id, code=cell.code.replace('THRESHOLD = 4', 'THRESHOLD = 7'))\n"
            + "        cw.run_cell(cell.id)\n"
            + "asyncio.run(_edit_directly())\n"
            + "_await_kernel_settle(cm.get_context, timeout_s=120.0)\n"
            + "print('DIRECT_EDIT_OK')\n"
        )
        direct = _execute_in_kernel(port, direct_code)
        assert direct.returncode == 0, direct.stdout + direct.stderr
        _wait_until(lambda: _count_lines(down) == down1 + 1, what="direct edit propagation")
        assert _count_lines(up) == up1
        assert down.read_text().splitlines()[-1] == "d 7"

        # --- Agent-side export preserves the direct edit and writes HTML ---
        out_html = work / "phase.final.html"
        export_code = (
            _KERNEL_PREAMBLE
            + "from brieflow_wizard import export_phase_html\n"
            + "r = asyncio.run(export_phase_html(\n"
            + f"    {str(nb)!r}, {str(out_html)!r},\n"
            + "    session={'ctx_factory': cm.get_context},\n"
            + "))\n"
            + "print('EXPORT_OK', r['ok'])\n"
        )
        res2 = _execute_in_kernel(port, export_code)
        assert res2.returncode == 0, f"export failed:\nOUT:{res2.stdout}\nERR:{res2.stderr}"

        # ctx.save() must have flushed THRESHOLD = 7 (directly edited value) to the .py
        # file before export — proves the disk<->kernel sync checkpoint.
        _wait_until(lambda: "THRESHOLD = 7" in nb.read_text(),
                    timeout_s=30, what="ctx.save() flush of swept value to disk")
        assert out_html.exists(), "export_phase_html did not write the HTML file"
    finally:
        bw.stop_marimo_kernel(pid)

    # Kernel is gone after stop — the port frees up (no leaked server/holder).
    _wait_until(lambda: not _port_open(port), timeout_s=30, what="kernel port to close")

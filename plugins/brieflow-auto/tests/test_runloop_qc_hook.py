"""The runloop emits a post-phase QC summary after each successful phase."""
from __future__ import annotations
import importlib.util
import sys
from pathlib import Path

RUNLOOP = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_runloop.py"


def _load():
    spec = importlib.util.spec_from_file_location("brieflow_runloop", RUNLOOP)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_runloop"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_emit_phase_qc_writes_summary_and_returns(tmp_path):
    rl = _load()
    out = tmp_path / "brieflow_output"
    (out / "sbs" / "eval" / "mapping" / "1").mkdir(parents=True)
    (out / "sbs" / "eval" / "mapping" / "1" / "mapping_overview.tsv").write_text(
        "well\ttotal_cells__count\t1_barcode_cells__count\t1_barcode_cells__percent\t"
        "1_or_more_barcodes__count\t1_or_more_barcodes__percent\t1_gene_cells__count\t"
        "1_gene_cells__percent\t1_or_more_genes__count\t1_or_more_genes__percent\n"
        "A1\t1000\t320\t32.0\t940\t94.0\t600\t60.0\t860\t86.0\n"
    )
    # emit_phase_qc resolves brieflow_output under analysis_dir, builds + writes
    summary = rl.emit_phase_qc(tmp_path, "sbs")
    assert summary is not None
    assert summary.phase == "sbs"
    assert (out / "sbs" / "eval" / "qc_summary.json").exists()
    assert (out / "sbs" / "eval" / "qc_summary.html").exists()


def test_emit_phase_qc_never_raises(tmp_path):
    rl = _load()
    # no brieflow_output at all -> must not raise (QC is best-effort, never
    # fails a successful phase)
    assert rl.emit_phase_qc(tmp_path, "sbs") is None


def test_emit_phase_qc_swallows_build_errors(tmp_path, monkeypatch):
    rl = _load()
    out = tmp_path / "brieflow_output"
    (out / "sbs" / "eval").mkdir(parents=True)  # phase dir exists so we reach build
    if rl._qc is None:
        import pytest
        pytest.skip("brieflow_qc not importable")
    monkeypatch.setattr(
        rl._qc,
        "build_phase_qc",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert rl.emit_phase_qc(tmp_path, "sbs") is None  # must not raise


def test_modules_path_emits_qc(tmp_path, monkeypatch):
    """Fix 3 regression: the --modules single-phase path must call emit_phase_qc
    after a successful run_one_phase, just as the multi-phase loop does.

    Exercises the code path via the source directly — checks that the same
    emit_phase_qc loop that exists in the multi-phase branch was added to
    the --modules branch (around the `if args.modules:` block).
    """
    import inspect

    rl = _load()

    # Parse the source and verify emit_phase_qc appears inside the
    # `if args.modules:` block — structural check that Fix 3 is present.
    src = inspect.getsource(rl)
    # The --modules block must contain _known_phases and emit_phase_qc before
    # on_post_terminal_phase; verify by checking the source order.
    modules_block_start = src.index("if args.modules:")
    # Find the next occurrence of "if args.phases" or "# Multi-phase" which
    # marks the end of the single-phase block.
    multi_phase_marker = src.index("# Multi-phase mode:", modules_block_start)
    single_phase_block = src[modules_block_start:multi_phase_marker]

    assert "emit_phase_qc" in single_phase_block, (
        "emit_phase_qc is not called in the --modules single-phase branch. "
        "Fix 3 requires QC emission after a successful run_one_phase."
    )
    assert "_known_phases" in single_phase_block, (
        "_known_phases guard missing from --modules branch."
    )

    # Also verify the call appears BEFORE on_post_terminal_phase in that block,
    # matching the multi-phase loop ordering (QC then terminal hook).
    qc_pos = single_phase_block.index("emit_phase_qc")
    hook_pos = single_phase_block.index("on_post_terminal_phase")
    assert qc_pos < hook_pos, (
        "emit_phase_qc must be called before on_post_terminal_phase in the "
        "--modules branch."
    )

"""Contract for the canonical phase list (brieflow_phases) — the single source
of truth every other phase view derives from, so they can't drift.
"""
from __future__ import annotations


def test_views_derive_from_pipeline_phases(load_script):
    p = load_script("brieflow_phases")
    # PHASE_ORDER is exactly the pipeline phase names, in order.
    assert p.PHASE_ORDER == tuple(ph for ph, _ in p.PIPELINE_PHASES)
    # PHASES = full order minus the not-yet-built slots, order preserved.
    assert p.PHASES == [ph for ph in p.PHASE_ORDER if ph not in p.NOT_YET_BUILT]
    # classify and analyze ship notebooks, so they are active phases, but
    # neither has a snakemake module — their pipeline-run steps are no-ops.
    assert "classify" in p.PHASES
    assert "analyze" in p.PHASES
    assert {"classify", "analyze"} <= p.NOTEBOOK_ONLY_PHASES
    assert p.NOTEBOOK_ONLY_PHASES <= set(p.PHASE_ORDER)
    # notebook map is 1:1 with the pipeline.
    assert p.PHASE_NOTEBOOKS == dict(p.PIPELINE_PHASES)
    assert set(p.PHASE_NOTEBOOKS) == set(p.PHASE_ORDER)

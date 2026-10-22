"""brieflow_phases.py — canonical pipeline phase order (single source of truth).

Phases are data. The full dependency order lives here once, as (phase, notebook)
pairs; every other view is derived from it so they cannot drift:

  * ``PIPELINE_PHASES`` — full order incl. the optional ``classify`` slot.
    Consumed by the end-to-end run chain.
  * ``PHASE_ORDER`` — just the ordered phase names (dry-run / rule mapping).
  * ``PHASES`` — active phases that ship a canonical notebook / interview / QC
    (``PIPELINE_PHASES`` minus the not-yet-built slots).
  * ``NOTEBOOK_ONLY_PHASES`` — phases whose notebook writes config but that
    have no snakemake module, so their pipeline-run step is a no-op.
  * ``PHASE_NOTEBOOKS`` — phase -> notebook filename.

Add or reorder a stage by editing ``PIPELINE_PHASES``; the run chain, dry-run
order, QC, interview review, and the runner loop all follow.
"""

from __future__ import annotations

#: Full dependency order as (phase, phase-notebook filename). preprocess writes
#: the combo TSVs later phases reference, so order is load-bearing.
PIPELINE_PHASES: list[tuple[str, str]] = [
    ("preprocess", "0_preprocess.py"),
    ("sbs", "1_sbs.py"),
    ("phenotype", "2_phenotype.py"),
    ("merge", "3_merge.py"),
    ("classify", "4_classify.py"),
    ("aggregate", "5_aggregate.py"),
    ("cluster", "6_cluster.py"),
    ("analyze", "7_analyze.py"),
]

#: Phases present in the chain but not yet shipping a canonical notebook —
#: skipped silently when their notebook is absent.
NOT_YET_BUILT: frozenset[str] = frozenset()

#: Phases configured by a notebook that writes config.yml but backed by no
#: snakemake module of their own name. Their notebook still runs; the
#: pipeline-run, dry-run and QC steps are no-ops. analyze configures the
#: billable mozzarellm stage, which the operator launches on demand with
#: ``bash flow.sh mozzarellm`` rather than as a step of the phase chain.
NOTEBOOK_ONLY_PHASES: frozenset[str] = frozenset({"classify", "analyze"})

#: Ordered phase names, including optional slots.
PHASE_ORDER: tuple[str, ...] = tuple(phase for phase, _ in PIPELINE_PHASES)

#: Active phases with a canonical notebook / interview / QC.
PHASES: list[str] = [phase for phase, _ in PIPELINE_PHASES if phase not in NOT_YET_BUILT]

#: phase -> notebook filename.
PHASE_NOTEBOOKS: dict[str, str] = dict(PIPELINE_PHASES)


def phase_notebook(analysis_dir, phase: str):
    """Path of the phase's notebook in a screen: the current name, else any ``<n>_<phase>.py``
    (screens created before the 0-7 renumbering keep their old numbers)."""
    from pathlib import Path

    analysis_dir = Path(analysis_dir)
    current = analysis_dir / PHASE_NOTEBOOKS.get(phase, f"{phase}.py")
    if current.exists():
        return current
    older = sorted(analysis_dir.glob(f"[0-9]*_{phase}.py"))
    return older[0] if older else current


def notebook_phase(nb_path) -> str | None:
    """Phase of a notebook path from its ``<n>_<phase>.py`` name, whatever the number."""
    from pathlib import Path

    stem = Path(nb_path).stem
    phase = stem.split("_", 1)[1] if "_" in stem and stem.split("_", 1)[0].isdigit() else None
    return phase if phase in PHASE_NOTEBOOKS else None

"""A head-node sweep where 1 of 36 runs produced a mapping rate still recorded its grid-edge
values as tuned-confirmed. Below the evidence floor the parameter now stays unresolved and the
phase pauses."""

import importlib.util
import sys
from pathlib import Path

import pandas as pd

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import brieflow_sweep as bs  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_resolve_phase import _base_ctx  # noqa: E402


def _resolvers():
    spec = importlib.util.spec_from_file_location("brieflow_resolvers", SCRIPTS / "brieflow_resolvers.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _summary(finite_by_value, trials=3):
    """A sweep summary as sweep_headless returns it, with per-trial finite counts attached."""
    rows = [{"value": v, "single_rate": 0.2 if i < finite else None}
            for v, finite in finite_by_value.items() for i in range(trials)]
    jobs = pd.DataFrame(rows)
    df = pd.DataFrame([{"value": v, "mean_single_rate": 0.2 if f else None, "n_tiles": trials}
                       for v, f in finite_by_value.items()])
    df.attrs["evidence"] = bs._finite_counts(jobs, ["value"])
    return df


def test_floor_rejects_a_sweep_that_mostly_failed():
    df = _summary({100: 0, 400: 0, 3200: 1, 6400: 0}, trials=9)
    ok, detail = bs.sweep_evidence(df, 3200)
    assert not ok and "1/36 trials finite" in detail


def test_floor_needs_the_winner_on_two_tiles():
    assert not bs.sweep_evidence(_summary({100: 3, 400: 3, 3200: 1}), 3200)[0]
    assert bs.sweep_evidence(_summary({100: 3, 400: 2, 3200: 1}), 400)[0]


def test_joint_floor_counts_tiles_per_pair():
    rows = pd.DataFrame(
        [{"tile": t, "outer": 15, "inner": 400, "single_rate": 0.5} for t in ("a", "b", "c")]
        + [{"tile": "a", "outer": 16, "inner": 400, "single_rate": 0.65}]
    )
    df = pd.DataFrame([{"outer": 15, "inner": 400}, {"outer": 16, "inner": 400}])
    df.attrs["evidence"] = bs._finite_counts(rows, ["outer", "inner"])
    assert bs.sweep_evidence(df, (15, 400))[0]
    assert not bs.sweep_evidence(df, (16, 400))[0]


def test_summaries_keep_their_columns_and_carry_counts():
    jobs = pd.DataFrame([{"value": 1, "single_rate": None}, {"value": 1, "single_rate": 0.3}])
    assert bs._finite_counts(jobs, ["value"]) == {1: {"trials": 2, "finite": 1, "tiles": 1}}


def test_unsupported_winner_pauses_instead_of_recording(tmp_path):
    resolvers = _resolvers()
    weak = _summary({100: 0, 400: 0, 3200: 1}, trials=11)

    async def _sweep(nb, param, band, tiles, **kw):
        return weak

    ctx = _base_ctx(
        tmp_path, "sbs",
        lab={"sweep_bands": {"sbs": {"THRESHOLD_READS": [100, 400, 3200]}},
             "sweep_tiles": {"n_tiles": 2}},
        sweep_headless=_sweep,
        pick_sweep_winner=bs.pick_sweep_winner,
        sweep_evidence=bs.sweep_evidence,
    )
    res = resolvers.resolve_phase("sbs", ctx)
    assert res["paused"] is True and "THRESHOLD_READS" in res["reason"]
    assert "not backed by data" in res["reason"]
    assert ctx["_recorded"] == [] and ctx["_patched"] == []

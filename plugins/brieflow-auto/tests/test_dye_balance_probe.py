"""Dye-balance probe: candidate construction and the refusal logic.

The decision surface, not the read path (that runs brieflow lib on a real tile).
What matters here is that the probe offers the configured barcode regions ONLY to a
screen that genuinely has two of them — a simple-barcode two-colour library must never
have a MAP/RECOMB split invented for it — and that nothing is written unless it beats
the stock call without raising the decoy rate.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
np = pytest.importorskip("numpy")
pd = pytest.importorskip("pandas")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


probe = _load("brieflow_dye_balance_probe")

MULTI = {"barcode_type": "multi", "map_start": 6, "map_end": 12, "recomb_start": 1, "recomb_end": 5}
SIMPLE = {"barcode_type": "simple", "prefix_col": "prefix"}


# --- configured regions: only when the screen really has two ------------------
def test_multi_screen_offers_its_two_barcode_regions():
    assert probe._configured_regions(MULTI, 12) == [[1, 5], [6, 12]]


def test_simple_barcode_screen_gets_no_configured_regions():
    """A two-colour T7 library has one region; there is no split to hold apart."""
    assert probe._configured_regions(SIMPLE, 12) is None


def test_multi_screen_missing_a_bound_gets_no_regions():
    for missing in ("map_start", "map_end", "recomb_start", "recomb_end"):
        sbs = {k: v for k, v in MULTI.items() if k != missing}
        assert probe._configured_regions(sbs, 12) is None


@pytest.mark.parametrize(
    "sbs",
    [
        {**MULTI, "map_start": 1, "map_end": 6, "recomb_start": 5, "recomb_end": 11},  # overlapping
        {**MULTI, "map_start": 6, "map_end": 6},  # single cycle
        {**MULTI, "recomb_end": 99},  # past the last cycle
        {**MULTI, "map_start": 0},  # not 1-based
    ],
)
def test_incoherent_windows_get_no_regions(sbs):
    assert probe._configured_regions(sbs, 12) is None


def test_region_candidates_for_a_simple_screen_are_stock_only_without_a_step():
    flat = {"cycle_median_total": [100.0] * 12}
    cands = probe._region_candidates(SIMPLE, 12, flat)
    assert [c["source"] for c in cands] == ["stock"]


def test_region_candidates_offer_a_detected_step_even_without_configured_regions():
    stepped = {"cycle_median_total": [400.0] * 6 + [100.0] * 6}
    cands = probe._region_candidates(SIMPLE, 12, stepped)
    assert [c["source"] for c in cands] == ["stock", "detected-brightness-step"]
    assert cands[1]["value"] == [[1, 6], [7, 12]]


def test_region_candidates_do_not_repeat_the_configured_split():
    stepped = {"cycle_median_total": [100.0] * 5 + [400.0] * 7}
    cands = probe._region_candidates(MULTI, 12, stepped)
    assert [c["source"] for c in cands] == ["stock", "configured-barcode-regions"]


# --- detected step ------------------------------------------------------------
def test_no_step_is_detected_in_a_flat_profile():
    assert probe._detect_regions([100.0] * 12, 12) is None


def test_step_needs_two_cycles_on_each_side():
    assert probe._detect_regions([400.0] + [100.0] * 11, 12) is None


def test_detect_regions_ignores_a_profile_of_the_wrong_length():
    assert probe._detect_regions([100.0] * 6, 12) is None


# --- dye scale ----------------------------------------------------------------
def test_dye_scale_levels_a_dim_dye_against_the_brightest():
    profile = {"dye_p90_bright": {"A488": 600.0, "A568": 900.0, "A647": 880.0}}
    cands = probe._dye_scale_candidates(profile, ["A488", "A568", "A647"])
    assert cands[0]["value"] is None
    exact = next(c for c in cands if c["source"] == "level-to-brightest-dye")["value"]
    assert exact["A488"] == pytest.approx(1.5, abs=0.01)
    assert "A568" not in exact  # already the brightest, nothing to scale


def test_balanced_dyes_get_no_scale_candidate():
    profile = {"dye_p90_bright": {"A488": 880.0, "A568": 900.0, "A647": 890.0}}
    cands = probe._dye_scale_candidates(profile, ["A488", "A568", "A647"])
    assert [c["source"] for c in cands] == ["stock"]


def test_missing_dye_levels_fall_back_to_stock_only():
    cands = probe._dye_scale_candidates({}, ["A488", "A568"])
    assert [c["source"] for c in cands] == ["stock"]


# --- the refusal logic --------------------------------------------------------
def _row(exact, decoy, scale=None, regions=None):
    return {"exact": exact, "decoy": decoy, "dye_scale": scale, "brightness_regions": regions}


def test_a_clear_winner_is_picked():
    stock = _row(0.25, 0.002)
    better = _row(0.40, 0.001, scale={"A488": 1.5})
    assert probe._pick_winner([stock, better], stock, 0.05) is better


def test_a_candidate_that_also_finds_more_decoys_is_refused():
    """More library hits AND more decoy hits means calling more of everything."""
    stock = _row(0.25, 0.002)
    noisy = _row(0.40, 0.02, scale={"A488": 4.0})
    assert probe._pick_winner([stock, noisy], stock, 0.05) is stock


def test_a_screen_with_no_barcode_signal_is_refused_a_calibration():
    """Tripling a match rate of 0.003 is still noise. Measured on a
    plate with no barcode signal, where stock sat at 0.0026 against a decoy level of 0.0102."""
    stock = _row(0.0026, 0.0102)
    noise = _row(0.0102, 0.0, scale={"A488": 3.6})
    assert probe._pick_winner([stock, noise], stock, 0.05) is stock


def test_a_candidate_barely_above_its_own_decoy_is_refused():
    stock = _row(0.03, 0.01)
    thin = _row(0.05, 0.02, scale={"A488": 1.5})
    assert probe._pick_winner([stock, thin], stock, 0.05) is stock


def test_a_decoy_rate_that_rises_more_slowly_than_the_signal_is_accepted():
    """Measured on a real plate: exact 0.186 -> 0.255 (+37%) while decoy went
    0.0039 -> 0.0048 (+23%). Purity improved; refusing on the raw decoy count would
    have thrown away a third more mapped reads."""
    stock = _row(0.1861, 0.0039)
    better = _row(0.2546, 0.0048, regions=[[1, 7], [8, 12]])
    assert probe._pick_winner([stock, better], stock, 0.05) is better


def test_a_gain_under_the_margin_is_refused():
    stock = _row(0.25, 0.002)
    marginal = _row(0.256, 0.002, scale={"A488": 1.1})
    assert probe._pick_winner([stock, marginal], stock, 0.05) is stock


def test_the_best_of_several_qualifying_candidates_wins():
    stock = _row(0.25, 0.002)
    good = _row(0.36, 0.002, scale={"A488": 1.25})
    best = _row(0.54, 0.001, scale={"A488": 1.5}, regions=[[1, 5], [6, 12]])
    assert probe._pick_winner([stock, good, best], stock, 0.05) is best


def test_a_stock_call_with_no_decoys_still_admits_a_clean_winner():
    """The decoy ceiling has a floor of its own, so a zero-decoy stock call does not
    make every candidate unbeatable."""
    stock = _row(0.25, 0.0)
    better = _row(0.40, 0.001, scale={"A488": 1.5})
    assert probe._pick_winner([stock, better], stock, 0.05) is better


# --- decoy library ------------------------------------------------------------
def test_decoys_hold_the_librarys_own_spacing():
    lib = {"ACGTACG", "TGCATGC", "GGTTAAC", "CCAATTG"}
    decoys = probe._decoy_library(lib, n=20, seed=1)
    assert decoys and not (decoys & lib)
    arr = np.array([list(p) for p in sorted(lib)])
    pair = (arr[:, None, :] != arr[None, :, :]).sum(2)
    np.fill_diagonal(pair, arr.shape[1] + 1)
    for d in decoys:
        assert (arr != np.array(list(d))).sum(1).min() >= int(pair.min())


def test_decoy_library_of_an_empty_library_is_empty():
    assert probe._decoy_library(set()) == set()


# --- brightness profile -------------------------------------------------------
def test_brightness_profile_reports_per_cycle_totals_and_dye_levels():
    rows = []
    for read in range(30):
        for cycle in range(1, 5):
            level = 900.0 if cycle <= 2 else 150.0
            for ch, val in (("A488", level), ("A568", level / 3), ("DAPI", 10.0)):
                rows.append(dict(read=read, cycle=cycle, channel=ch, intensity=val))
    profile = probe._brightness_profile(pd.DataFrame(rows), ["A488", "A568"])
    assert profile["cycle_median_total"] == [1200.0, 1200.0, 200.0, 200.0]
    assert profile["dye_p90_bright"]["A488"] == pytest.approx(900.0)
    assert "DAPI" not in profile["dye_p90_bright"]


def test_a_paired_dye_is_scored_on_bright_cycles_not_where_it_dominates():
    """A dye that is always the junior partner is never the dominant channel; scoring
    it there reads it as far dimmer than it is and asks for an absurd scale."""
    rows = []
    for read in range(40):
        for cycle in range(1, 5):
            for ch, val in (("A488", 300.0), ("A568", 900.0)):
                rows.append(dict(read=read, cycle=cycle, channel=ch, intensity=val))
    profile = probe._brightness_profile(pd.DataFrame(rows), ["A488", "A568"])
    assert profile["dye_p90_bright"]["A488"] == pytest.approx(300.0)
    scale = next(
        c
        for c in probe._dye_scale_candidates(profile, ["A488", "A568"])
        if c["source"] == "level-to-brightest-dye"
    )["value"]
    assert scale["A488"] == pytest.approx(3.0, abs=0.01)


def test_brightness_profile_of_an_empty_table_is_empty():
    assert probe._brightness_profile(pd.DataFrame(), ["A488"]) == {}

"""Tests for the workflow/lib scan + chemistry/call_reads-method accepted-value
guards in brieflow_preflight.py (Task 7, branch-drift-combinatorial-sbs).

Before this change, analyze_brieflow only scanned workflow/scripts/**/*.py, so
combinatorial-SBS's sbs.chemistry guard (workflow/lib/sbs/call_reads.py) and its
chemistry-conditional method guard were invisible to preflight — a bad chemistry
or a frac/four_color mismatch passed preflight and only failed at runtime.

Note: surface.accepted_value_guards dicts use the "key" field (not "var") for
the config key being validated — this matches the pre-existing
parse_unknown_value_guards()/validate_config() convention (guard["key"]) in
this module; there is no "var" field anywhere in the real schema.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import brieflow_preflight as pf  # noqa: E402

BRIEFLOW = os.environ.get("BRIEFLOW_CHECKOUT", "")

pytestmark = pytest.mark.skipif(
    not BRIEFLOW or not Path(BRIEFLOW).is_dir(),
    reason="BRIEFLOW_CHECKOUT unset or not a brieflow checkout",
)


def test_chemistry_accepted_values_discovered():
    surface = pf.analyze_brieflow(Path(BRIEFLOW))
    guards = surface.accepted_value_guards  # list[dict] with keys key/accepted
    chem = [g for g in guards if "chemistry" in str(g.get("key", "")).lower()]
    assert chem, "preflight should discover the sbs.chemistry accepted-value guard in workflow/lib"
    assert {"four_color", "combinatorial"} <= set(chem[0]["accepted"])


def test_method_guard_is_conditional_on_chemistry():
    """The `supported = {...}` dict shape yields a `method` guard whose
    accepted set depends on `chemistry`'s value, not a flat set."""
    surface = pf.analyze_brieflow(Path(BRIEFLOW))
    guards = surface.accepted_value_guards
    method_guards = [g for g in guards if g.get("key") == "method" and "call_reads" in g.get("file", "")]
    assert method_guards, "preflight should discover the call_reads method guard"
    guard = method_guards[0]
    assert guard.get("depends_on") == "chemistry"
    by_dep = guard.get("accepted_by_dependency", {})
    assert set(by_dep.get("four_color", [])) == {"median", "percentile"}
    assert set(by_dep.get("combinatorial", [])) == {"frac", "merfish"}
    # Flat union fallback (used when `chemistry` isn't resolvable in config).
    assert set(guard["accepted"]) == {"median", "percentile", "frac", "merfish"}


def test_frac_four_color_mismatch_flagged_high(tmp_path):
    """The motivating bug: chemistry=four_color + method=frac (valid only
    for combinatorial) must be caught pre-run, not fail deep into an sbs run."""
    surface = pf.analyze_brieflow(Path(BRIEFLOW))
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "sbs:\n"
        "  chemistry: four_color\n"
        "  method: frac\n"
    )
    issues = pf.validate_config(config_path, surface)
    bad = [i for i in issues if i.code == "CONFIG_VALUE_NOT_ACCEPTED" and "method" in i.message]
    assert bad, "chemistry=four_color + method=frac should be flagged"
    assert bad[0].severity == "HIGH"


def test_valid_chemistry_method_combo_not_flagged(tmp_path):
    surface = pf.analyze_brieflow(Path(BRIEFLOW))
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "sbs:\n"
        "  chemistry: four_color\n"
        "  method: median\n"
    )
    issues = pf.validate_config(config_path, surface)
    bad = [i for i in issues if i.code == "CONFIG_VALUE_NOT_ACCEPTED" and "method" in i.message]
    assert not bad, f"chemistry=four_color + method=median is valid, should not be flagged: {bad}"


def test_unknown_chemistry_value_flagged_high(tmp_path):
    surface = pf.analyze_brieflow(Path(BRIEFLOW))
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "sbs:\n"
        "  chemistry: bogus_chemistry\n"
        "  method: median\n"
    )
    issues = pf.validate_config(config_path, surface)
    bad = [i for i in issues if i.code == "CONFIG_VALUE_NOT_ACCEPTED" and "chemistry" in i.message]
    assert bad
    assert bad[0].severity == "HIGH"


def test_five_preexisting_lib_unknown_guards_do_not_surface():
    """Step-1 audit lock: workflow/lib has 5 other `Unknown ...` guards
    (cluster_eval.metric, classify/train.{scaler_type,model_type},
    cluster_analysis.normalize_method, aggregate/align.method-'scaling
    method'). None of them are wired to a real config[...]/snakemake.params
    key (verified: no caller in workflow/scripts or workflow/rules — they're
    notebook-only), so surfacing them would risk false-positiving a valid
    config on a generic key name. This test guards against a future regex
    widening silently starting to surface them without a matching allowlist
    update."""
    surface = pf.analyze_brieflow(Path(BRIEFLOW))
    keys = {g["key"] for g in surface.accepted_value_guards}
    assert "metric" not in keys
    assert "scaler_type" not in keys
    assert "model_type" not in keys
    assert "normalize_method" not in keys

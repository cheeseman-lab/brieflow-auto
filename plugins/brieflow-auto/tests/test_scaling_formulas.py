"""Tests for the scaling-formula resolver (HARDENING #23).

Covers: geometry probe, formula evaluation, layered resolution priority,
fallback to slurm floor, mem_recs overrides take precedence.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
GEOMETRY_PY = SCRIPT_DIR / "brieflow_screen_geometry.py"
RESOLVE_PY = SCRIPT_DIR / "brieflow_resolve_resources.py"


@pytest.fixture(scope="module")
def resolver():
    spec = importlib.util.spec_from_file_location(
        "brieflow_resolve_resources", RESOLVE_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_resolve_resources"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def geometry_mod():
    spec = importlib.util.spec_from_file_location(
        "brieflow_screen_geometry", GEOMETRY_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_screen_geometry"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Geometry probe
# ---------------------------------------------------------------------------


def _write_minimal_analysis_dir(tmp_path: Path) -> Path:
    """Build a tiny analysis dir with combo TSVs + config."""
    analysis = tmp_path / "analysis"
    cfg_dir = analysis / "config"
    cfg_dir.mkdir(parents=True)

    (cfg_dir / "config.yml").write_text(
        "all:\n  scope: test\n"
        "preprocess:\n"
        "  sbs_combo_fp: config/sbs_combo.tsv\n"
        "  phenotype_combo_fp: config/phenotype_combo.tsv\n"
    )
    # 1 plate, 2 wells, 3 cycles, 2 channels, 4 tiles per well per cycle.
    sbs_rows = []
    for plate in [1]:
        for well in ["A1", "A2"]:
            for cycle in [1, 2, 3]:
                for tile in range(4):
                    for ch in ["G", "T"]:
                        sbs_rows.append((plate, cycle, well, ch, tile))
    with open(cfg_dir / "sbs_combo.tsv", "w") as f:
        f.write("plate\tcycle\twell\tchannel\ttile\n")
        for r in sbs_rows:
            f.write("\t".join(str(x) for x in r) + "\n")
    # 1 plate, 2 wells, 1 round, 1 channel, 10 tiles per well.
    ph_rows = []
    for plate in [1]:
        for well in ["A1", "A2"]:
            for round_ in [1]:
                for tile in range(10):
                    ph_rows.append((plate, round_, well, "DAPI", tile))
    with open(cfg_dir / "phenotype_combo.tsv", "w") as f:
        f.write("plate\tround\twell\tchannel\ttile\n")
        for r in ph_rows:
            f.write("\t".join(str(x) for x in r) + "\n")
    return analysis


def test_probe_basic_geometry(geometry_mod, tmp_path: Path):
    analysis = _write_minimal_analysis_dir(tmp_path)
    geo = geometry_mod.probe_geometry(analysis, analysis / "config" / "config.yml")
    assert geo["n_plates"] == 1
    assert geo["n_wells"] == 2
    assert geo["n_cycles"] == 3
    assert geo["n_channels_per_cycle_sbs"] == 2
    assert geo["n_tiles_per_well_sbs"] == 4
    assert geo["n_tiles_per_well_per_cycle_sbs"] == 4
    assert geo["n_rounds_phenotype"] == 1
    assert geo["n_channels_per_round_phenotype"] == 1
    assert geo["n_tiles_per_well_phenotype"] == 10
    assert geo["n_tiles_per_well_per_round_phenotype"] == 10


def test_probe_missing_combos_returns_none(geometry_mod, tmp_path: Path):
    """Missing combo TSVs → all geometry fields None (probe tolerates)."""
    analysis = tmp_path / "empty"
    (analysis / "config").mkdir(parents=True)
    (analysis / "config" / "config.yml").write_text("all:\n  scope: empty\n")
    geo = geometry_mod.probe_geometry(analysis, analysis / "config" / "config.yml")
    assert geo["n_plates"] is None
    assert geo["n_tiles_per_well_sbs"] is None


def test_probe_writes_geometry_json(tmp_path: Path):
    analysis = _write_minimal_analysis_dir(tmp_path)
    r = subprocess.run(
        [sys.executable, str(GEOMETRY_PY), "--analysis-dir", str(analysis)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    p = analysis / ".brieflow" / "geometry.json"
    assert p.exists()
    geo = json.loads(p.read_text())
    assert geo["n_wells"] == 2


# ---------------------------------------------------------------------------
# Formula evaluation
# ---------------------------------------------------------------------------


def test_evaluate_constant(resolver):
    entry = {"scaling": "constant", "mem_mb": 5000, "margin": 1.0}
    assert resolver.evaluate_formula(entry, {}) == 5000


def test_evaluate_constant_with_margin(resolver):
    entry = {"scaling": "constant", "mem_mb": 1000, "margin": 1.5}
    assert resolver.evaluate_formula(entry, {}) == 1500


def test_evaluate_per_tile(resolver):
    """per_tile uses N=1 — degenerates to k+b."""
    entry = {"scaling": "per_tile", "k_mb": 0, "b_mb": 3000, "margin": 1.0}
    assert resolver.evaluate_formula(entry, {}) == 3000


def test_evaluate_per_well_phenotype(resolver):
    geo = {"n_tiles_per_well_phenotype": 1281}
    entry = {"scaling": "per_well_phenotype", "k_mb": 100, "b_mb": 1000, "margin": 1.0}
    # 100 * 1281 + 1000 = 129100
    assert resolver.evaluate_formula(entry, geo) == 129100


def test_evaluate_per_well_per_round_phenotype(resolver):
    geo = {"n_tiles_per_well_per_round_phenotype": 1281}
    entry = {
        "scaling": "per_well_per_round_phenotype",
        "k_mb": 270,
        "b_mb": 2000,
        "margin": 1.0,
    }
    # 270 * 1281 + 2000 = 347870
    assert resolver.evaluate_formula(entry, geo) == 347870


def test_evaluate_missing_geometry_field_returns_none(resolver):
    """If the geometry field is missing, evaluate returns None (caller falls
    through to next layer)."""
    entry = {"scaling": "per_well_phenotype", "k_mb": 10, "b_mb": 0}
    assert resolver.evaluate_formula(entry, {}) is None


def test_evaluate_unknown_scaling_label_returns_none(resolver):
    entry = {"scaling": "made_up_label", "mem_mb": 5000}
    assert resolver.evaluate_formula(entry, {}) is None


def test_evaluate_margin_applied(resolver):
    geo = {"n_tiles_per_well_sbs": 100}
    entry = {"scaling": "per_well_sbs", "k_mb": 10, "b_mb": 0, "margin": 1.3}
    # 10*100 + 0 = 1000 * 1.3 = 1300
    assert resolver.evaluate_formula(entry, geo) == 1300


# ---------------------------------------------------------------------------
# Layered resolution priority
# ---------------------------------------------------------------------------


def _setup_resolution_dir(tmp_path: Path) -> Path:
    """Build an analysis dir with all four resolution layers populated."""
    analysis = tmp_path / "analysis"
    slurm = analysis / "slurm"
    brieflow_state = analysis / ".brieflow"
    slurm.mkdir(parents=True)
    brieflow_state.mkdir()

    # Layer 1: mem_recommendations.json (one rule with manual_override,
    # one with efficiency_aggregated observation).
    mem_recs = {
        "format_singlecell_anndata": {
            "mem_mb_recommended": 125000,
            "obs_source": "manual_override_after_OOM",
            "tier": "well",
        },
        "filter": {
            "mem_mb_recommended": 109100,
            "obs_source": "efficiency_aggregated",
            "tier": "well",
        },
    }
    (slurm / "mem_recommendations.json").write_text(json.dumps(mem_recs))

    # Layer 2: formula table (rule with formula, no mem_recs).
    formulas = {
        "defaults": {"margin": 1.0},
        "rules": {
            "calculate_ic_phenotype": {
                "scaling": "per_well_per_round_phenotype",
                "k_mb": 270,
                "b_mb": 2000,
                "margin": 1.0,
            },
            "bootstrap_gene": {
                "scaling": "constant",
                "mem_mb": 3300,
                "margin": 1.0,
            },
            "missing_geometry_rule": {
                "scaling": "per_well_per_combo",  # geometry has None
                "k_mb": 100,
                "b_mb": 0,
            },
        },
    }
    (slurm / "scaling_formulas.yaml").write_text(yaml.safe_dump(formulas))

    # Layer 3: slurm/config.yaml set-resources.
    slurm_cfg = {
        "default-resources": {"mem_mb": 3000},
        "set-resources": {
            "missing_geometry_rule": {"mem_mb": 7777},
            "another_floor_rule": {"mem_mb": 1234},
        },
    }
    (slurm / "config.yaml").write_text(yaml.safe_dump(slurm_cfg))

    # Geometry.
    geo = {
        "n_tiles_per_well_per_round_phenotype": 1281,
        "n_tiles_per_well_phenotype": 1281,
        "n_channel_combos": None,  # missing — forces fallback
    }
    (brieflow_state / "geometry.json").write_text(json.dumps(geo))
    return analysis


def test_resolution_manual_override_wins(resolver, tmp_path: Path):
    analysis = _setup_resolution_dir(tmp_path)
    out = resolver.resolve(analysis)
    assert out["format_singlecell_anndata"]["source"] == "manual_override"
    assert out["format_singlecell_anndata"]["mem_mb"] == 125000


def test_resolution_observation_wins_over_formula(resolver, tmp_path: Path):
    """A rule with mem_recs observation should not fall through to formula."""
    analysis = _setup_resolution_dir(tmp_path)
    out = resolver.resolve(analysis)
    assert out["filter"]["source"] == "observation"
    assert out["filter"]["mem_mb"] == 109100


def test_resolution_formula_when_no_mem_recs(resolver, tmp_path: Path):
    """No mem_recs entry → formula layer kicks in."""
    analysis = _setup_resolution_dir(tmp_path)
    out = resolver.resolve(analysis)
    assert out["calculate_ic_phenotype"]["source"] == "formula"
    # 270 * 1281 + 2000 = 347870
    assert out["calculate_ic_phenotype"]["mem_mb"] == 347870


def test_resolution_constant_formula(resolver, tmp_path: Path):
    analysis = _setup_resolution_dir(tmp_path)
    out = resolver.resolve(analysis)
    assert out["bootstrap_gene"]["source"] == "formula"
    assert out["bootstrap_gene"]["mem_mb"] == 3300


def test_resolution_slurm_floor_when_formula_unresolvable(resolver, tmp_path: Path):
    """Formula can't resolve (missing geometry field) → falls to slurm floor."""
    analysis = _setup_resolution_dir(tmp_path)
    out = resolver.resolve(analysis)
    assert out["missing_geometry_rule"]["source"] == "slurm_floor"
    assert out["missing_geometry_rule"]["mem_mb"] == 7777


def test_resolution_slurm_floor_when_no_formula(resolver, tmp_path: Path):
    """No mem_recs, no formula, only floor → slurm_floor."""
    analysis = _setup_resolution_dir(tmp_path)
    out = resolver.resolve(analysis)
    assert out["another_floor_rule"]["source"] == "slurm_floor"
    assert out["another_floor_rule"]["mem_mb"] == 1234


def test_to_set_resources_args(resolver, tmp_path: Path):
    """Output format is `rule:mem_mb=N` space-separated."""
    analysis = _setup_resolution_dir(tmp_path)
    out = resolver.resolve(analysis)
    args = resolver.to_set_resources_args(out)
    assert any("filter:mem_mb=109100" in a for a in args)
    assert any("bootstrap_gene:mem_mb=3300" in a for a in args)


# ---------------------------------------------------------------------------
# End-to-end via subprocess
# ---------------------------------------------------------------------------


def test_e2e_snakemake_format_output(tmp_path: Path):
    """Resolver --format snakemake emits space-separated rule:mem_mb=N args."""
    analysis = _setup_resolution_dir(tmp_path)
    r = subprocess.run(
        [
            sys.executable,
            str(RESOLVE_PY),
            "--analysis-dir",
            str(analysis),
            "--format",
            "snakemake",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    out = r.stdout.strip()
    parts = out.split()
    assert all(":mem_mb=" in p for p in parts)
    assert any(p.startswith("calculate_ic_phenotype:mem_mb=") for p in parts)


def test_e2e_json_format_includes_source(tmp_path: Path):
    analysis = _setup_resolution_dir(tmp_path)
    r = subprocess.run(
        [
            sys.executable,
            str(RESOLVE_PY),
            "--analysis-dir",
            str(analysis),
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    sources = {meta["source"] for meta in payload.values()}
    assert "manual_override" in sources
    assert "observation" in sources
    assert "formula" in sources
    assert "slurm_floor" in sources


# ---------------------------------------------------------------------------
# Baseline file shipped with plugin
# ---------------------------------------------------------------------------


def test_baseline_yaml_parses(resolver):
    """Plugin's shipped baseline must parse cleanly."""
    assert resolver.PLUGIN_BASELINE.exists()
    with open(resolver.PLUGIN_BASELINE) as f:
        data = yaml.safe_load(f)
    assert "rules" in data
    assert len(data["rules"]) > 30


def test_baseline_yaml_all_scalings_valid(resolver):
    """Every entry uses a valid closed-enum scaling label."""
    with open(resolver.PLUGIN_BASELINE) as f:
        data = yaml.safe_load(f)
    for rule, entry in data["rules"].items():
        assert entry["scaling"] in resolver.VALID_SCALINGS, (
            f"rule {rule!r} has unknown scaling {entry['scaling']!r}"
        )


def test_baseline_yaml_constant_has_mem_mb(resolver):
    """constant scaling entries must have mem_mb set."""
    with open(resolver.PLUGIN_BASELINE) as f:
        data = yaml.safe_load(f)
    for rule, entry in data["rules"].items():
        if entry["scaling"] == "constant":
            assert "mem_mb" in entry, f"{rule}: constant needs mem_mb"

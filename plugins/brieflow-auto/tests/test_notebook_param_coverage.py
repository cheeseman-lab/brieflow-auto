"""Coverage guard: every ALL-CAPS operator variable in a marimo notebook must be
accounted for in the interview template.

Three failure modes are asserted absent (outside an explicit benign allowlist):

1. GAP        — a var assigned INSIDE an OPERATOR PARAMETERS marker block with
                no interview row anywhere (operator can't configure it).
2. UNPATCHABLE — a var with a user/override interview row but assigned OUTSIDE
                any marker block (the patch silently misses).
3. MISSING    — a user/override interview row whose var the canonical notebook
                does not assign in any marker block (the patch is dropped).

Marker-block detection uses the REAL patcher (`scan_marker_blocks`), so the test
matches exactly what `patch_marker_block_value` can reach (case-insensitive,
suffixed headers allowed).

The detector machinery is exercised on self-contained fixtures
(tests/fixtures/notebook_coverage/) so this always runs in-repo — clean, GAP,
and UNPATCHABLE cases all asserted. The SAME check also runs against the real
canonical notebooks when they're available: point $BRIEFLOW_CANONICAL_NB_DIR at
a screen analysis dir. When it is unset or absent, a single visible skip says
so — never a silent empty collection.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_wizard.py"
TEMPLATE = Path(__file__).resolve().parent.parent / "data" / "interview_template.json"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "notebook_coverage"

_NB_ENV = os.environ.get("BRIEFLOW_CANONICAL_NB_DIR")
NB_DIR = Path(_NB_ENV) if _NB_ENV else None

NOTEBOOKS = {
    "preprocess": "0_preprocess.py",
    "sbs": "1_sbs.py",
    "phenotype": "2_phenotype.py",
    "merge": "3_merge.py",
    "classify": "4_classify.py",
    "aggregate": "5_aggregate.py",
    "cluster": "6_cluster.py",
    "analyze": "7_analyze.py",
}

ASSIGN = re.compile(r"^\s*([A-Z][A-Z0-9_]{1,})\s*=(?!=)")
TILE_SELECTORS = {"TEST_PLATE", "TEST_WELL", "TEST_TILE"}

# Operator-buckets: rows the operator is expected to set/tune (vs auto-derived).
OPERATOR_BUCKETS = {"user", "override", "user_tuned_seed"}

# Vars assigned IN a marker block that are intentionally NOT interview params.
ALLOWLIST_GAP = {
    "CONFIG_FILE_PATH",            # infra constant: "config/config.yml"
    "SBS_CHANNELS_METADATA",       # hierarchical dict — patched field-by-field
    "PHENOTYPE_CHANNELS_METADATA",
    "NONTARGETING_FORMAT",         # library standardization constants
    "NONTARGETING_PATTERNS",
    "UNIPROT_DATA_FP",             # temp file path, created+deleted in-cell
    "TEST_CELL_CLASS",             # cluster reuses the aggregate-scoped test selection
    "TEST_CHANNEL_COMBO",
    "TEST_COMPARTMENT_COMBO",      # cluster test selection when split_by_compartment is on
    "RETURN_INTERMEDIATE_OUTPUTS", # viz-only: show the secondary object threshold mask in the preview
    "TEST_PLATES",                 # classify: plate/well selection for the evaluation pass only
    "TEST_WELLS",
    "MODEL_RUN_DIR",               # classify: which past training run to inspect; None = latest
    "DATA_SAMPLE_FRACTION",        # classify: in-notebook memory caps on the labeling/eval pools
    "DATA_MAX_ROWS",
    "BATCH_SIZE",                  # classify: labeling-UI batch shape
    "OUT_OF_GATE_COUNT",
    "SCALE_BAR",                   # classify: crop overlay length, viz-only
    "RANKLINE_CLASS",              # classify: ranked-prediction review table, viz-only
    "MINIMUM_DIFFERENCE",
    "RANKLINE_MAX_ROWS",
    "CLUSTER_ID",                  # analyze: exploratory plot/montage selection, one cluster at a time
    "FEATURES_TO_ANALYZE",
    "GENES_TO_LABEL",
    "VOLCANO_FEATURE",             # analyze: volcano plot for one bootstrapped feature
    "VOLCANO_FDR_THRESHOLD",
    "VOLCANO_ZSCORE_THRESHOLD",
    "MONTAGE_FEATURE",             # analyze: cell montage of one gene in one well
    "MONTAGE_PLATE",
    "MONTAGE_WELL",
    "MONTAGE_GENE",
    "MONTAGE_NUM_CELLS",
    "MONTAGE_CELL_SIZE",
    "MONTAGE_SHAPE",
    "MONTAGE_ASCENDING",
    "MONTAGE_SUBSET_ROWS",
}

# Vars with a user/override interview row that are legitimately OUTSIDE a marker
# block because they're derived in-notebook from an operator input.
ALLOWLIST_UNPATCHABLE = {
    "MANUAL_BACKGROUND_CYCLE_INDEX",   # derived from MANUAL_BACKGROUND_CYCLE
    "SKIP_CYCLES_INDICES",             # derived from SKIP_CYCLES
}

# Operator rows with no notebook var: phase opt-in switches the interview itself reads.
ALLOWLIST_ROW_ONLY = {"RUN_CLASSIFY", "RUN_ANALYZE"}


@pytest.fixture(scope="module")
def scan_marker_blocks():
    spec = importlib.util.spec_from_file_location("brieflow_wizard", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.scan_marker_blocks


def _by_lower(template_path: Path) -> dict[str, list]:
    rows = json.loads(template_path.read_text())["params"]
    by_lower: dict[str, list] = {}
    for r in rows:
        nm = r.get("param_name")
        if nm:
            by_lower.setdefault(nm.lower(), []).append(r)
    return by_lower


@pytest.fixture(scope="module")
def interview_by_lower():
    return _by_lower(TEMPLATE)


@pytest.fixture(scope="module")
def fixture_by_lower():
    return _by_lower(FIXTURE_DIR / "interview_template.json")


def _extract(path: Path, scan_marker_blocks) -> dict[str, str]:
    blocks = scan_marker_blocks(path)

    def in_marker(n):
        return any(o <= n <= c for o, c in blocks)

    found: dict[str, str] = {}
    for i, ln in enumerate(path.read_text().splitlines(), start=1):
        m = ASSIGN.match(ln)
        if not m:
            continue
        var = m.group(1)
        if var in TILE_SELECTORS:
            continue
        inside = in_marker(i)
        if var not in found or inside:
            found[var] = "in" if inside else found.get(var, "out")
    return found


def _findings(vars_: dict[str, str], phase: str, by_lower: dict[str, list]):
    """Return (gaps, unpatchable) for one notebook's extracted vars."""
    gaps, unpatchable = [], []
    for var, where in vars_.items():
        rows = by_lower.get(var.lower(), [])
        same = [r for r in rows if r.get("notebook") == phase]
        if where == "in":
            if not rows and var not in ALLOWLIST_GAP:
                gaps.append(var)
        else:  # assigned only outside any marker block
            is_op_knob = any(r.get("bucket") in OPERATOR_BUCKETS for r in same)
            if is_op_knob and var not in ALLOWLIST_UNPATCHABLE:
                unpatchable.append(var)
    return gaps, unpatchable


# ---------------------------------------------------------------------------
# Detector machinery — always runs in-repo against self-contained fixtures.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "nb,phase,exp_gaps,exp_unpatchable",
    [
        ("clean.py", "clean", [], []),
        ("gap.py", "gap", ["UNCOVERED_KNOB"], []),
        ("unpatchable.py", "unpatchable", [], ["STRAY_KNOB"]),
    ],
)
def test_detector_on_fixture(nb, phase, exp_gaps, exp_unpatchable, scan_marker_blocks, fixture_by_lower):
    vars_ = _extract(FIXTURE_DIR / nb, scan_marker_blocks)
    gaps, unpatchable = _findings(vars_, phase, fixture_by_lower)
    assert sorted(gaps) == exp_gaps
    assert sorted(unpatchable) == exp_unpatchable


# ---------------------------------------------------------------------------
# Real canonical notebooks — collected when $BRIEFLOW_CANONICAL_NB_DIR is set and
# present. When it is unset or absent, a single VISIBLE skip says so instead of
# silently not collecting.
# ---------------------------------------------------------------------------
if NB_DIR is not None and NB_DIR.exists():
    _PRESENT = [(phase, fn) for phase, fn in NOTEBOOKS.items() if (NB_DIR / fn).exists()]
else:
    _PRESENT = [
        pytest.param(
            None,
            None,
            marks=pytest.mark.skip(
                reason=f"canonical NB dir absent: {NB_DIR}"
                if NB_DIR is not None
                else "BRIEFLOW_CANONICAL_NB_DIR unset"
            ),
            id="canonical-nb-dir-absent",
        )
    ]


@pytest.mark.parametrize("phase,fn", _PRESENT)
def test_real_notebook_param_coverage(phase, fn, scan_marker_blocks, interview_by_lower):
    vars_ = _extract(NB_DIR / fn, scan_marker_blocks)
    gaps, unpatchable = _findings(vars_, phase, interview_by_lower)
    assert not gaps, (
        f"{fn}: operator vars in a marker block with NO interview row: {sorted(gaps)}. "
        f"Add an interview row, or allowlist if genuinely internal."
    )
    assert not unpatchable, (
        f"{fn}: user/override interview rows whose notebook var is OUTSIDE a marker "
        f"block (patch will silently miss): {sorted(unpatchable)}. "
        f"Move the var into a marker block, or allowlist if derived in-notebook."
    )


@pytest.mark.parametrize("phase,fn", _PRESENT)
def test_real_notebook_has_every_operator_row(phase, fn, scan_marker_blocks):
    """Every user/override row names a marker-block var; otherwise its patch is dropped."""
    vars_ = _extract(NB_DIR / fn, scan_marker_blocks)
    rows = json.loads(TEMPLATE.read_text())["params"]
    missing = sorted(
        r["param_name"]
        for r in rows
        if r.get("notebook") == phase
        and r.get("bucket") in OPERATOR_BUCKETS
        and r["param_name"] not in ALLOWLIST_ROW_ONLY | ALLOWLIST_UNPATCHABLE
        and vars_.get(r["param_name"]) != "in"
    )
    assert not missing, (
        f"{fn}: user/override interview rows with no var in a marker block: {missing}. "
        f"The notebook predates these params, or the row names the wrong var."
    )

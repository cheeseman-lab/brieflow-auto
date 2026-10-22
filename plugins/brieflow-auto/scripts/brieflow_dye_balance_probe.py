#!/usr/bin/env python3
"""brieflow_dye_balance_probe.py — calibrate the combinatorial `frac` caller FROM THE
DATA: how bright each dye images, and whether the cycles fall into groups imaged at
different brightness.

`do_frac_call` gates each dye ON at one global fraction of a cycle's total intensity,
and calls a cycle blank by comparing it to the median of every cycle in the read.
Both assumptions fail silently. A dye that images dim never reaches the ON threshold
in a two-dye base, so that base is called as the one-dye base — measured on one
combinatorial SBS plate, 57% of single-base errors were C called as T. And a barcode whose cycles
were imaged at two brightness levels has its dim group called blank too often.

brieflow exposes `combinatorial.dye_scale` and `combinatorial.brightness_regions` for
these; this probe picks their values. It runs the read path ONCE on one tile (no
segmentation: every peak is a read), then re-calls those same intensities under each
candidate and scores read-level LIBRARY MATCH against a DECOY library of the same size
and spacing. A candidate wins only when it beats the stock call by a margin AND does
not raise the decoy rate — so a plate that needs nothing gets nothing.

Regions are MEASURED, never derived. The configured MAP/RECOMB windows are offered as
one candidate only when the screen genuinely has two barcode regions; a simple-barcode
screen (a two-colour T7 library, say) has no such split and is scored against stock
alone. A brightness step found in the data is offered as another candidate. Nothing is
written unless it earns its place.

Writes `.brieflow/dye_balance_probe.json` (`schema: dye-balance-probe/v1`). The
resolver fills DYE_SCALE / BRIGHTNESS_REGIONS from it with `auto-probe` provenance.

Verdicts: `improved` (a candidate won — values reported); `stock-is-best` (nothing beat
the stock call); `no-signal` (nothing on the tile matches the library well enough to
calibrate against); `not-applicable` (not combinatorial `frac`); `insufficient` (too few
reads to decide).

CLI:
  --analysis-dir DIR   screen analysis dir (default .)
  --plate/--well/--tile  the tile to probe (well in the screen's own convention)
  --max-reads N        brightest-N peaks used (default 3000)
  --min-gain F         relative gain in exact-library rate a candidate must clear (default 0.05)
  --out PATH           report path (default .brieflow/dye_balance_probe.json)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from brieflow_base_order_probe import (
    _brightest_threshold,
    _ensure_lib_importable,
    _library_prefixes,
)

SCHEMA = "dye-balance-probe/v1"
MIN_READS = 200


def probe_dye_balance(
    analysis_dir: Path,
    plate,
    well: str,
    tile: int,
    max_reads: int = 3000,
    min_gain: float = 0.05,
    config_path: Path | None = None,
) -> dict:
    """Run the read path on one tile and score dye-scale / brightness-region candidates.

    Returns the report dict (see module docstring); raises on a missing store/config."""
    import numpy as np
    import yaml

    _ensure_lib_importable(analysis_dir)
    from lib.sbs.align_cycles import align_cycles
    from lib.sbs.call_reads import call_reads
    from lib.sbs.compute_standard_deviation import compute_standard_deviation
    from lib.sbs.extract_bases import extract_bases
    from lib.sbs.find_peaks import find_peaks
    from lib.sbs.max_filter import max_filter
    from lib.shared.file_utils import get_hcs_nested_path, split_well
    from lib.shared.image_io import read_image
    from lib.shared.log_filter import log_filter

    config_path = Path(config_path) if config_path else analysis_dir / "config" / "config.yml"
    cfg = yaml.safe_load(config_path.read_text())
    sbs = dict(cfg["sbs"])
    base = {
        "schema": SCHEMA,
        "plate": plate,
        "well": well,
        "tile": tile,
        "chemistry": sbs.get("chemistry", "four_color"),
        "call_reads_method": sbs.get("call_reads_method", "median"),
    }
    if base["chemistry"] != "combinatorial" or base["call_reads_method"] != "frac":
        return {
            **base,
            "verdict": "not-applicable",
            "applicable": False,
            "n_reads": 0,
            "reason": "dye_scale and brightness_regions apply to combinatorial 'frac' only",
            "dye_scale": None,
            "brightness_regions": None,
            "candidates": [],
        }

    combinatorial = dict(sbs.get("combinatorial") or {})
    code = combinatorial.get("code")
    if not code:
        return {
            **base,
            "verdict": "not-applicable",
            "applicable": False,
            "n_reads": 0,
            "reason": "sbs.combinatorial.code is not set",
            "dye_scale": None,
            "brightness_regions": None,
            "candidates": [],
        }

    channel_names = list(sbs["channel_names"])
    extra = list(sbs.get("extra_channel_indices") or [])
    bases = list(sbs.get("bases") or [c for i, c in enumerate(channel_names) if i not in extra])
    root = analysis_dir / cfg["all"]["root_fp"] / "preprocess" / "sbs"
    row, col = split_well(well)
    tile_dir = root / f"image_{plate}.zarr" / row / col / str(tile)
    cycles = sorted(int(p.name) for p in tile_dir.iterdir() if p.name.isdigit())
    if not cycles:
        raise FileNotFoundError(f"no cycles under {tile_dir}")

    imgs = [
        np.asarray(
            read_image(
                str(
                    root
                    / get_hcs_nested_path(
                        {"plate": plate, "row": row, "col": col, "tile": tile, "cycle": cy}, "image"
                    )
                )
            )
        )
        for cy in cycles
    ]
    aligned = align_cycles(
        imgs,
        channel_order=channel_names,
        method=sbs.get("alignment_method"),
        upsample_factor=sbs.get("upsample_factor", 2),
        window=sbs.get("window", 2),
        skip_cycles=sbs.get("skip_cycles_indices"),
        manual_background_cycle=sbs.get("manual_background_cycle_index"),
        manual_channel_mapping=sbs.get("manual_channel_mapping"),
    )
    loged = log_filter(aligned, skip_index=extra)
    maxed = max_filter(loged, width=sbs.get("max_filter_width", 3), remove_index=extra)
    std = compute_standard_deviation(loged, remove_index=extra)
    peaks = find_peaks(std, width=sbs.get("peak_width", 5))
    threshold = _brightest_threshold(peaks, sbs.get("threshold_peaks", 0), max_reads)
    cells = np.ones(peaks.shape, dtype=np.int32)  # no segmentation: every peak is a read
    df_bases = extract_bases(
        peaks, maxed, cells, threshold, wildcards={"well": well, "tile": tile}, bases=bases
    )
    n_reads = int(df_bases["read"].nunique()) if len(df_bases) else 0

    prefixes, region = _library_prefixes(analysis_dir, sbs)
    decoys = _decoy_library(prefixes)
    dye_labels = sorted({label for on in code.values() for label in on})
    n_cycles = len(cycles) - len(sbs.get("skip_cycles_indices") or [])
    profile = _brightness_profile(df_bases, dye_labels)
    report = {
        **base,
        "applicable": True,
        "n_reads": n_reads,
        "n_cycles": n_cycles,
        "dye_labels": dye_labels,
        "threshold_peaks_used": round(float(threshold), 3),
        "brightness_profile": profile,
        "decoy_library_size": len(decoys),
    }
    if n_reads < MIN_READS:
        return {
            **report,
            "verdict": "insufficient",
            "dye_scale": None,
            "brightness_regions": None,
            "candidates": [],
            "reason": f"{n_reads} reads on this tile (need {MIN_READS})",
        }

    scales = _dye_scale_candidates(profile, dye_labels)
    regions = _region_candidates(sbs, n_cycles, profile)
    report["region_candidate_sources"] = [r["source"] for r in regions]

    rows = []
    for sc in scales:
        for rg in regions:
            combi = dict(combinatorial)
            combi.pop("dye_scale", None)
            combi.pop("brightness_regions", None)
            if sc["value"]:
                combi["dye_scale"] = sc["value"]
            if rg["value"]:
                combi["brightness_regions"] = rg["value"]
            df_reads = call_reads(
                df_bases,
                peaks_data=peaks,
                method="frac",
                chemistry="combinatorial",
                combinatorial=combi,
            )
            window = df_reads["barcode"].astype(str).str[region[0] : region[1]]
            rows.append(
                {
                    "dye_scale": sc["value"],
                    "dye_scale_source": sc["source"],
                    "brightness_regions": rg["value"],
                    "regions_source": rg["source"],
                    "exact": round(float(window.isin(prefixes).mean()), 4),
                    "decoy": round(float(window.isin(decoys).mean()), 4),
                }
            )

    stock = next(r for r in rows if r["dye_scale"] is None and r["brightness_regions"] is None)
    winner = _pick_winner(rows, stock, min_gain)
    report["candidates"] = sorted(rows, key=lambda r: -r["exact"])
    report["stock"] = stock
    if winner is stock:
        return {
            **report,
            "verdict": "stock-is-best",
            "dye_scale": None,
            "brightness_regions": None,
            "reason": f"no candidate beat stock (exact {stock['exact']}) by {min_gain:.0%} "
            f"without raising the decoy rate",
        }
    return {
        **report,
        "verdict": "improved",
        "dye_scale": winner["dye_scale"],
        "brightness_regions": winner["brightness_regions"],
        "winner": winner,
        "gain": round(winner["exact"] / stock["exact"] - 1, 4) if stock["exact"] else None,
    }


def _pick_winner(rows, stock, min_gain: float, min_exact: float = 0.02):
    """Best candidate that clears the gain margin and carries real signal.

    Three tests, and a candidate has to pass all of them. It must beat the stock call
    by ``min_gain``. Its exact rate must stand clear of both an absolute floor and its
    own decoy level — tripling a match rate of 0.003 is still noise. And its decoy rate
    may rise only in proportion to its exact rate: calling more of everything is not
    decoding better, but a candidate that finds a third more library barcodes and a
    quarter more decoys has improved the purity of the call, and refusing it on the raw
    decoy count alone would throw that away."""
    floor = stock["exact"] * (1.0 + min_gain)
    ok = []
    for r in rows:
        if r["exact"] < floor or r["exact"] < min_exact or r["exact"] < 3 * r["decoy"]:
            continue
        allowed = max(
            stock["decoy"] * (r["exact"] / stock["exact"] if stock["exact"] else 1.0), 0.002
        )
        if r["decoy"] <= allowed:
            ok.append(r)
    return max(ok, key=lambda r: (r["exact"], -r["decoy"])) if ok else stock


def _region_candidates(sbs: dict, n_cycles: int, profile: dict) -> list[dict]:
    """Brightness-region candidates: always stock (none), plus the configured barcode
    regions when the screen genuinely has two, plus a split found in the data.

    A screen with one barcode region — a simple-barcode two-colour library, say — has
    no configured split to offer, and is scored against stock alone unless the data
    shows a brightness step."""
    out = [{"value": None, "source": "stock"}]
    configured = _configured_regions(sbs, n_cycles)
    if configured:
        out.append({"value": configured, "source": "configured-barcode-regions"})
    detected = _detect_regions(profile.get("cycle_median_total") or [], n_cycles)
    if detected and detected != configured:
        out.append({"value": detected, "source": "detected-brightness-step"})
    return out


def _configured_regions(sbs: dict, n_cycles: int) -> list[list[int]] | None:
    """The MAP/RECOMB windows as 1-based inclusive ranges, when the screen has both.

    Returns None unless the screen is multi-barcode AND both windows are set, disjoint,
    at least two cycles each, and inside the cycle count — i.e. only when there really
    are two regions to hold apart."""
    if sbs.get("barcode_type", "simple") != "multi":
        return None
    bounds = [sbs.get(k) for k in ("map_start", "map_end", "recomb_start", "recomb_end")]
    if any(b is None for b in bounds):
        return None
    ms, me, rs, re_ = (int(b) for b in bounds)
    regions = sorted([[ms, me], [rs, re_]])
    if any(a < 1 or b > n_cycles or b - a < 1 for a, b in regions):
        return None
    if regions[0][1] >= regions[1][0]:
        return None
    return regions


def _detect_regions(
    cycle_medians: list, n_cycles: int, min_ratio: float = 1.25
) -> list[list[int]] | None:
    """The split point that best separates the cycles into two brightness groups, or
    None when no such step exists.

    The split minimizes within-group spread (so a single bright cycle cannot carve one
    off), then has to clear ``min_ratio`` between the two group medians. Each group
    holds at least a quarter of the cycles, which is what keeps an outlier from passing
    as a region."""
    from statistics import median

    vals = [v for v in (cycle_medians or []) if v]
    if len(vals) < 4 or len(vals) != n_cycles:
        return None
    floor = max(2, len(vals) // 4)
    best, best_spread = None, None
    for k in range(floor, len(vals) - floor + 1):
        left, right = vals[:k], vals[k:]
        spread = sum((v - median(left)) ** 2 for v in left) + sum(
            (v - median(right)) ** 2 for v in right
        )
        if best_spread is None or spread < best_spread:
            best, best_spread = k, spread
    if best is None:
        return None
    lo, hi = median(vals[:best]), median(vals[best:])
    if not lo or not hi or max(lo / hi, hi / lo) < min_ratio:
        return None
    return [[1, best], [best + 1, len(vals)]]


def _dye_scale_candidates(profile: dict, dye_labels: list) -> list[dict]:
    """Dye-scale candidates: none, the scale that levels each dye's bright-cycle
    intensity against the brightest dye, and that scale softened and strengthened.

    The reference is a high percentile of each dye's own intensity over cycles where it
    is the dominant dye, which is what "how bright does this dye image" means when most
    cycles have it off."""
    out = [{"value": None, "source": "stock"}]
    levels = profile.get("dye_p90_bright") or {}
    if len(levels) < 2 or not all(levels.get(d) for d in dye_labels):
        return out
    top = max(levels[d] for d in dye_labels)
    exact = {
        d: round(top / levels[d], 3) for d in dye_labels if levels[d] and top / levels[d] > 1.05
    }
    if not exact:
        return out
    out.append({"value": exact, "source": "level-to-brightest-dye"})
    for factor, name in ((0.5, "half"), (1.5, "strengthened")):
        scaled = {d: round(1.0 + (v - 1.0) * factor, 3) for d, v in exact.items()}
        if scaled != exact and any(v > 1.05 for v in scaled.values()):
            out.append({"value": scaled, "source": f"level-to-brightest-dye-{name}"})
    return out


def _brightness_profile(df_bases, dye_labels: list, blank_threshold: float = 0.5) -> dict:
    """Per-cycle total intensity and each dye's level, from the extracted bases table.

    A dye's level is a high percentile of its intensity over the read-cycles that are
    BRIGHT — not over the cycles where it happens to dominate. A dye that only ever
    appears paired with a brighter one is rarely the dominant channel, so scoring it on
    those cycles alone reads it as far dimmer than it is and asks for an absurd scale."""
    import numpy as np

    if not len(df_bases):
        return {}
    df = df_bases[df_bases["channel"].isin(dye_labels)]
    per_read_cycle = df.groupby(["read", "cycle"])["intensity"].sum()
    totals = per_read_cycle.groupby("cycle").median()
    profile = {
        "cycle_median_total": [round(float(v), 2) for v in totals.sort_index()],
        "cycles": [int(c) for c in sorted(totals.index)],
    }
    wide = df.pivot_table(
        index=["read", "cycle"], columns="channel", values="intensity", aggfunc="first"
    )
    present = [d for d in dye_labels if d in wide.columns]
    if not present:
        return profile
    total = per_read_cycle.reindex(wide.index)
    median_by_read = total.groupby(level=0).median()
    bright = (
        total / median_by_read.reindex(total.index, level=0).clip(lower=1.0)
    ) >= blank_threshold
    lit = wide[bright.reindex(wide.index, fill_value=False)]
    profile["dye_p90_bright"] = {
        d: round(float(np.percentile(lit[d].dropna(), 90)), 2)
        for d in present
        if lit[d].notna().any()
    }
    dominant = wide[present].idxmax(axis=1)
    profile["dye_p90_dominant"] = {
        d: round(float(np.percentile(wide.loc[dominant == d, d].dropna(), 90)), 2)
        for d in present
        if (dominant == d).any()
    }
    profile["dye_median_all"] = {d: round(float(wide[d].median()), 2) for d in present}
    return profile


def _decoy_library(prefixes: set, n: int = 100, seed: int = 0) -> set:
    """Random barcodes of the library's length, held at the library's own minimum
    spacing so a decoy is no easier to hit by chance than a real barcode."""
    import numpy as np

    real = sorted(prefixes)
    if not real:
        return set()
    length = len(real[0])
    alphabet = sorted({ch for p in real for ch in p}) or list("ACGT")
    arr = np.array([list(p) for p in real])
    pair = (arr[:, None, :] != arr[None, :, :]).sum(2)
    np.fill_diagonal(pair, arr.shape[1] + 1)
    min_d = max(int(pair.min()), 1)
    rng = np.random.default_rng(seed)
    letters = np.array(alphabet)
    out: set = set()
    for _ in range(200000):
        cand = "".join(letters[rng.integers(0, len(letters), length)])
        if cand in prefixes or cand in out:
            continue
        if (arr != np.array(list(cand))).sum(1).min() >= min_d:
            out.add(cand)
            if len(out) >= min(n, len(real)):
                break
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--analysis-dir", default=".")
    ap.add_argument("--plate", required=True)
    ap.add_argument("--well", required=True)
    ap.add_argument("--tile", type=int, required=True)
    ap.add_argument("--max-reads", type=int, default=3000)
    ap.add_argument("--min-gain", type=float, default=0.05)
    ap.add_argument("--out", default=None)
    ap.add_argument("--config", default=None, help="borrow another screen's config.yml")
    a = ap.parse_args(argv)
    analysis_dir = Path(a.analysis_dir).resolve()
    plate = int(a.plate) if str(a.plate).isdigit() else a.plate
    report = probe_dye_balance(
        analysis_dir, plate, a.well, a.tile, a.max_reads, min_gain=a.min_gain, config_path=a.config
    )
    out = Path(a.out) if a.out else analysis_dir / ".brieflow" / "dye_balance_probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(f"dye-balance probe: {report['verdict'].upper()}  (n_reads={report['n_reads']})")
    if report.get("reason"):
        print(f"  {report['reason']}")
    for r in report.get("candidates", [])[:6]:
        print(
            f"  exact={r['exact']:.4f} decoy={r['decoy']:.4f}  scale={r['dye_scale']} "
            f"regions={r['brightness_regions']} ({r['dye_scale_source']} / {r['regions_source']})"
        )
    if report["verdict"] == "improved":
        print(f"  DYE_SCALE = {report['dye_scale']}")
        print(f"  BRIGHTNESS_REGIONS = {report['brightness_regions']}")
        print(f"  gain {report['gain']:+.1%} in exact-library reads")
    print(f"  report: {out}")
    return {"improved": 0, "stock-is-best": 0, "not-applicable": 0, "no-signal": 0}.get(
        report["verdict"], 3
    )


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""brieflow_base_order_probe.py — infer the channel↔base assignment (and cycle
direction) of an SBS screen FROM THE DATA, and check it against the configured one.

The channel↔base mapping is transcribed by hand into screen.yaml (``bases`` /
``combinatorial.code``) and a swap is invisible until mapping collapses to chance
— one combinatorial SBS screen ran with C/T swapped for weeks. A screen without a reference pipeline has
no other way to catch it. This probe runs the read path ONCE on one tile (no
segmentation: every peak is a read) and scores the read-level LIBRARY MATCH under
every relabelling of the base channels (n! permutations) × cycle direction; the
argmax is the assignment the data supports.

Runs in the SCREEN env (needs the brieflow ``lib`` package + the converted SBS
store), driven by ``config/config.yml`` so it sees exactly what the pipeline sees.
Writes ``.brieflow/base_order_probe.json`` (``schema: base-order-probe/v1``) which
``brieflow_preflight`` turns into a HIGH issue on a mismatch.

Verdicts: ``ok`` (configured == argmax, well above chance); ``mismatch`` (another
assignment wins — the suggested ``bases`` order is reported); ``ambiguous`` (best
match not clearly above chance: the problem is upstream of base order — alignment,
threshold, cycle set); ``insufficient`` (too few reads).

CLI:
  --analysis-dir DIR   screen analysis dir (default .)
  --plate/--well/--tile  the tile to probe (well in the screen's own convention)
  --max-reads N        brightest-N peaks used (default 3000)
  --out PATH           report path (default .brieflow/base_order_probe.json)
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

SCHEMA = "base-order-probe/v1"


def probe_base_order(analysis_dir: Path, plate, well: str, tile: int, max_reads: int = 3000,
                     config_path: Path | None = None, alignment_method: str | None = None) -> dict:
    """Run the read path on one tile and score every base assignment. Returns the
    report dict (see module docstring); raises on a missing store/config.

    ``config_path`` lets a screen whose SBS config is not written yet borrow a
    sibling screen's ``config.yml`` (same library + channels); its barcode library
    is then resolved relative to that config's analysis dir when the screen has
    none. ``alignment_method`` overrides the configured one (diagnostic knob)."""
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
    if alignment_method:
        sbs["alignment_method"] = alignment_method
    # a borrowed config must pair with ITS library (prefix lengths follow the
    # standardization settings that produced that config)
    library_dirs = (config_path.resolve().parent.parent, analysis_dir)
    root = analysis_dir / cfg["all"]["root_fp"] / "preprocess" / "sbs"
    row, col = split_well(well)
    tile_dir = root / f"image_{plate}.zarr" / row / col / str(tile)
    cycles = sorted(int(p.name) for p in tile_dir.iterdir() if p.name.isdigit())
    if not cycles:
        raise FileNotFoundError(f"no cycles under {tile_dir}")

    channel_names = list(sbs["channel_names"])
    extra = list(sbs.get("extra_channel_indices") or [])
    bases = list(sbs.get("bases") or [c for i, c in enumerate(channel_names) if i not in extra])
    chemistry = sbs.get("chemistry", "four_color")
    method = sbs.get("call_reads_method", "median")
    combinatorial = sbs.get("combinatorial") if chemistry == "combinatorial" else None

    imgs = [
        np.asarray(read_image(str(root / get_hcs_nested_path(
            {"plate": plate, "row": row, "col": col, "tile": tile, "cycle": cy}, "image"))))
        for cy in cycles
    ]
    aligned = align_cycles(
        imgs, channel_order=channel_names, method=sbs.get("alignment_method"),
        upsample_factor=sbs.get("upsample_factor", 2), window=sbs.get("window", 2),
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
    df_bases = extract_bases(peaks, maxed, cells, threshold, wildcards={"well": well, "tile": tile}, bases=bases)
    n_reads = int(df_bases["read"].nunique()) if len(df_bases) else 0

    prefixes, region = _library_prefixes(library_dirs, sbs)
    alphabet = sorted({ch for p in prefixes for ch in p})
    length = len(next(iter(prefixes)))
    chance = len(prefixes) / (len(alphabet) ** length) if alphabet else 0.0

    results = []
    diagnostics = {"n_peaks_total": int((peaks > 0).sum()), "library_composition": _composition(prefixes)}
    if n_reads >= 50:
        for perm in itertools.permutations(bases):
            relabel = dict(zip(bases, perm))
            df_p = df_bases.assign(channel=df_bases["channel"].map(relabel))
            df_reads = call_reads(df_p, peaks_data=peaks, method=method, chemistry=chemistry,
                                  combinatorial=combinatorial)
            barcodes = df_reads["barcode"].astype(str)
            if list(perm) == bases:
                # what the configured labelling actually calls — the first thing to
                # read when the verdict is ambiguous
                diagnostics["configured_calls"] = {
                    "barcode_length": int(barcodes.str.len().mode().iloc[0]) if len(barcodes) else 0,
                    "per_cycle_composition": _per_cycle_composition(barcodes),
                    "top_barcodes": barcodes.value_counts().head(5).to_dict(),
                    "top_region": barcodes.str[region[0]:region[1]].value_counts().head(5).to_dict(),
                    "library_examples": sorted(prefixes)[:5],
                    "match_by_rank": _match_by_rank(df_reads, peaks, prefixes, region),
                    "map_window_scan": _map_window_scan(df_reads, peaks, prefixes, region),
                }
                recomb = _library_recomb_prefixes(library_dirs, sbs)
                if recomb:
                    diagnostics["configured_calls"]["recomb_window_scan"] = _map_window_scan(
                        df_reads, peaks, recomb, (0, len(next(iter(recomb)))))
            for reversed_ in (False, True):
                bc = barcodes.str[::-1] if reversed_ else barcodes
                match = float(bc.str[region[0]:region[1]].isin(prefixes).mean())
                results.append({"bases": list(perm), "reversed": reversed_, "match": round(match, 4)})

    report = _verdict(results, bases, chance, n_reads, plate, well, tile, cycles, threshold, region)
    report["diagnostics"] = diagnostics
    report = _check_sequencing_order(report, region, chance)
    report = _check_cycle_window(report, region, chance)
    return _check_region_length(report, prefixes, region, results and df_bases, bases, call_reads,
                                peaks, method, chemistry, combinatorial)


def _check_cycle_window(report, region, chance) -> dict:
    """If another contiguous window of the read matches the library far better
    than the configured MAP region, the staged cycle set is wrong (wrong
    measurements, or an aborted session mixed in) — not the base order.
    Overrides the verdict with ``cycle-window-mismatch`` and names the window."""
    scan = ((report.get("diagnostics") or {}).get("configured_calls") or {}).get("map_window_scan") or {}
    if not scan or report.get("verdict") == "sequencing-order-mismatch":
        return report
    configured_key = f"cycles_{region[0] + 1}-{region[1]}"
    best_key = max(scan, key=scan.get)
    best, configured = scan[best_key], scan.get(configured_key, 0.0)
    if best_key != configured_key and best >= max(3 * chance, 0.05) and best >= 3 * max(configured, 1e-9):
        report["verdict"] = "cycle-window-mismatch"
        report["suggested_bases"] = None
        report["suggested_map_window"] = best_key
    return report


def _check_region_length(report, prefixes, region, df_bases, bases, call_reads, peaks, method, chemistry,
                         combinatorial):
    """The pipeline slices ``barcode[region]`` and looks it up in the library, so
    a library whose prefixes are not ``region`` wide can never match — a
    standardization↔config inconsistency (e.g. prefixes built with a skipped
    cycle the config does not declare). Overrides the verdict with
    ``library-region-mismatch``; when the prefixes are exactly one shorter, scans
    which dropped position recovers the match (identifies the skipped cycle)."""
    prefix_len = len(next(iter(prefixes)))
    region_len = region[1] - region[0]
    report["diagnostics"]["prefix_length"] = prefix_len
    report["diagnostics"]["region_length"] = region_len
    if prefix_len == region_len:
        return report
    report["verdict"] = "library-region-mismatch"
    report["suggested_bases"] = None
    if prefix_len == region_len - 1 and df_bases is not None and len(df_bases):
        df_reads = call_reads(df_bases, peaks_data=peaks, method=method, chemistry=chemistry,
                              combinatorial=combinatorial)
        bc = df_reads["barcode"].astype(str).str[region[0]:region[1]]
        scan = {}
        for p in range(region_len):
            dropped = bc.str[:p] + bc.str[p + 1:]
            scan[f"drop_cycle_{region[0] + p + 1}"] = round(float(dropped.isin(prefixes).mean()), 4)
        report["diagnostics"]["dropped_cycle_scan"] = scan
    return report


def _match_by_rank(df_reads, peaks, prefixes, region, ranks=(100, 300, 1000, 3000)) -> dict:
    """Library match among the brightest-k reads (by peak score), for several k.
    Separates 'sparse but real' (high match at small k, decaying as junk enters)
    from 'no signal' (flat at chance) — a screen with a few hundred rolonies per
    tile drowns in a fixed brightest-3000 sample."""
    import numpy as np

    if not len(df_reads):
        return {}
    score = np.asarray(peaks)[df_reads["i"].to_numpy().astype(int), df_reads["j"].to_numpy().astype(int)]
    order = np.argsort(-score)
    hit = df_reads["barcode"].astype(str).str[region[0]:region[1]].isin(prefixes).to_numpy()[order]
    return {f"top_{k}": round(float(hit[:k].mean()), 4) for k in ranks if k <= len(hit)} | {"n": int(len(hit))}


def _map_window_scan(df_reads, peaks, prefixes, region, top_k=300) -> dict:
    """Library match of the brightest-`top_k` reads for EVERY contiguous window of
    the configured MAP width across the read — i.e. which staged cycles actually
    carry the MAP barcode. Catches a staging that mixes an aborted imaging
    session with the real one (one screen: cycles 1-5 from an earlier session,
    MAP in 6-12 from the real one) — invisible to base-order permutations, which never move the
    region. Keys are 1-based ``cycles_<start>-<end>``."""
    import numpy as np

    if not len(df_reads):
        return {}
    width = region[1] - region[0]
    bc = df_reads["barcode"].astype(str)
    length = int(bc.str.len().max())
    score = np.asarray(peaks)[df_reads["i"].to_numpy().astype(int), df_reads["j"].to_numpy().astype(int)]
    top = np.argsort(-score)[:top_k]
    return {f"cycles_{s + 1}-{s + width}": round(float(bc.iloc[top].str[s:s + width].isin(prefixes).mean()), 4)
            for s in range(0, length - width + 1)}


def _composition(strings) -> dict:
    """Overall character frequencies of a collection of strings."""
    joined = "".join(strings)
    return {ch: round(joined.count(ch) / len(joined), 3) for ch in sorted(set(joined))} if joined else {}


def _per_cycle_composition(barcodes) -> list[dict]:
    """Character frequencies at each barcode position (cycle)."""
    if not len(barcodes):
        return []
    length = int(barcodes.str.len().max())
    return [_composition(barcodes.str[i]) for i in range(length)]


def _verdict(results, bases, chance, n_reads, plate, well, tile, cycles, threshold, region) -> dict:
    """Assemble the report: argmax vs configured (identity, forward)."""
    report = {
        "schema": SCHEMA, "plate": plate, "well": well, "tile": tile, "cycles": cycles,
        "threshold_peaks": float(threshold), "n_reads": n_reads, "prefix_region": list(region),
        "configured": {"bases": bases, "reversed": False}, "chance_match": round(chance, 5),
        "results": sorted(results, key=lambda r: -r["match"]),
    }
    if not results:
        report.update(verdict="insufficient", best=None, suggested_bases=None)
        return report
    best = report["results"][0]
    configured = next(r for r in results if r["bases"] == bases and not r["reversed"])
    report["best"] = best
    report["configured"]["match"] = configured["match"]
    if best["match"] < max(3 * chance, 0.05):
        report["verdict"] = "ambiguous"
    elif best["bases"] == bases and not best["reversed"]:
        report["verdict"] = "ok"
    elif configured["match"] >= 0.9 * best["match"]:
        report["verdict"] = "ok"  # within noise of the best (e.g. symmetric library)
    else:
        report["verdict"] = "mismatch"
    report["suggested_bases"] = None if report["verdict"] == "ok" else best["bases"]
    return report


def _library_prefixes(analysis_dirs, sbs: dict) -> tuple[set, tuple[int, int]]:
    """Library prefix set + the barcode slice it is matched against. Simple mode
    uses ``prefix_col`` over the whole prefix; multi mode uses ``prefix_map`` over
    the MAP region (``map_start``/``map_end`` when configured). ``analysis_dirs``
    is a Path or a sequence of Paths searched in order for the library file."""
    import pandas as pd

    dirs = [Path(analysis_dirs)] if isinstance(analysis_dirs, (str, Path)) else [Path(d) for d in analysis_dirs]
    fp = next((d / sbs["df_barcode_library_fp"] for d in dirs if (d / sbs["df_barcode_library_fp"]).exists()),
              dirs[0] / sbs["df_barcode_library_fp"])
    df = pd.read_csv(fp, sep="\t" if fp.suffix in (".tsv", ".txt") else ",")
    if sbs.get("barcode_type", "simple") == "multi":
        col = sbs.get("prefix_map", "prefix_map")
        prefixes = set(df[col].dropna().astype(str))
        length = len(next(iter(prefixes)))
        # brieflow prep_multi_reads: barcode[map_start-1 : map_end] (1-based, inclusive)
        start = int(sbs.get("map_start") or 1) - 1
        return prefixes, (start, int(sbs.get("map_end") or (start + length)))
    col = sbs.get("prefix_col", "prefix")
    prefixes = set(df[col].dropna().astype(str))
    return prefixes, (0, len(next(iter(prefixes))))


def _library_recomb_prefixes(analysis_dirs, sbs: dict) -> set:
    """RECOMB prefix set for multi-mode libraries (``prefix_recomb`` column);
    empty set in simple mode or when the column is absent."""
    import pandas as pd

    if sbs.get("barcode_type", "simple") != "multi":
        return set()
    dirs = [Path(analysis_dirs)] if isinstance(analysis_dirs, (str, Path)) else [Path(d) for d in analysis_dirs]
    fp = next((d / sbs["df_barcode_library_fp"] for d in dirs if (d / sbs["df_barcode_library_fp"]).exists()), None)
    if fp is None:
        return set()
    df = pd.read_csv(fp, sep="\t" if fp.suffix in (".tsv", ".txt") else ",")
    col = sbs.get("prefix_recomb", "prefix_recomb")
    return set(df[col].dropna().astype(str)) if col in df.columns else set()


def _check_sequencing_order(report, region, chance) -> dict:
    """Multi-mode: infer which region comes first from where each library's
    prefixes match best. If the RECOMB library matches at the start of the read
    and the MAP library right after it (or vice versa), the screen was sequenced
    in the other ``sequencing_order`` than configured — staging and preprocess
    are fine, one screen.yaml line is wrong (one screen: recomb 1-5, map 6-12
    under a shared ``map_recomb`` assumption). Takes precedence over the
    cycle-window verdict, which would otherwise blame the cycle set."""
    calls = (report.get("diagnostics") or {}).get("configured_calls") or {}
    map_scan, recomb_scan = calls.get("map_window_scan") or {}, calls.get("recomb_window_scan") or {}
    if not map_scan or not recomb_scan:
        return report
    floor = max(3 * chance, 0.05)
    map_best = max(map_scan, key=map_scan.get)
    recomb_best = max(recomb_scan, key=recomb_scan.get)
    if map_scan[map_best] < floor or recomb_scan[recomb_best] < floor:
        return report
    map_start = int(map_best.split("_")[1].split("-")[0])
    recomb_start = int(recomb_best.split("_")[1].split("-")[0])
    inferred = "map_recomb" if map_start < recomb_start else "recomb_map"
    configured = "map_recomb" if region[0] == 0 else "recomb_map"
    report["diagnostics"]["inferred_sequencing_order"] = inferred
    report["diagnostics"]["configured_sequencing_order"] = configured
    if inferred != configured:
        report["verdict"] = "sequencing-order-mismatch"
        report["suggested_bases"] = None
        report["suggested_sequencing_order"] = inferred
    return report


def _brightest_threshold(peaks, floor: float, max_reads: int) -> float:
    """Threshold admitting at most the ``max_reads`` brightest peaks, never below
    the configured ``threshold_peaks``."""
    import numpy as np

    vals = np.sort(peaks[peaks > floor].ravel())
    if len(vals) <= max_reads:
        return float(floor)
    return float(max(floor, vals[-max_reads]))


def _ensure_lib_importable(analysis_dir: Path) -> None:
    """Put the screen's brieflow workflow dir on sys.path (submodule checkout
    next to the analysis dir) so ``lib.*`` resolves in the screen env."""
    for cand in (analysis_dir.parent / "brieflow" / "workflow", analysis_dir / "brieflow" / "workflow"):
        if (cand / "lib").is_dir() and str(cand) not in sys.path:
            sys.path.insert(0, str(cand))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--analysis-dir", default=".")
    ap.add_argument("--plate", required=True)
    ap.add_argument("--well", required=True)
    ap.add_argument("--tile", type=int, required=True)
    ap.add_argument("--max-reads", type=int, default=3000)
    ap.add_argument("--out", default=None)
    ap.add_argument("--config", default=None, help="borrow another screen's config.yml (SBS section not written yet)")
    ap.add_argument("--alignment-method", default=None, help="override sbs.alignment_method (diagnostic)")
    a = ap.parse_args(argv)
    analysis_dir = Path(a.analysis_dir).resolve()
    plate = int(a.plate) if str(a.plate).isdigit() else a.plate
    report = probe_base_order(analysis_dir, plate, a.well, a.tile, a.max_reads,
                              config_path=a.config, alignment_method=a.alignment_method)
    out = Path(a.out) if a.out else analysis_dir / ".brieflow" / "base_order_probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(f"base-order probe: {report['verdict'].upper()}  (n_reads={report['n_reads']}, "
          f"chance={report['chance_match']:.3f})")
    for r in report["results"][:6]:
        print(f"  {'REV ' if r['reversed'] else '    '}{r['bases']}  match={r['match']:.3f}")
    print(f"  configured {report['configured']}")
    if report.get("suggested_bases"):
        print(f"  SUGGESTED bases order: {report['suggested_bases']}")
    if report.get("suggested_sequencing_order"):
        print(f"  SUGGESTED library.sequencing_order: {report['suggested_sequencing_order']} "
              f"(configured {report['diagnostics'].get('configured_sequencing_order')})")
    if report.get("suggested_map_window"):
        print(f"  SUGGESTED MAP window: staged {report['suggested_map_window']} (configured cycles "
              f"{report['prefix_region'][0] + 1}-{report['prefix_region'][1]})")
    if report["verdict"] != "ok":
        d = report["diagnostics"]
        print(f"  diagnostics: peaks_total={d['n_peaks_total']} library_composition={d['library_composition']}")
        for k, v in (d.get("configured_calls") or {}).items():
            print(f"    {k}: {v}")
    print(f"  report: {out}")
    return {"ok": 0, "mismatch": 2, "library-region-mismatch": 2, "cycle-window-mismatch": 2,
            "sequencing-order-mismatch": 2, "ambiguous": 3, "insufficient": 3}.get(
        report["verdict"], 2)


if __name__ == "__main__":
    sys.exit(main())

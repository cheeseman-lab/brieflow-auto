#!/usr/bin/env python3
"""brieflow_base_composition.py — called-base composition and all-on rate of a
combinatorial (`frac`) SBS run, read from its outputs.

The `frac` caller matches each cycle's dye state to the nearest base in
`sbs.combinatorial.code`. A state equally near two bases is a tie, and with no
`combinatorial.ambiguous_call` the tie goes to the first base in code order. On a
three-dye code with G=[], T=[568], C=[488,568], A=[488,647], a cycle with all three
dyes on is one mismatch from both C and A, so every such cycle is called C, and the
plate reads C-rich with no error raised. With `ambiguous_call: N` the same cycles
become N.

This probe reports what that looks like on a finished run:

* per-cycle shares of each called base (and N) over a sample of `reads.parquet`
  (or `reads.tsv`) files, against the share the barcode library expects at that
  cycle; `base_excess_max` is the largest observed-minus-expected share;
* the all-on rate — bright cycles with every code dye on — from a sample of
  per-tile `bases.tsv`, using the caller's own blank and fraction rules;
* whether `ambiguous_call: N` is configured but no N reaches the reads (the
  installed brieflow does not honour the key).

Reads with no signal (every cycle G or N) are left out of the shares: they are
dim spots, not calls.

CLI:
  --analysis-dir DIR   screen analysis dir (default .)
  --max-files N        reads / bases files sampled each (default 12)
  --json               print the full report as JSON
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

SCHEMA = "base-composition/v1"
MIN_READS = 1000


def _even_sample(paths: list[Path], n: int) -> list[Path]:
    if len(paths) <= n:
        return paths
    idx = np.linspace(0, len(paths) - 1, n).round().astype(int)
    return [paths[i] for i in sorted(set(idx))]


def _dyes(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _windows(sbs: dict) -> list[tuple[str, int, int]]:
    """(library column, 0-based start, end exclusive) per barcode region."""
    if sbs.get("barcode_type", "simple") == "multi":
        out = []
        if sbs.get("map_start") and sbs.get("map_end"):
            out.append((sbs.get("prefix_map", "prefix_map"), int(sbs["map_start"]) - 1,
                        int(sbs["map_end"])))
        if sbs.get("recomb_start") and sbs.get("recomb_end"):
            out.append((sbs.get("prefix_recomb", "prefix_recomb"), int(sbs["recomb_start"]) - 1,
                        int(sbs["recomb_end"])))
        return out
    return [(sbs.get("barcode_col", "prefix"), 0, None)]


def expected_shares(library: pd.DataFrame, sbs: dict, bases: list[str]) -> dict[int, dict]:
    """{barcode position: {base: share}} the library implies, per configured region."""
    out: dict[int, dict] = {}
    for col, start, end in _windows(sbs):
        if col not in library.columns:
            continue
        seqs = library[col].dropna().astype(str).drop_duplicates()
        if seqs.empty:
            continue
        width = seqs.str.len().min() if end is None else end - start
        for k in range(width):
            counts = seqs.str[k].value_counts(normalize=True)
            out[start + k] = {b: float(counts.get(b, 0.0)) for b in bases}
    return out


def observed_shares(barcodes: pd.Series, bases: list[str],
                    blank: str | None = None) -> tuple[dict[int, dict], int]:
    """{position: {base or N: share}} over reads with signal in at least one cycle (any
    call other than the blank base or N)."""
    bc = barcodes.dropna().astype(str)
    signal = bc[~bc.str.fullmatch(f"[{blank or ''}N]*")]
    if signal.empty:
        return {}, 0
    width = int(signal.str.len().mode().iloc[0])
    signal = signal[signal.str.len() == width]
    chars = np.array([list(s) for s in signal])
    out = {}
    for k in range(width):
        col = chars[:, k]
        out[k] = {b: float((col == b).mean()) for b in [*bases, "N"]}
    return out, len(signal)


def all_on_rate(df_bases: pd.DataFrame, combinatorial: dict) -> tuple[float | None, int]:
    """Share of bright read-cycles with every code dye on, by the `frac` caller's rules
    (dye_scale, blank_threshold vs the read's median brightness or per-region median,
    fraction_threshold)."""
    code = combinatorial.get("code") or {}
    dye_labels = sorted({d for v in code.values() for d in _dyes(v)})
    if not dye_labels or df_bases.empty:
        return None, 0
    df = df_bases[df_bases["channel"].isin(dye_labels)]
    wide = df.pivot_table(index=["tile", "read", "cycle"], columns="channel",
                          values="intensity", aggfunc="first")
    wide = wide.reindex(columns=dye_labels).fillna(0.0)
    scale = combinatorial.get("dye_scale") or {}
    dye = wide.to_numpy(float) * np.array([float(scale.get(lab, 1.0)) for lab in dye_labels])
    total = dye.sum(axis=1)
    keys = wide.index.to_frame(index=False)
    keys["total"] = total
    regions = combinatorial.get("brightness_regions")
    # regions index the caller's cycle axis (1-based rank of the cycle), not cycle labels
    position = keys["cycle"].map({c: i + 1 for i, c in enumerate(sorted(keys["cycle"].unique()))})
    keys["region"] = -1
    for r, (lo, hi) in enumerate(regions or []):
        keys.loc[position.between(int(lo), int(hi)), "region"] = r
    ref = keys.groupby(["tile", "read", "region"])["total"].transform("median")
    if regions:
        glob = keys.groupby(["tile", "read"])["total"].transform("median")
        ref = ref.where(keys["region"] >= 0, glob)
    rel = total / np.clip(ref.to_numpy(float), 1.0, None)
    frac = dye / np.clip(total, 1.0, None)[:, None]
    max_dyes = max((len(_dyes(v)) for v in code.values()), default=1)
    thr = combinatorial.get("fraction_threshold")
    thr = float(thr) if thr is not None else 0.36 / max(max_dyes, 1)
    bright = rel >= float(combinatorial.get("blank_threshold", 0.5))
    on = bright[:, None] & (frac >= thr)
    n_bright = int(bright.sum())
    if not n_bright:
        return None, 0
    return float(on.all(axis=1).sum() / n_bright), n_bright


def _read_barcodes(path: Path) -> pd.Series:
    if path.suffix == ".parquet":
        return pd.read_parquet(path, columns=["barcode"])["barcode"]
    return pd.read_csv(path, sep="\t", usecols=["barcode"])["barcode"]


def base_composition(analysis_dir: Path, config: dict | None = None, max_files: int = 12) -> dict:
    """The report dict (schema base-composition/v1). `verdict` is `not-applicable` for a
    run that is not combinatorial `frac`, `insufficient` when too few reads were found."""
    analysis_dir = Path(analysis_dir)
    if config is None:
        fp = analysis_dir / "config" / "config.yml"
        config = yaml.safe_load(fp.read_text()) if fp.exists() else {}
    sbs = (config or {}).get("sbs") or {}
    comb = sbs.get("combinatorial") or {}
    report = {"schema": SCHEMA, "ambiguous_call": comb.get("ambiguous_call")}
    if sbs.get("call_reads_method") != "frac" or not comb.get("code"):
        return {**report, "verdict": "not-applicable"}
    bases = list(comb["code"])
    blank = next((b for b, v in comb["code"].items() if not _dyes(v)), None)
    sbs_root = analysis_dir / "brieflow_output" / "sbs"

    reads_files = sorted((sbs_root / "parquets").rglob("reads.parquet"))
    if not reads_files:
        reads_files = sorted((sbs_root / "tsvs").rglob("reads.tsv"))
    reads_files = _even_sample(reads_files, max_files)
    barcodes = (pd.concat([_read_barcodes(f) for f in reads_files], ignore_index=True)
                if reads_files else pd.Series(dtype=str))
    observed, n_reads = observed_shares(barcodes, bases, blank)
    report.update(n_reads=n_reads, reads_files=[str(f) for f in reads_files])

    expected = {}
    lib_fp = sbs.get("df_barcode_library_fp")
    if lib_fp and (analysis_dir / lib_fp).exists():
        lib = pd.read_csv(analysis_dir / lib_fp, sep="\t" if str(lib_fp).endswith((".tsv", ".txt")) else ",")
        expected = expected_shares(lib, sbs, bases)

    cycles = []
    for k, obs in observed.items():
        called = 1.0 - obs["N"]
        row = {"position": k + 1, "observed": obs}
        if k in expected and called > 0:
            # shares among called (non-N) cycles, so an N fix does not read as a deficit
            row["excess"] = {b: obs[b] / called - expected[k][b] for b in bases}
        cycles.append(row)
    excess = [(v, b, r["position"]) for r in cycles for b, v in r.get("excess", {}).items()]
    top = max(excess, default=None)
    report["cycles"] = cycles
    report["base_excess_max"] = top[0] if top else None
    report["base_excess_where"] = {"base": top[1], "position": top[2]} if top else None
    n_share = float(np.mean([r["observed"]["N"] for r in cycles])) if cycles else None
    report["n_call_rate"] = n_share
    report["base_shares"] = ({b: float(np.mean([r["observed"][b] for r in cycles])) for b in bases}
                             if cycles else {})

    bases_files = _even_sample(sorted((sbs_root / "tsvs").rglob("bases.tsv")), max_files)
    if bases_files:
        df_b = pd.concat([pd.read_csv(f, sep="\t", usecols=["tile", "read", "cycle", "channel", "intensity"])
                          .assign(tile=i) for i, f in enumerate(bases_files)], ignore_index=True)
        rate, n_bright = all_on_rate(df_b, comb)
    else:
        rate, n_bright = None, 0
    report.update(all_on_rate=rate, n_bright_cycles=n_bright, bases_files=[str(f) for f in bases_files])
    report["all_on_unresolved_rate"] = (
        None if rate is None else (0.0 if comb.get("ambiguous_call") == "N" else rate))
    report["ambiguous_n_inert"] = (
        1.0 if comb.get("ambiguous_call") == "N" and n_reads >= MIN_READS and n_share == 0 else 0.0)
    report["verdict"] = "insufficient" if n_reads < MIN_READS else "measured"
    return report


METRICS = ("base_excess_max", "n_call_rate", "all_on_rate", "all_on_unresolved_rate",
           "ambiguous_n_inert")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--analysis-dir", type=Path, default=Path("."))
    ap.add_argument("--max-files", type=int, default=12)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    rep = base_composition(args.analysis_dir, max_files=args.max_files)
    if args.json:
        print(json.dumps(rep, indent=2))
        return 0
    print(f"verdict {rep['verdict']}  ambiguous_call={rep.get('ambiguous_call')}  reads={rep.get('n_reads')}")
    for r in rep.get("cycles", []):
        obs = " ".join(f"{b}={v:.2f}" for b, v in r["observed"].items())
        ex = " ".join(f"{b}{v:+.2f}" for b, v in r.get("excess", {}).items())
        print(f"  pos {r['position']:>2}  {obs}   vs library {ex}")
    for m in METRICS:
        print(f"{m}: {rep.get(m)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

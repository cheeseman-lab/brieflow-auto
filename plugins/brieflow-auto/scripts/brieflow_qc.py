#!/usr/bin/env python3
"""brieflow_qc.py — post-phase QC consolidation.

Reads the per-phase eval_* outputs that brieflow already writes to
brieflow_output/<phase>/eval/, extracts headline metrics, applies lab-wide
thresholds (data/qc_gates.yaml), and produces a normalized QCSummary the
operator reviews after each phase and the runner gates on.

All rate metrics are normalized to 0-1 (brieflow `*__percent` columns / 100).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict
from html import escape as _he
from pathlib import Path

import pandas as pd
import yaml

from brieflow_phases import NOTEBOOK_ONLY_PHASES, PHASES

#: status precedence, worst-first
_STATUS_RANK = {"fail": 3, "warn": 2, "pass": 1, "info": 0}


@dataclass
class Metric:
    name: str
    value: float | None
    unit: str = ""            # rate | count | px | percent
    level: str = "phase"      # phase | plate | well | tile
    threshold: dict | None = None
    status: str = "info"      # pass | warn | fail | info
    source_file: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class QCSummary:
    phase: str
    metrics: list[Metric]
    overall_status: str
    generated_at: str
    provenance: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "overall_status": self.overall_status,
            "generated_at": self.generated_at,
            "provenance": list(self.provenance),
            "metrics": [m.to_dict() for m in self.metrics],
        }


def apply_threshold(value: float | None, gate: dict | None) -> str:
    """Return pass|warn|fail|info for a value against a gate.

    Gate forms (monotone):
      {"min": x}            fail if value < x, else pass
      {"min": x,"warn": w}  (w >= x) fail if < x, warn if < w, else pass
      {"max": y}            fail if value > y, else pass
      {"max": y,"warn": w}  (w <= y) fail if > y, warn if > w, else pass
    Missing value or missing gate -> "info" (shown, not gated).
    """
    if value is None or not gate or (isinstance(value, float) and math.isnan(value)):
        return "info"
    if "min" in gate:
        if value < gate["min"]:
            return "fail"
        if "warn" in gate and value < gate["warn"]:
            return "warn"
        return "pass"
    if "max" in gate:
        if value > gate["max"]:
            return "fail"
        if "warn" in gate and value > gate["warn"]:
            return "warn"
        return "pass"
    return "info"


def edge_keep(cell_diameter_px: float, tile_px: float) -> float | None:
    """Share of cells expected to survive tile-edge removal: ((T - d) / T)^2."""
    if not cell_diameter_px or not tile_px or cell_diameter_px >= tile_px:
        return None
    return ((tile_px - cell_diameter_px) / tile_px) ** 2


def edge_scaled_gate(
    gate: dict | None, cell_diameter_px, tile_px, ref: dict | None
) -> dict | None:
    """Scale a retention gate's min/warn by keep(screen) / keep(reference).

    Without a reference, a gate, or the screen's diameter and tile size, the gate is
    returned unchanged."""
    if not ref or not gate:
        return gate
    keep = edge_keep(cell_diameter_px, tile_px)
    keep_ref = edge_keep(ref.get("cell_diameter_px"), ref.get("tile_px"))
    if keep is None or keep_ref is None:
        return gate
    factor = min(1.0, keep / keep_ref)
    scaled = {k: round(v * factor, 4) for k, v in gate.items() if k in ("min", "warn")}
    scaled["edge_scaled"] = {
        "cell_diameter_px": round(float(cell_diameter_px), 1),
        "tile_px": int(tile_px),
        "factor": round(factor, 4),
        "unscaled": {k: gate[k] for k in ("min", "warn") if k in gate},
    }
    return scaled


def _edge_geometry(config: dict | None) -> tuple[float | None, float | None]:
    """Measured phenotype cell diameter and tile size from config.yml."""
    config = config or {}
    diameter = (config.get("phenotype") or {}).get("cell_diameter")
    dims = (config.get("merge") or {}).get("phenotype_dimensions") or []
    return diameter, (min(dims) if dims else None)


def overall_status(statuses: list[str]) -> str:
    """Worst status wins; 'info' only if nothing else is present."""
    ranked = [s for s in statuses if s in _STATUS_RANK]
    if not ranked:
        return "info"
    return max(ranked, key=lambda s: _STATUS_RANK[s])


_DEFAULT_GATES_PATH = Path(__file__).resolve().parent.parent / "data" / "qc_gates.yaml"


def _read_config(path: Path) -> dict:
    try:
        return yaml.safe_load(Path(path).read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}


def load_qc_gates(path: str | Path | None = None) -> dict:
    """Load lab-wide QC thresholds. Defaults to data/qc_gates.yaml."""
    p = Path(path) if path else _DEFAULT_GATES_PATH
    with open(p) as f:
        return yaml.safe_load(f) or {}


def phase_gates(gates: dict, phase: str) -> dict:
    """Return the metric->gate mapping for one phase ({} if absent).

    Supports both the new nested format (phase has a 'phase_qc' sub-key)
    and the legacy flat format (phase maps directly to metric->gate dicts).
    """
    entry = gates.get(phase, {}) or {}
    if "phase_qc" in entry:
        return entry["phase_qc"] or {}
    return entry


def sweep_gates(gates: dict, phase: str) -> dict:
    """Return the sweep metric->gate mapping for one phase ({} if absent).

    Returns the 'sweep_qc' sub-dict from the nested format, or {} for legacy
    flat files (which have no sweep_qc section).
    """
    entry = gates.get(phase, {}) or {}
    return entry.get("sweep_qc", {}) or {}


def _eval_files(phase_root: Path, eval_type: str, basename: str) -> list[Path]:
    """All eval files matching *<basename> under <phase>/eval/<eval_type>/.

    Tolerates both layouts found across real screens:
    - subdir layout:  eval/<type>/<plate>/<basename>   (e.g. brieflow-speed)
    - prefix layout:  eval/<type>/<plate>__<basename>  (e.g. damavand, multi-plate)
    rglob finds both without any per-layout branching.
    """
    base = phase_root / "eval" / eval_type
    return sorted(base.rglob(f"*{basename}")) if base.exists() else []


def extract_sbs_qc(phase_root: Path, config: dict | None = None) -> list[Metric]:
    """phase_root = brieflow_output/sbs. Reads mapping_overview.tsv (count-
    weighted single/any map rate, total cells) + segmentation_overview.tsv, and for a
    combinatorial `frac` run the called-base composition and all-on rate
    (brieflow_base_composition)."""
    metrics: list[Metric] = []
    mp_files = _eval_files(phase_root, "mapping", "mapping_overview.tsv")
    if mp_files:
        map_df = pd.concat([pd.read_csv(f, sep="\t") for f in mp_files], ignore_index=True)
        total = float(map_df["total_cells__count"].sum())
        # Gate on BARCODE-level mapping: it measures "did sequencing work". The
        # gene-level columns lose 55-68% of barcode-mapped cells and report
        # impossible >100% per-well rates on some plates (2026-08-28: one plate
        # 0.582 barcode vs 0.164 gene; 8 wells >100% on another), so gating on them
        # fails good screens. Gene-level stays as an ungated metric.
        single = float(map_df["1_barcode_cells__count"].sum())
        anyb = float(map_df["1_or_more_barcodes__count"].sum())
        src = str(mp_files[0])
        metrics += [
            Metric("single_map_rate", (single / total) if total else None, "rate",
                   "phase", source_file=src),
            Metric("any_map_rate", (anyb / total) if total else None, "rate",
                   "phase", source_file=src),
            Metric("total_cells", total, "count", "phase", source_file=src),
        ]
        if "1_gene_cells__count" in map_df.columns:
            gene = float(map_df["1_gene_cells__count"].sum())
            anyg = float(map_df["1_or_more_genes__count"].sum())
            metrics += [
                Metric("single_gene_map_rate", (gene / total) if total else None,
                       "rate", "phase", source_file=src),
                Metric("any_gene_map_rate", (anyg / total) if total else None,
                       "rate", "phase", source_file=src),
            ]
    seg_files = _eval_files(phase_root, "segmentation", "segmentation_overview.tsv")
    if seg_files:
        seg_df = pd.concat([pd.read_csv(f, sep="\t") for f in seg_files], ignore_index=True)
        cols = set(seg_df.columns)
        src = str(seg_files[0])
        if "initial_cells" in cols and "final_cells" in cols:
            init_cells = float(seg_df["initial_cells"].sum())
            final_cells = float(seg_df["final_cells"].sum())
            metrics.append(
                Metric("cell_retention", (final_cells / init_cells) if init_cells else None,
                       "rate", "phase", source_file=src)
            )
        if "initial_nuclei" in cols and "final_nuclei" in cols:
            init_nuclei = float(seg_df["initial_nuclei"].sum())
            final_nuclei = float(seg_df["final_nuclei"].sum())
            metrics.append(
                Metric("nuclei_retention", (final_nuclei / init_nuclei) if init_nuclei else None,
                       "rate", "phase", source_file=src)
            )
    metrics += _base_composition_metrics(phase_root, config)
    return metrics


def _base_composition_metrics(phase_root: Path, config: dict | None) -> list[Metric]:
    """Composition metrics for a combinatorial `frac` run; none for any other chemistry
    or when the run's config.yml is not beside brieflow_output/."""
    from brieflow_base_composition import METRICS, base_composition

    analysis_dir = phase_root.parent.parent
    if config is None:
        fp = analysis_dir / "config" / "config.yml"
        if not fp.exists():
            return []
        config = _read_config(fp)
    rep = base_composition(analysis_dir, config)
    if rep["verdict"] != "measured":
        return []
    src = (rep.get("reads_files") or [""])[0]
    units = {"ambiguous_n_inert": "flag"}
    return [Metric(m, rep.get(m), units.get(m, "rate"), "phase", source_file=src)
            for m in METRICS]


def _weighted_mean(df, value_col: str, weight_col: str) -> float | None:
    w = df[weight_col].astype(float)
    v = df[value_col].astype(float)
    tot = float(w.sum())
    return float((v * w).sum() / tot) if tot else None


def extract_merge_qc(phase_root: Path) -> list[Metric]:
    """phase_root = brieflow_output/merge. Cell-weighted recovery + single-gene
    rates from the plate-level merge_summary.tsv files (one per plate).

    Returns [] when no *merge_summary.tsv files exist (old per-well
    *final_sbs_matching_rates.tsv / *final_phenotype_matching_rates.tsv are
    deprecated and no longer parsed).
    """
    base = phase_root / "eval"
    summary_files = sorted(base.glob("*merge_summary.tsv"))
    if not summary_files:
        return []
    df = pd.concat([pd.read_csv(f, sep="\t") for f in summary_files], ignore_index=True)
    src = str(summary_files[0])
    cols = set(df.columns)
    metrics: list[Metric] = []
    if "ph_recovery_rate" in cols and "ph_cells" in cols:
        metrics.append(Metric("ph_recovery_rate", _weighted_mean(df, "ph_recovery_rate", "ph_cells"),
                              "rate", "phase", source_file=src))
    if "sbs_recovery_rate" in cols and "sbs_cells" in cols:
        metrics.append(Metric("sbs_recovery_rate", _weighted_mean(df, "sbs_recovery_rate", "sbs_cells"),
                              "rate", "phase", source_file=src))
    if {"unique_sbs_in_merge", "ph_cells", "sbs_cells"} <= cols:
        # Partial phenotype coverage caps SBS recovery at ~ph_cells/sbs_cells; gate the
        # matched cells against the smaller side, which both imagings could have seen.
        reachable = float(df[["ph_cells", "sbs_cells"]].astype(float).min(axis=1).sum())
        matched = float(df["unique_sbs_in_merge"].astype(float).sum())
        metrics.append(Metric("recovery_of_reachable", matched / reachable if reachable else None,
                              "rate", "phase", source_file=src))
    if "single_gene_rate" in cols and "total_match_pairs" in cols:
        metrics.append(Metric("single_gene_rate", _weighted_mean(df, "single_gene_rate", "total_match_pairs"),
                              "rate", "phase", source_file=src))
    return metrics


def extract_phenotype_qc(phase_root: Path) -> list[Metric]:
    """phase_root = brieflow_output/phenotype. Cell/nuclei retention + final count
    from segmentation_overview.tsv. (segment_cells=False screens still have
    final_cells == final_nuclei, so retention is meaningful either way.)"""
    metrics: list[Metric] = []
    seg_files = _eval_files(phase_root, "segmentation", "segmentation_overview.tsv")
    if seg_files:
        seg_df = pd.concat([pd.read_csv(f, sep="\t") for f in seg_files], ignore_index=True)
        cols = set(seg_df.columns)
        src = str(seg_files[0])
        if "initial_cells" in cols and "final_cells" in cols:
            init_cells = float(seg_df["initial_cells"].sum())
            final_cells = float(seg_df["final_cells"].sum())
            metrics.append(
                Metric("cell_retention", (final_cells / init_cells) if init_cells else None,
                       "rate", "phase", source_file=src)
            )
            metrics.append(
                Metric("final_cells", final_cells, "count", "phase", source_file=src)
            )
        if "initial_nuclei" in cols and "final_nuclei" in cols:
            init_nuclei = float(seg_df["initial_nuclei"].sum())
            final_nuclei = float(seg_df["final_nuclei"].sum())
            metrics.append(
                Metric("nuclei_retention", (final_nuclei / init_nuclei) if init_nuclei else None,
                       "rate", "phase", source_file=src)
            )
    return metrics


def extract_aggregate_qc(phase_root: Path) -> list[Metric]:
    """phase_root = brieflow_output/aggregate. Max/mean missingness across all
    na_stats.tsv files (one per cell_class x channel_combo)."""
    base = phase_root / "eval"
    files = sorted(base.glob("*na_stats.tsv"))
    if not files:
        return []
    df = pd.concat([pd.read_csv(f, sep="\t") for f in files], ignore_index=True)
    src = str(files[0])
    if "percent_na" not in df.columns:
        return []
    vals = df["percent_na"].astype(float)
    mx, mn = vals.max(), vals.mean()
    return [
        Metric("max_percent_na", None if pd.isna(mx) else float(mx), "rate", "phase", source_file=src),
        Metric("mean_percent_na", None if pd.isna(mn) else float(mn), "rate", "phase", source_file=src),
    ]


def extract_cluster_qc(
    phase_root: Path,
    target_genes_per_cluster: int = 20,
) -> list[Metric]:
    """phase_root = brieflow_output/cluster. Reads enrichment JSONs.

    Globs phase_root/*/*/*/CB-Real__global_metrics.json
    (path = <combo>/<cell_class>/<res>/) and the sibling
    CB-Real__integrated_results.json. Returns [] if no files found.
    """
    global_files = sorted(phase_root.glob("*/*/*/CB-Real__global_metrics.json"))
    if not global_files:
        return []

    best_recall: float | None = None
    best_src = str(global_files[0])
    target_recall: float | None = None
    target_gpk: float | None = None
    target_dist = float("inf")
    target_src: str | None = None

    for gf in global_files:
        try:
            gdata = json.loads(gf.read_text())
            recall = float(gdata["STRING"]["recall"])
        except Exception:
            continue

        if best_recall is None or recall > best_recall:
            best_recall = recall
            best_src = str(gf)

        sib = gf.parent / "CB-Real__integrated_results.json"
        try:
            sdata = json.loads(sib.read_text())
            n_clusters = int(sdata["total_clusters_analyzed"])
            total_genes = sum(int(c["size"]) for c in sdata["clusters"].values())
            gpk = total_genes / n_clusters if n_clusters else None
        except Exception:
            gpk = None

        if gpk is not None:
            dist = abs(gpk - target_genes_per_cluster)
            if dist < target_dist:
                target_dist = dist
                target_recall = recall
                target_gpk = gpk
                target_src = str(gf)

    src = target_src or best_src
    metrics = [
        Metric("best_recall", best_recall, "rate", "phase", source_file=best_src),
        Metric("recall_at_target_granularity", target_recall, "rate", "phase",
               source_file=src),
        Metric("genes_per_cluster_at_target", target_gpk, "count", "phase",
               source_file=src),
    ]
    return metrics


# ---------------------------------------------------------------------------
# Task 9: orchestrator
# ---------------------------------------------------------------------------

#: phase -> extractor. preprocess has no eval/*.tsv output; returns empty info.
_EXTRACTORS = {
    "sbs": extract_sbs_qc,
    "phenotype": extract_phenotype_qc,
    "merge": extract_merge_qc,
    "aggregate": extract_aggregate_qc,
    "cluster": extract_cluster_qc,
}


def build_phase_qc(
    brieflow_output: str | Path,
    phase: str,
    generated_at: str,
    gates: dict | None = None,
    lab_defaults: dict | None = None,
    config: dict | None = None,
) -> QCSummary:
    """Extract + threshold + persist QC for one phase.

    brieflow_output: the run's brieflow_output/ dir. phase: one of PHASES.
    generated_at: ISO timestamp (caller-supplied; module never calls the clock).
    Writes brieflow_output/<phase>/eval/qc_summary.json and returns the summary.
    """
    brieflow_output = Path(brieflow_output)
    phase_root = brieflow_output / phase
    extractor = _EXTRACTORS.get(phase)
    cluster_defaults: dict = {}
    if extractor is None:
        metrics = []
    elif phase == "cluster":
        tgpc = 20
        tol = 5
        if lab_defaults is not None:
            cd = lab_defaults.get("cluster_selection") or {}
            tgpc = int(cd.get("target_genes_per_cluster", 20))
            tol = int(cd.get("genes_per_cluster_tolerance", 5))
        cluster_defaults = {"target_genes_per_cluster": tgpc, "genes_per_cluster_tolerance": tol}
        metrics = extractor(phase_root, target_genes_per_cluster=tgpc)
    elif phase == "sbs":
        metrics = extractor(phase_root, config=config)
    else:
        metrics = extractor(phase_root)

    gates = gates if gates is not None else load_qc_gates()
    pgates = phase_gates(gates, phase)
    for m in metrics:
        if phase == "cluster" and m.name == "genes_per_cluster_at_target":
            # warn-band: pass if within [target - tol, target + tol], warn otherwise
            tgpc = cluster_defaults.get("target_genes_per_cluster", 20)
            tol = cluster_defaults.get("genes_per_cluster_tolerance", 5)
            if m.value is None:
                m.status = "info"
            elif (tgpc - tol) <= m.value <= (tgpc + tol):
                m.status = "pass"
            else:
                m.status = "warn"
            m.threshold = {"warn_band": [tgpc - tol, tgpc + tol]}
            continue
        g = pgates.get(m.name)
        edge_ref = (gates.get(phase) or {}).get("edge_reference") or {}
        if g and m.name in (edge_ref.get("metrics") or []):
            if config is None:
                config = _read_config(brieflow_output.parent / "config" / "config.yml")
            g = edge_scaled_gate(g, *_edge_geometry(config), edge_ref)
        m.threshold = g
        m.status = apply_threshold(m.value, g)

    summary = QCSummary(
        phase=phase,
        metrics=metrics,
        overall_status=overall_status([m.status for m in metrics]),
        generated_at=generated_at,
        provenance=sorted({m.source_file for m in metrics if m.source_file}),
    )

    eval_dir = phase_root / "eval"
    eval_dir.mkdir(parents=True, exist_ok=True)
    (eval_dir / "qc_summary.json").write_text(json.dumps(summary.to_dict(), indent=2))
    return summary


# ---------------------------------------------------------------------------
# Task 10: gate verdict for the runner
# ---------------------------------------------------------------------------

def qc_gate_verdict(summary: QCSummary, warn_is_fail: bool = False) -> dict:
    """Reduce a QCSummary to an advance/escalate decision for the runner.

    pass=True advances. By default only 'fail' blocks; set warn_is_fail to make
    'warn' block too. failures lists the offending metric names.
    """
    blocking = {"fail", "warn"} if warn_is_fail else {"fail"}
    failures = [m.name for m in summary.metrics if m.status in blocking]
    ok = not failures
    if ok:
        rationale = f"{summary.phase}: overall={summary.overall_status}; no blocking metrics"
    else:
        _failset = set(failures)
        detail = ", ".join(
            f"{m.name}={m.value} ({m.status}, gate={m.threshold})"
            for m in summary.metrics if m.name in _failset
        )
        rationale = f"{summary.phase}: blocking metrics -> {detail}"
    return {"pass": ok, "failures": failures, "rationale": rationale}


# ---------------------------------------------------------------------------
# Task 11: operator-facing reports
# ---------------------------------------------------------------------------

_STATUS_GLYPH = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "info": "info"}


def render_qc_text(summary: QCSummary) -> str:
    """Plain-text table the runloop prints at phase end."""
    lines = [
        f"=== QC: {summary.phase}  [{_STATUS_GLYPH.get(summary.overall_status, summary.overall_status)}] "
        f"({summary.generated_at}) ===",
        f"{'metric':<22}{'value':>12}  {'status':<6} gate",
    ]
    for m in summary.metrics:
        val = "n/a" if m.value is None else f"{m.value:.4g}"
        lines.append(
            f"{m.name:<22}{val:>12}  {_STATUS_GLYPH.get(m.status, m.status):<6} "
            f"{m.threshold or ''}"
        )
    if not summary.metrics:
        lines.append("(no metrics — extractor not available for this phase)")
    return "\n".join(lines)


def write_qc_report_html(summary: QCSummary, out_path: str | Path) -> Path:
    """Standalone per-phase HTML report (post-run; distinct from the tuning
    notebook HTML)."""
    rows = "".join(
        f"<tr class='{m.status}'><td>{_he(m.name)}</td>"
        f"<td>{'n/a' if m.value is None else _he(f'{m.value:.4g}')}</td>"
        f"<td>{_he(_STATUS_GLYPH.get(m.status, m.status))}</td>"
        f"<td>{_he(str(m.threshold or ''))}</td></tr>"
        for m in summary.metrics
    )
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>QC {_he(summary.phase)}</title>
<style>
body{{font-family:Arial,sans-serif;margin:2rem}}
table{{border-collapse:collapse}} td,th{{border:1px solid #ccc;padding:4px 10px}}
.fail{{background:#fde}} .warn{{background:#ffd}} .pass{{background:#efe}}
</style></head><body>
<h2>QC: {_he(summary.phase)} &mdash; {_he(_STATUS_GLYPH.get(summary.overall_status, summary.overall_status))}</h2>
<p>generated {_he(summary.generated_at)}</p>
<table><tr><th>metric</th><th>value</th><th>status</th><th>gate</th></tr>{rows}</table>
<p>sources: {', '.join(_he(p) for p in summary.provenance) or 'n/a'}</p>
</body></html>"""
    out_path = Path(out_path)
    out_path.write_text(html)
    return out_path


# ---------------------------------------------------------------------------
# CLI — the qc module's operator entry point (drives /brieflow-qc). Both modes
# use this layer: the runner calls build_phase_qc / qc_gate_verdict in-process;
# the operator runs it standalone here.
# ---------------------------------------------------------------------------

def _load_lab_defaults() -> dict:
    p = Path(__file__).resolve().parent.parent / "data" / "lab_defaults.yaml"
    try:
        return yaml.safe_load(p.read_text()) or {}
    except FileNotFoundError:
        return {}


def main() -> int:
    import argparse
    from datetime import datetime, timezone

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--analysis-dir", required=True, type=Path)
    ap.add_argument(
        "--phase",
        default="all",
        help="one of %s, or 'all' (default)" % ", ".join(PHASES),
    )
    ap.add_argument(
        "--warn-is-fail",
        action="store_true",
        help="treat 'warn' metrics as a gate failure (default: only 'fail' blocks)",
    )
    ap.add_argument(
        "--html",
        action="store_true",
        help="also write <phase>/eval/qc_report.html",
    )
    ap.add_argument("--json", action="store_true", help="emit machine-readable verdict JSON")
    args = ap.parse_args()

    if args.phase != "all" and args.phase not in PHASES:
        ap.error("unknown phase %r; expected one of %s" % (args.phase, ", ".join(PHASES + ["all"])))

    brieflow_output = Path(args.analysis_dir).resolve() / "brieflow_output"
    # Notebook-only phases produce no eval/ outputs, so "all" skips them.
    phases = (
        [p for p in PHASES if p not in NOTEBOOK_ONLY_PHASES]
        if args.phase == "all"
        else [args.phase]
    )
    lab = _load_lab_defaults()
    generated_at = datetime.now(timezone.utc).isoformat()

    verdicts = []
    rc = 0
    for phase in phases:
        summary = build_phase_qc(
            brieflow_output, phase, generated_at=generated_at, lab_defaults=lab
        )
        verdict = qc_gate_verdict(summary, warn_is_fail=args.warn_is_fail)
        verdicts.append({"phase": phase, **verdict})
        if not args.json:
            print(render_qc_text(summary))
            print(f"-> gate: {'PASS' if verdict['pass'] else 'FAIL'} {verdict.get('rationale', '')}\n")
        if args.html:
            write_qc_report_html(summary, brieflow_output / phase / "eval" / "qc_report.html")
        if not verdict["pass"]:
            rc = 1
    if args.json:
        print(json.dumps(verdicts, indent=2))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

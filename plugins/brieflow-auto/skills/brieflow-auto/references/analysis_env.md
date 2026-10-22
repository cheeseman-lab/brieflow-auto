# Brieflow analysis environment — reference

Architecture, repo conventions, and pipeline modules. Use this when you need
to orient yourself in a brieflow analysis directory — not as a source of
parameter values for any specific screen (see `wizard_workflow.md` for the
correct "what NOT to read" scope rules).

## Repository conventions

This skill deliberately **does not** enumerate active screens, archive paths,
or prior-analysis-directory locations. Those exist on the filesystem; you
don't need a map. If you find yourself probing for prior screens during a
wizard run, you're doing the wrong thing — re-read `wizard_workflow.md`
"Scope: what NOT to read."

The only paths that matter for the wizard are:

- **The current screen's analysis dir** (where you are): contains `screen.yaml`,
  the marimo gens, the `analysis/.brieflow/` plugin state, the eventual
  `analysis/config/`, and possibly preserved `analysis/brieflow_output/` if
  the operator has staged it for re-derive.
- **The raw-data dirs the operator tells you about** (the answers to
  `SBS_IMAGES_DIR_FP` and `PHENOTYPE_IMAGES_DIR_FP` user-bucket prompts).
  These are operator-supplied — **never guess**.

## Per-screen directory shape

```
<screen>-analysis/
├── analysis/
│   ├── config/
│   │   ├── config.yml              # Main pipeline config (canonical output)
│   │   ├── sbs_combo.tsv           # Pipeline-input TSVs (gen-produced)
│   │   ├── phenotype_combo.tsv
│   │   ├── merge_combo.tsv
│   │   ├── aggregate_combo.tsv
│   │   ├── cluster_combo.tsv
│   │   └── cell_data_metadata_cols.tsv
│   ├── .brieflow/                  # Plugin state (interview.json lives here)
│   ├── slurm/
│   │   ├── config.yaml             # SLURM resources
│   │   └── slurm_output/rule/      # Per-rule job logs
│   ├── flow.sh                     # Pipeline launcher (calls snakemake)
│   ├── 0_preprocess.py             # Dual-mode marimo gens (Stage 1a)
│   ├── 1_sbs.py
│   ├── 2_phenotype.py
│   ├── 3_merge.py
│   ├── 4_classify.py
│   ├── 5_aggregate.py
│   ├── 6_cluster.py
│   ├── 7_analyze.py
│   ├── screen.yaml                 # High-level screen metadata
│   └── brieflow_output/            # Pipeline outputs (parquet/tiff/zarr)
├── brieflow/                       # Git submodule — core pipeline code
│   ├── workflow/
│   │   ├── lib/       # Canonical library functions (gens are thin wrappers)
│   │   ├── scripts/   # Scripts calling lib
│   │   ├── rules/     # Snakemake rules (*.smk)
│   │   └── targets/   # Output definitions
│   └── pyproject.toml
```

## Pipeline modules (sequential)

1. **preprocess** — ND2 metadata, TIFF/zarr conversion, illumination correction
2. **sbs** — Alignment, peak finding, base calling, read calling
3. **phenotype** — CellPose/CellProfiler segmentation, feature extraction
4. **merge** — Cross-tile merging, deduplication
5. **aggregate** — Cell classification, filtering, dataset aggregation
6. **cluster** — PHATE + Leiden clustering, benchmarking

Plus two notebook-only phases (`NOTEBOOK_ONLY_PHASES` in `scripts/brieflow_phases.py`),
which write config but submit no module: `classify` (between merge and aggregate) and
`analyze` (after cluster). `PIPELINE_PHASES` in the same file is the authoritative order.

## Running modules

The interview-driven wizard + plugin handles this automatically via
`bin/brieflow-run`. For manual operations:

```bash
cd analysis/
bash flow.sh preprocess --configfile config/config.yml
bash flow.sh sbs phenotype --configfile config/config.yml
bash flow.sh merge --configfile config/config.yml
bash flow.sh aggregate --configfile config/config.yml
bash flow.sh cluster --configfile config/config.yml
```

## Conda environments

Convention: one env per screen, named `brieflow_<screen-id>`. Activate via:

```bash
eval "$(conda shell.bash hook)" && conda activate brieflow_<screen-id>
```

## Monitoring SLURM

```bash
squeue -u $USER
tail -f analysis/slurm/slurm_output/rule/<rule-name>/<job-id>.out
```

Per-rule slurm logs are the ground truth for failure investigation (the
plugin's `brieflow_recover.py` parses these). Never use mtime to identify
the right log — match by output filename grep.

## Snakemake tags

`all_preprocess`, `all_sbs`, `all_phenotype`, `all_merge`, `all_aggregate`,
`all_cluster`.

## External resources

- Docs: https://brieflow.readthedocs.io/
- Issues: https://github.com/cheeseman-lab/brieflow/issues
- Discord: https://discord.gg/yrEh6GP8JJ

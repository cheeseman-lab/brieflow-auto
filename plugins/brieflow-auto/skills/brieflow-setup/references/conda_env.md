# Setup Workflow Reference

Detailed steps for setting up a new OPS screen analysis (Mode A) or re-running/optimizing
an existing one (Mode B).

---

## Mode A: New Screen Setup (From Scratch)

### A1. Gather Screen Information

Collect the following before starting:

```
Screen name:          [e.g., "Whitney"]
Screen description:   [brief experimental description]
Organism/cell line:   [e.g., "HeLa", "RPE1"]
Number of plates:     [e.g., 12]
SBS cycles:           [e.g., 12]
Phenotype channels:   [e.g., DAPI, GFP, mCherry]
ND2 data location:    [full path to raw data]
Output location:      [where results should go, default: analysis/brieflow_output]
Compute environment:  [Slurm HPC / local / GCP]
Segmentation method:  [cellpose (default) / micro-sam]
```

If the user doesn't know something, provide the default and note it can be changed later.

### A2. Repository Creation

```bash
# 1. Create repo from template (user does this on GitHub)
#    Go to https://github.com/cheeseman-lab/brieflow-analysis
#    Click "Use this template" → "Create a new repository"
#    Name it: SCREEN_NAME-analysis (e.g., whitney-analysis)

# 2. Clone locally
git clone https://github.com/USER/SCREEN_NAME-analysis.git
cd SCREEN_NAME-analysis

# 3. Set up brieflow submodule
# Option A: Direct (for users who won't modify brieflow source)
git submodule update --init --recursive

# Option B: Fork-based (recommended for advanced users / Cheeseman lab)
git submodule set-url brieflow https://github.com/USER/brieflow.git
git submodule update --init --recursive
cd brieflow
git remote add upstream https://github.com/cheeseman-lab/brieflow.git
cd ..
```

**For pulling latest main** (important for getting upstream fixes):
```bash
cd brieflow
git fetch upstream
git checkout main
git merge upstream/main
cd ..
git add brieflow
git commit -m "Update brieflow submodule to latest main"
```

### A3. Environment Setup

**Use miniforge, not miniconda (recommended).** Miniconda ships conda 26.x, which
is hostile to scripted installs (see the gotchas below). Miniforge defaults to
conda-forge and carries neither the Anaconda ToS gate nor the `anaconda-anon-usage`
plugin, so `conda create` just works:

```bash
# One-time, on a fresh VM:
curl -fsSL -o ~/miniforge.sh \
  https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh
bash ~/miniforge.sh -b -p "$HOME/miniforge3"
eval "$("$HOME/miniforge3/bin/conda" shell.bash hook)" && conda init bash
```

```bash
# Create screen-specific conda environment
# Use python=3.12 (not 3.11 — the zarr3 brieflow line requires 3.12)
conda create -n brieflow_SCREEN_NAME -c conda-forge python=3.12 uv pip -y
conda activate brieflow_SCREEN_NAME

# Install dependencies
cd brieflow
uv pip install -r pyproject.toml
uv pip install -e .

# Optional: micro-sam for segmentation
conda install -c conda-forge micro_sam -y
```

**Miniconda fallback (conda 26.x gotchas).** If an operator is stuck on miniconda,
three issues bite, all avoided by miniforge:
- **ToS gate (B1):** conda 26.x refuses `conda create` until Anaconda default-channel
  Terms of Service are accepted — *even when `-c conda-forge` is the only channel*.
  Run once: `conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main`
  and again for `.../pkgs/r`.
- **anaconda-anon-usage sqlite lock (B2):** the bundled plugin contends on its
  sqlite db and surfaces as an opaque `sqlite3 OperationalError: database is locked`.
  Disable plugins (which also forces the classic solver, itself a plugin):
  `CONDA_NO_PLUGINS=true conda create --solver=classic -n brieflow_SCREEN_NAME -c conda-forge python=3.12 uv pip -y`.
- **libmamba shard-cache lock:** the libmamba solver can fail with the same
  `database is locked` collecting repodata; `--solver=classic` (above) avoids it.
  One-time during first env creation; later conda ops use libmamba fine.

**Gotcha — brieflow import name:** The editable install adds
`$BRIEFLOW_DATA_ROOT/<screen>-analysis/brieflow/workflow` to `sys.path`. The
importable package is `lib`, not `brieflow`. Verify with:
```python
python -c "import lib; print('brieflow lib ok')"
```

### A4. Verify Installation

```bash
eval "$(conda shell.bash hook)" && conda activate brieflow_SCREEN_NAME
cd brieflow/tests/small_test_analysis
python small_test_analysis_setup.py
sh run_brieflow.sh
cd ../../
pytest
```

If tests pass, proceed. If not, check the error output — common issues:
- Missing conda packages → re-run install step
- Python version mismatch → ensure 3.12
- Submodule not initialized → `git submodule update --init --recursive`

### A5. Stage screen.yaml

Stage the operator's filled-in `screen.yaml` (`scripts/brieflow_screen_yaml.py find` / `stage`).
If only the blank template exists, stop: `brieflow_screen_yaml.py check` lists the fields the
operator must fill in before configuration.

### A6. Proceed to Configuration

Configure each phase notebook with `/brieflow-notebook`, then launch with `/brieflow-run`.

---

## Mode B: Re-run / Optimize Existing Screen

### B1. Assess Current State

Ask:
- Which screen repo are you working with?
- What version of brieflow are you on? (`cd brieflow && git log --oneline -1`)
- Which step did you last complete successfully?
- What do you want to change? (parameters, brieflow version, re-run from specific step)

### B2. Update Brieflow (if needed)

```bash
cd brieflow
git fetch upstream
git checkout main
git merge upstream/main
cd ..

# Reinstall
eval "$(conda shell.bash hook)" && conda activate brieflow_SCREEN_NAME
uv pip install -e .
git add brieflow
git commit -m "Update brieflow submodule to latest main"
```

### B3. Config Migration

When porting configs from an old screen to a new one, or after a brieflow version update:

1. Identify which notebooks have changed:
   ```bash
   # Compare template notebooks with current
   diff -r analysis/ /path/to/brieflow-analysis-template/analysis/ --brief
   ```

2. For each changed notebook, re-run `/brieflow-notebook` for that phase; it pre-fills
   the new notebook's marker blocks from `.brieflow/interview.json`.

### B4. Proceed to the Relevant Step

Re-run from the phase you want with `/brieflow-run --from <phase>`.

---

## Multi-Screen Batch Setup

When running multiple screens (e.g., 8 screens in 2 weeks):

### Strategy

1. Create a master config YAML for each screen with all parameters
2. Use a loop to set up repos:
   ```bash
   for SCREEN in <screen-a> <screen-b> <screen-c>; do
     git clone https://github.com/cheeseman-lab/brieflow-analysis.git ${SCREEN}-analysis
     cd ${SCREEN}-analysis
     git submodule update --init --recursive
     # Apply screen-specific config via papermill or sed
     cd ..
   done
   ```
3. Launch preprocessing for all screens first (most I/O bound)
4. Stagger SBS+Phenotype runs to avoid overwhelming the cluster
5. Use tmux windows per screen for monitoring

### Screen Config Template (screen_config.yaml)
```yaml
screen_name: "whitney"
organism: "HeLa"
plates: 12
sbs_cycles: 12
phenotype_channels: ["DAPI", "GFP", "mCherry"]
nd2_data_path: "/path/to/whitney/nd2"
output_path: "/path/to/whitney/output"
segmentation:
  method: "cellpose"
  model: "cyto2"
  diameter: 30
barcode_calling:
  threshold: 0.5  # start with default, optimize
slurm:
  partition: "normal"
  memory: "32G"
  time: "24:00:00"
```

---

## Quick Reference Links

- **Brieflow source**: https://github.com/cheeseman-lab/brieflow
- **Brieflow-analysis template**: https://github.com/cheeseman-lab/brieflow-analysis
- **Docs**: https://brieflow.readthedocs.io
- **Discord**: https://discord.gg/yrEh6GP8JJ

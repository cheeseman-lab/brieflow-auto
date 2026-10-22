"""Group jobs indent Snakemake's `Error in rule X:` blocks by four spaces; the
anchored regexes missed them and recover.py escalated with rule=None (a
phenotype run 2026-08-27: extract_phenotype IndexError on an empty tile)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

LOG = """\
[Wed Aug 27 08:58:01 2026]
    Error in rule extract_phenotype:
        jobid: 4211
        output: brieflow_output/phenotype/parquets/1/r02/c05/106/phenotype_cp.parquet
        log: slurm/slurm_output/rule/group_x/7980521.log (check log file(s) for error details)
"""


def test_indented_error_block_is_parsed():
    spec = importlib.util.spec_from_file_location("brieflow_failure_logs", SCRIPTS / "brieflow_failure_logs.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_failure_logs"] = mod
    spec.loader.exec_module(mod)
    assert mod._ERROR_IN_RULE_RE.findall(LOG) == ["extract_phenotype"]
    m = mod._RULE_CONTEXT_RE.search(LOG)
    assert m and m.group(1) == "extract_phenotype" and m.group(2) == "4211" and "phenotype_cp.parquet" in m.group(3)
    m2 = mod._ERROR_LOG_PATH_RE.search(LOG)
    assert m2 and m2.group(3).endswith("7980521.log")

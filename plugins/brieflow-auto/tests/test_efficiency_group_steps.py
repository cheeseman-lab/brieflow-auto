"""A grouped job reports one efficiency row per job step, so phenotype_tile_group counted 21,900
observations for 4,380 jobs (5x) and inflated obs_n and CPU-hours."""

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import brieflow_aggregate_efficiency as ae  # noqa: E402

GROUP = "rule_phenotype_tile_group_align_phenotype_apply_ic_field_phenotype_extract_phenotype_inf"
FIELDS = ["JobID", "MainJobID", "RuleName", "MaxRSS_MB", "RequestedMem_MB", "Elapsed_sec",
          "CPU Efficiency (%)"]


def test_job_steps_count_as_one_observation(tmp_path):
    report = tmp_path / "efficiency_1.log"
    report.mkdir()
    with open(report / "efficiency_report_x.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for job in ("100", "101"):
            for step, rss in enumerate([600, 480, 1670, 340, 400]):
                w.writerow({"JobID": f"{job}.{step}", "MainJobID": job, "RuleName": GROUP,
                            "MaxRSS_MB": rss, "RequestedMem_MB": 6000, "Elapsed_sec": 10,
                            "CPU Efficiency (%)": 50})
    agg, observations, _ = ae.aggregate(tmp_path)
    assert agg["phenotype_tile_group"]["n"] == 2
    assert agg["phenotype_tile_group"]["peak"] == 1670
    assert [o["Elapsed_s"] for o in observations] == [50, 50]
    assert [o["MaxRSS_MB"] for o in observations] == [1670, 1670]

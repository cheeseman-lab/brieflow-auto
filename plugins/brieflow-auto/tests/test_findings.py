import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import brieflow_findings as bf  # noqa: E402

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_findings.py"


def _rows(tmp_path):
    return json.loads((tmp_path / ".brieflow" / "findings.json").read_text())


def test_workaround_is_appended_with_schema(tmp_path):
    bf.record_finding(tmp_path, kind="input-symlink-tree", phase="preprocess",
                      summary="filename well pattern not parsed",
                      workaround="symlinked Well1..3 to A1..A3", evidence=["data/bf_input/"])
    bf.record_finding(tmp_path, kind="resource-stopgap", phase="preprocess",
                      summary="rule missing from mem baseline", workaround="set-resources 64000")
    doc = _rows(tmp_path)
    assert doc["schema"] == "wizard-findings/v1"
    assert [r["kind"] for r in doc["findings"]] == ["input-symlink-tree", "resource-stopgap"]
    assert doc["findings"][0]["evidence"] == ["data/bf_input/"]
    assert doc["findings"][0]["source"] == "agent"


def test_legacy_list_is_kept(tmp_path):
    (tmp_path / ".brieflow").mkdir()
    (tmp_path / ".brieflow" / "findings.json").write_text('[{"kind": "canonical-template-gap"}]')
    bf.record_finding(tmp_path, kind="x", phase="sbs", summary="y")
    assert [r["kind"] for r in _rows(tmp_path)["findings"]] == ["canonical-template-gap", "x"]


def test_rejects_bad_source_and_empty_summary(tmp_path):
    with pytest.raises(ValueError):
        bf.record_finding(tmp_path, kind="x", phase="sbs", summary="y", source="bogus")
    with pytest.raises(ValueError):
        bf.record_finding(tmp_path, kind="x", phase="sbs", summary=" ")


def test_cli_and_wizard_facade(tmp_path):
    subprocess.run([sys.executable, str(SCRIPT), "--analysis-dir", str(tmp_path), "--kind",
                    "screen-yaml-edit", "--phase", "sbs", "--summary", "dye-named channels",
                    "--workaround", "renamed to G/T/A/C"], check=True, capture_output=True)
    assert _rows(tmp_path)["findings"][0]["workaround"] == "renamed to G/T/A/C"
    import brieflow_wizard
    assert brieflow_wizard.record_finding is bf.record_finding

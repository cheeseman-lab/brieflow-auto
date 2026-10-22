"""The prospective loop is tested on synthetic evidence and an isolated toy helper."""

import json
import subprocess
import sys

import pytest

import brieflow_improve as improve


@pytest.fixture
def inputs(tmp_path):
    source = tmp_path / "repo/plugins/brieflow-auto"
    (source / "scripts").mkdir(parents=True)
    (source / "tests").mkdir()
    helper = source / "scripts/example.py"
    helper.write_text("def value():\n    return 1\n")
    ledger = tmp_path / "ledger.md"
    ledger.write_text("Synthetic failure: value should be 2. Not a screen result.")
    bundle = improve.collect([], tmp_path / "bundle.json", [ledger])
    evidence_id = json.loads(bundle.read_text())["sources"][0]["id"]
    test = "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))\nfrom example import value\ndef test_value():\n    assert value() == 2\n"
    spec = {
        "hypothesis": "Synthetic off-by-one defect",
        "compatibility": "Toy fixture only",
        "held_out_plan": "Independent inputs required before any general claim",
        "evidence_ids": [evidence_id],
        "edits": [
            {
                "path": "scripts/example.py",
                "before_sha256": improve.digest(helper.read_bytes()),
                "content": "def value():\n    return 2\n",
            },
            {"path": "tests/test_regression.py", "before_sha256": None, "content": test},
        ],
    }
    proposal = tmp_path / "proposal.json"
    proposal.write_text(json.dumps(spec))
    return source, bundle, proposal, tmp_path / "round"


def test_real_synthetic_comparison_does_not_promote(inputs):
    source, bundle, proposal, workspace = inputs
    improve.stage(*inputs)
    result = improve.evaluate(workspace, execute_tests=True, timeout=30)
    assert result["status"] == "review-required"
    assert result["tests"]["baseline"]["returncode"] == 1
    assert result["tests"]["candidate"]["returncode"] == 0
    assert not result["promoted"]
    assert "return 1" in (source / "scripts/example.py").read_text()
    assert not json.loads(bundle.read_text())["rollouts_included"]


@pytest.mark.parametrize(
    "path",
    [
        "../brieflow/a.py",
        "/etc/a.py",
        "data/qc_gates.yaml",
        "scripts/brieflow_qc.py",
        "scripts/brieflow_improve.py",
    ],
)
def test_protected_paths_rejected(inputs, path):
    source, bundle, proposal, workspace = inputs
    spec = json.loads(proposal.read_text())
    spec["edits"][0]["path"] = path
    proposal.write_text(json.dumps(spec))
    with pytest.raises(ValueError):
        improve.stage(*inputs)
    assert not workspace.exists()


def test_stale_source_rejected(inputs):
    source, _, _, workspace = inputs
    (source / "scripts/example.py").write_text("changed")
    with pytest.raises(ValueError, match="Source changed"):
        improve.stage(*inputs)
    assert not workspace.exists()


def test_existing_tests_cannot_be_changed(inputs):
    source, _, _, _ = inputs
    (source / "tests/test_regression.py").write_text("existing")
    with pytest.raises(ValueError):
        improve.stage(*inputs)


def test_requires_execution_authority_and_unchanged_snapshot(inputs):
    improve.stage(*inputs)
    workspace = inputs[-1]
    with pytest.raises(ValueError, match="execute-tests"):
        improve.evaluate(workspace)
    (workspace / "candidate/plugins/brieflow-auto/scripts/example.py").write_text("changed")
    with pytest.raises(ValueError, match="Frozen candidate"):
        improve.evaluate(workspace, execute_tests=True)


@pytest.mark.parametrize(
    "codes,status",
    [([0, 0], "no-regression-fix-demonstrated"), ([1, 1], "rejected"), ([2, 0], "inconclusive")],
)
def test_verdicts_and_notification_isolation(inputs, monkeypatch, codes, status):
    improve.stage(*inputs)
    returns = iter(codes)

    def run(argv, **kwargs):
        assert argv[:3] == [sys.executable, "-m", "pytest"]
        assert kwargs["env"]["BRIEFLOW_NOTIFY_MODE"] == "disabled"
        assert kwargs["env"]["BRIEFLOW_SLACK_WEBHOOK"] == ""
        assert "SECRET_TEST_TOKEN" not in kwargs["env"]
        return subprocess.CompletedProcess(argv, next(returns), "", "")

    monkeypatch.setenv("SECRET_TEST_TOKEN", "never-inherited")
    monkeypatch.setattr(improve.subprocess, "run", run)
    assert improve.evaluate(inputs[-1], execute_tests=True)["status"] == status

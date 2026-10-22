"""Evidence collection finds, chunks and excerpts run records instead of refusing them."""

import json

import pytest

import brieflow_improve as improve


def _line(stamp, text):
    return json.dumps({"timestamp": stamp, "message": text}) + "\n"


@pytest.fixture
def screen(tmp_path, monkeypatch):
    monkeypatch.setattr(improve, "MAX_BYTES", 400)
    workspace = tmp_path / "screen"
    analysis = workspace / "screen-analysis" / "analysis"
    state = analysis / ".brieflow"
    trace = state / "traces" / "20260928T120000Z-abc"
    (trace / "before").mkdir(parents=True)
    (trace / "trace.json").write_text(
        json.dumps({"schema": "brieflow-trace/v1", "started_at": "20260928T120000Z"})
    )
    (trace / "runner.log").write_text("phase sbs: failed\n")
    (state / "decisions.jsonl").write_text('{"kind": "blocker-pause"}\n')
    (trace / "before" / "decisions.jsonl").write_text('{"kind": "blocker-pause"}\n')
    (state / "issue_drafts.jsonl").write_text('{"phase": "preprocess"}\n')
    (state / "tuned_sbs.json").write_text('{"THRESHOLD_READS": 3200}')
    sweep = state / "sweeps" / "sbs" / "joint"
    (sweep / "__marimo__").mkdir(parents=True)
    (sweep / "tile.tsv").write_text("value\tsingle_map\n3200\tnan\n")
    (sweep / "tile.prints.log").write_text("intra_cycle_channel_shift_residual_max_px: 452.0\n")
    (sweep / "__marimo__" / "session.json").write_text("{}")
    efficiency = analysis / "logs" / "efficiency_1.log"
    efficiency.mkdir(parents=True)
    (efficiency / "efficiency_report_x.csv").write_text(
        "RuleName,RequestedMem_MB,MaxRSS_MB,TotalCPU_sec\n"
        "rule_segment_sbs_wildcards_1_A_2,3000.0,2900.5,7200\n"
        "rule_segment_sbs_wildcards_1_A_3,3000.0,2950.0,3600\n"
    )
    calls = "".join(
        f"sbatch call: sbatch --job-name {i:08d}-aaaa-bbbb-cccc-dddddddddddd --output "
        f"{'/long/path' * 40}/%j.log -p 60 --mem 9000 -D {'/workdir' * 40} --wrap 'x'\n"
        for i in range(40)
    )
    (analysis / "logs" / "sbs-1.log").write_text("ok\n" * 200 + calls)
    rollouts = workspace / ".brieflow" / "rollouts" / "session"
    rollouts.mkdir(parents=True)
    early = _line("2026-09-28T11:00:00Z", "start") + _line("2026-09-28T11:30:00Z", "x" * 300)
    late = early + _line("2026-09-28T13:00:00Z", "launched the runner")
    (rollouts / "rollout-early.jsonl").write_text(early)
    (rollouts / "rollout-late.jsonl").write_text(late)
    (rollouts / "trace.json").write_text(
        json.dumps({"rollouts": [{"sha256": improve.digest(late.encode()), "session_id": "s1"}]})
    )
    (workspace / "SCREEN_LOG.md").write_text("- workaround: renamed channels to G/T/A/C\n")
    return analysis, workspace, late


def _bundle(tmp_path, analysis, **kwargs):
    return json.loads(improve.collect([analysis], tmp_path / "bundle.json", **kwargs).read_text())


def test_discovers_records_next_to_the_analysis_dir(tmp_path, screen):
    analysis, workspace, _ = screen
    bundle = _bundle(tmp_path, analysis)
    paths = {s["path"] for s in bundle["sources"]}
    names = {p.rsplit("/", 1)[-1] for p in paths}
    assert {"trace.json", "runner.log", "SCREEN_LOG.md", "rollout-late.jsonl"} <= names
    assert {"issue_drafts.jsonl", "tuned_sbs.json", "tile.tsv", "tile.prints.log"} <= names
    assert "efficiency_report_x.csv" in names and "session.json" not in names
    assert bundle["rollouts_included"] and bundle["auto_discovered"]
    kinds = {s["kind"] for s in bundle["sources"] if s["path"].endswith("SCREEN_LOG.md")}
    assert kinds == {"operator-log"}


def test_without_discovery_only_explicit_selections(tmp_path, screen):
    analysis, _, _ = screen
    bundle = _bundle(tmp_path, analysis, auto_discover=False)
    names = {s["path"].rsplit("/", 1)[-1] for s in bundle["sources"]}
    assert "rollout-late.jsonl" not in names and "SCREEN_LOG.md" not in names
    assert not bundle["rollouts_included"]


def test_oversized_transcript_is_chunked_and_prefix_snapshot_skipped(tmp_path, screen):
    analysis, _, late = screen
    bundle = _bundle(tmp_path, analysis)
    chunks = [s for s in bundle["sources"] if s["path"].endswith("rollout-late.jsonl")]
    assert len(chunks) > 1 and all(s["bytes"] <= improve.MAX_BYTES for s in chunks)
    assert "".join(s["content"] for s in chunks) == late
    assert {s["file_sha256"] for s in chunks} == {improve.digest(late.encode())}
    skip = [s for s in bundle["skipped"] if s["path"].endswith("rollout-early.jsonl")]
    assert skip and "byte prefix of rollout-late.jsonl" in skip[0]["reason"]


def test_large_pipeline_log_is_excerpted_with_counts(tmp_path, screen):
    analysis, _, _ = screen
    bundle = _bundle(tmp_path, analysis)
    (excerpt,) = [s for s in bundle["sources"] if s["path"].endswith("sbs-1.log")]
    assert excerpt["kind"] == "log-excerpt" and "excerpt_of" in excerpt
    assert "(x40)" in excerpt["content"] and "-p 60" in excerpt["content"]
    assert "ok" not in excerpt["content"]


def test_efficiency_reports_become_per_rule_summaries(tmp_path, screen):
    analysis, _, _ = screen
    bundle = _bundle(tmp_path, analysis)
    (summary,) = [s for s in bundle["sources"] if s["kind"] == "efficiency-summary"]
    assert "rule_segment_sbs\t2\t3000\t2950\t2950\t3.00" in summary["content"]
    assert not any(s["path"].endswith("efficiency_1.log") for s in bundle["skipped"])


def test_duplicates_keep_one_copy_and_explicit_missing_file_fails(tmp_path, screen):
    analysis, _, _ = screen
    bundle = _bundle(tmp_path, analysis)
    copies = [s for s in bundle["sources"] if s["path"].endswith("decisions.jsonl")]
    assert len(copies) == 2 and sum("content" in s for s in copies) == 1
    assert any("duplicate_of" in s for s in copies)
    with pytest.raises(ValueError, match="does not exist"):
        improve.collect([analysis], tmp_path / "b2.json", [tmp_path / "missing.md"])


def test_symlink_is_skipped_not_fatal(tmp_path, screen):
    analysis, workspace, _ = screen
    (workspace / "LINK_LOG.md").symlink_to(workspace / "SCREEN_LOG.md")
    bundle = _bundle(tmp_path, analysis)
    assert any(s["reason"] == "not a regular file" for s in bundle["skipped"])


def test_traces_link_to_rollouts_by_time_then_session(tmp_path, screen):
    analysis, _, late = screen
    bundle = _bundle(tmp_path, analysis)
    assert [link["method"] for link in bundle["links"]] == ["time-window"]
    trace = next((analysis / ".brieflow" / "traces").iterdir()) / "trace.json"
    trace.write_text(json.dumps({"started_at": "20261001T000000Z", "agent_session_id": "s1"}))
    linked = json.loads(improve.collect([analysis], tmp_path / "b3.json").read_text())["links"]
    assert [link["method"] for link in linked] == ["session-id"]
    assert linked[0]["rollout_sha256"] == improve.digest(late.encode())


def test_chunk_ids_are_accepted_as_proposal_evidence(tmp_path, screen):
    analysis, _, _ = screen
    bundle = _bundle(tmp_path, analysis)
    known = {s["id"] for s in bundle["sources"]}
    chunk = next(s for s in bundle["sources"] if "chunk" in s)
    assert chunk["id"] in known

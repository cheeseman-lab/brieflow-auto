"""render_interview_review_html — the one-glance runner param-summary artifact
(same content as the markdown review, as portable HTML)."""
from __future__ import annotations


def _review():
    return {
        "rows": [
            {"notebook": "preprocess", "param": "SBS_DATA_FORMAT", "value": "tiff",
             "source": "operator", "category": "operator"},
            {"notebook": "sbs", "param": "THRESHOLD_READS", "value": 200,
             "source": "tuned-output", "category": "agent-resolved"},
        ],
        "summary": {"operator": 1, "agent-resolved": 1},
        "attention": {
            "needs_answer": ["CELL_DIAMETER (phenotype)"],
            "note_overrides": [{"param": "DIAMETER", "suggestion": "raise to 40",
                                "excerpt": "cells look big"}],
            "detected_confirm": [{"param": "N_CYCLES", "value": 12}],
            "runtime_pending": ["THRESHOLD_READS", "FOCI_THRESHOLD"],
        },
    }


def test_html_summary_has_badges_attention_and_per_phase_table(load_script):
    iv = load_script("brieflow_interview")
    html = iv.render_interview_review_html(_review())

    assert "1 operator" in html and "1 agent-resolved" in html          # badges
    assert "Needs your answer" in html and "CELL_DIAMETER (phenotype)" in html
    assert "From your notes" in html and "DIAMETER" in html and "raise to 40" in html
    assert "Detected from your data" in html and "N_CYCLES" in html
    assert "Resolved at runtime (2:" in html                            # pending count
    assert "<table>" in html
    assert "THRESHOLD_READS" in html and "200" in html
    assert "SBS_DATA_FORMAT" in html and "tiff" in html
    assert html.startswith("<!doctype html>") and html.rstrip().endswith("</html>")


def test_html_escapes_and_renders_none_as_dash(load_script):
    iv = load_script("brieflow_interview")
    review = {
        "rows": [{"notebook": "sbs", "param": "X<Y", "value": None,
                  "source": "default", "category": "default"}],
        "summary": {"default": 1},
        "attention": {"needs_answer": [], "note_overrides": [], "runtime_pending": []},
    }
    html = iv.render_interview_review_html(review)  # detected_confirm absent → no crash
    assert "X&lt;Y" in html  # escaped
    assert "—" in html       # None → em-dash

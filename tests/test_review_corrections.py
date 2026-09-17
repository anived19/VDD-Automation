"""apply_corrections must never record a correction that changes nothing on
the report.

Background (2026-09-16, first gpt-5.6-luna run): the reviewer proposed
field='gst_since' -> '2024-01-02' for the "GST Since: N/A" row. The row
reads entity['date_of_registration'], so the value was set on a key nothing
renders, logged as "applied" in two consecutive passes, and the report never
changed. These tests drive graph.apply_corrections directly -- no LLM.

Run: C:\\Python313\\python.exe -m pytest tests -q
"""
import re
from pathlib import Path

from vdd.report.build_context import REPORT_ENTITY_FIELDS
from vdd.review import graph
from vdd.review.graph import _format_entity_fields


def test_report_entity_fields_match_what_build_context_reads():
    src = Path(graph.__file__).parent.parent.joinpath("report", "build_context.py").read_text(encoding="utf-8")
    read = set(re.findall(r'entity\.get\("([a-z_0-9]+)"', src)) | set(re.findall(r'entity\["([a-z_0-9]+)"\]', src))
    assert read == set(REPORT_ENTITY_FIELDS)
    assert list(REPORT_ENTITY_FIELDS) == sorted(REPORT_ENTITY_FIELDS)   # keep it greppable


def _state(findings, entity=None, monkeypatch=None):
    """Minimal ReviewState for apply_corrections, with the scoring/context
    rebuild stubbed out -- this is about the correction bookkeeping only."""
    class _Engine:
        def __init__(self, path): pass
        def score_no_consent(self, resolved): return {"resolved": resolved}
    monkeypatch.setattr(graph, "ScoringEngine", _Engine)
    monkeypatch.setattr(graph, "build_context", lambda entity, result: {"entity": entity})
    return {"scoring_model_path": "unused", "entity": entity or {}, "resolved": {}, "iteration": 1,
            "passes": [{"findings": findings}], "escalations": []}


def _correct(field, value="2024-01-02"):
    return {"field": field, "parameter_id": None, "action": "correct", "confidence": "verified",
            "proposed_value": value, "proposed_note": "recheck_gstin_live: date_of_registration=2024-01-02",
            "issue": "GST Since shows N/A"}


def test_correction_to_a_field_the_report_reads_is_applied(monkeypatch):
    out = graph.apply_corrections(_state([_correct("date_of_registration")], monkeypatch=monkeypatch))
    assert out["entity"]["date_of_registration"] == "2024-01-02"
    assert len(out["corrections_applied"]) == 1 and out["escalations"] == []
    assert out["corrections_applied"][0]["before"] == {"value": None}


def test_correction_to_an_unknown_field_is_escalated_not_applied(monkeypatch):
    out = graph.apply_corrections(_state([_correct("gst_since")], monkeypatch=monkeypatch))
    assert out["corrections_applied"] == []
    assert "gst_since" not in out["entity"]                                  # nothing silently set
    (esc,) = out["escalations"]
    assert "does not read" in esc["reason"] and "date_of_registration" in esc["reason"]


def test_correction_with_neither_id_nor_field_is_escalated(monkeypatch):
    out = graph.apply_corrections(_state([_correct(None)], monkeypatch=monkeypatch))
    assert out["corrections_applied"] == [] and len(out["escalations"]) == 1


def test_entity_fields_section_lists_every_field_with_missing_ones_visible():
    txt = _format_entity_fields({"gstin": "33AAAAA0000A1Z5", "partners": ["A", "B"]})
    for k in REPORT_ENTITY_FIELDS:
        assert f"- {k}: " in txt
    assert "- gstin: '33AAAAA0000A1Z5'" in txt
    assert "- partners: <list of 2>" in txt
    assert "- date_of_registration: None  (report shows N/A / Not Available)" in txt

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
    txt = _format_entity_fields({"gstin": "33AAAAA0000A1Z5", "declared_hsn": [("7204", "a"), ("7215", "b")]})
    for k in REPORT_ENTITY_FIELDS:
        assert f"- {k}: " in txt
    assert "- gstin: '33AAAAA0000A1Z5'" in txt
    assert "- declared_hsn: <list of 2>" in txt
    assert "- date_of_registration: None  (report shows N/A / Not Available)" in txt


def _correct_param(pid, value, note="recheck_gstin_live: GSTR3B 3 late, max delay 8 days"):
    return {"field": None, "parameter_id": pid, "action": "correct", "confidence": "verified",
            "proposed_value": value, "proposed_note": note, "issue": "wording contradicts the evidence"}


def test_note_only_correction_keeps_the_value(monkeypatch):
    """A reviewer objecting to the wording (proposed_value null) must not turn a
    scored parameter into an unresolved one (Skandan Plastrix, 18-Sep: 3/3 -> 0/3)."""
    from vdd.resolve.resolvers import Resolved
    st = _state([_correct_param("com_gst_delay_days", None)], monkeypatch=monkeypatch)
    st["resolved"] = {"com_gst_delay_days": Resolved.ok(8.0, "ongrid filing_data", note="max delay 8 days")}
    out = graph.apply_corrections(st)
    r = out["resolved"]["com_gst_delay_days"]
    assert r.value == 8.0 and not r.unresolved and r.source == "llm-review"
    assert "3 late" in r.note
    (rec,) = out["corrections_applied"]
    assert rec["note_only"] is True and rec["before"]["value"] == 8.0


def test_value_correction_still_replaces_the_value(monkeypatch):
    from vdd.resolve.resolvers import Resolved
    st = _state([_correct_param("com_gst_delay_days", 14.0)], monkeypatch=monkeypatch)
    st["resolved"] = {"com_gst_delay_days": Resolved.ok(8.0, "ongrid filing_data")}
    out = graph.apply_corrections(st)
    assert out["resolved"]["com_gst_delay_days"].value == 14.0


def test_note_only_correction_on_unresolved_parameter_is_escalated(monkeypatch):
    from vdd.resolve.resolvers import Resolved
    st = _state([_correct_param("com_pf_filing_status", None)], monkeypatch=monkeypatch)
    st["resolved"] = {"com_pf_filing_status": Resolved.missing("no EPFO data")}
    out = graph.apply_corrections(st)
    assert out["corrections_applied"] == [] and len(out["escalations"]) == 1
    assert out["resolved"]["com_pf_filing_status"].unresolved


def test_field_correction_without_a_value_is_escalated(monkeypatch):
    out = graph.apply_corrections(_state([_correct("date_of_registration", value=None)], monkeypatch=monkeypatch))
    assert out["corrections_applied"] == [] and len(out["escalations"]) == 1
    assert "date_of_registration" not in out["entity"]


def test_correction_to_a_value_outside_the_rows_options_is_escalated(monkeypatch):
    """An unknown bucket would score as unresolved -- never applied."""
    from vdd.resolve.resolvers import Resolved
    st = _state([_correct_param("addr_electricity_bill", "unresolved")], monkeypatch=monkeypatch)
    st["scoring_model_path"] = "config/scoring_model.json"
    st["resolved"] = {"addr_electricity_bill": Resolved.ok("match", "bill vs gst")}
    out = graph.apply_corrections(st)
    assert out["corrections_applied"] == [] and len(out["escalations"]) == 1
    assert "not one of" in out["escalations"][0]["reason"]
    assert out["resolved"]["addr_electricity_bill"].value == "match"
    # a valid bucket still applies
    st = _state([_correct_param("addr_electricity_bill", "not_match")], monkeypatch=monkeypatch)
    st["scoring_model_path"] = "config/scoring_model.json"
    st["resolved"] = {"addr_electricity_bill": Resolved.ok("match", "bill vs gst")}
    assert graph.apply_corrections(st)["resolved"]["addr_electricity_bill"].value == "not_match"

"""The analyst review workbook round trip: write -> edit in Excel -> read ->
validate -> apply -> re-score. Uses the real scoring model and a synthetic
vendor so the numbers are checkable by hand.
"""
from __future__ import annotations

import re

import pytest
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from vdd.extract.classify import ClassifiedDocs
from vdd.resolve.resolvers import Resolved
from vdd.review_sheet import (
    C_ANALYST, C_CODE, C_NAME, C_NOTE, C_PID, C_PREVIEW, S_DOCS, S_ENTITY, S_META, S_REVIEW, ReviewSheetError,
    _excel_condition, apply_review_sheet, param_specs, read_review_workbook, validate_review_sheet,
    write_review_workbook,
)
from vdd.score.engine import ScoringEngine

MODEL = "config/scoring_model.json"


def _vendor():
    """A partly-resolved vendor: some clean values, one numeric, the rest unresolved."""
    engine = ScoringEngine(MODEL)
    resolved = {p["parameterId"]: Resolved.missing("not determined in this run")
                for c in engine.model["categories"] for p in c["parameters"]}
    resolved.update({
        "com_gstin_active": Resolved.ok("active", "ongrid"),
        "com_gst_vintage": Resolved.ok(2.7, "gst_certificate"),
        "com_gst_delay_days": Resolved.ok(8.0, "ongrid filing_data"),
        "addr_ownership_type": Resolved.ok("owned", "doc:electricity_bill", note="NO SALE DEED ON FILE -- inferred"),
        "addr_electricity_bill": Resolved.ok("match", "bill vs gst"),
        "ident_constitution": Resolved.ok("partnership_proprietorship", "gst_certificate"),
        "ident_pan_name_match": Resolved.ok("match", "pan vs gst"),
    })
    entity = {"legal_name": "EXAMPLE TRADING CO", "trade_name": "EXAMPLE TRADING CO", "gstin": "19AAAAA0000A1Z5",
              "pan": "AAAAA0000A", "constitution": "Partnership", "date_of_registration": None,
              "partners": ["A PERSON", "B PERSON"], "declared_hsn": [("7204", "Ferrous waste")]}
    docs = ClassifiedDocs(by_type={"gst_certificate": ["v/GST.pdf"], "electricity_bill": ["v/bill.pdf"]},
                          unmatched=["v/Screenshot 1.png"])
    return engine, entity, resolved, docs


def _write(tmp_path, engine, entity, resolved, docs, **kw):
    result = engine.score_no_consent(resolved)
    path = str(tmp_path / "EXAMPLE_review.xlsx")
    write_review_workbook(path, vendor_name="Example Trading Co", entity=entity, resolved=resolved, result=result,
                          docs=docs, model=engine.model, scoring_model_path=MODEL,
                          cross_check_items=["addr_ownership_type: NO SALE DEED ON FILE"], **kw)
    return path, result


def _edit(path, param_values: dict, entity_values: dict = None, doc_values: dict = None, analyst=""):
    """What an analyst does in Excel: fill the yellow cells and save."""
    wb = load_workbook(path)
    ws = wb[S_REVIEW]
    for r in ws.iter_rows(min_row=1):
        pid = r[C_PID - 1].value
        if isinstance(pid, str) and pid in param_values:
            value, note = param_values[pid]
            r[C_ANALYST - 1].value = value
            r[C_NOTE - 1].value = note
    for r in wb[S_ENTITY].iter_rows(min_row=3):
        if r[0].value in (entity_values or {}):
            r[2].value, r[3].value = entity_values[r[0].value]
    for r in wb[S_DOCS].iter_rows(min_row=3):
        if r[0].value in (doc_values or {}):
            r[2].value = doc_values[r[0].value]
    if analyst:
        for r in wb[S_META].iter_rows(min_row=1, max_col=2):
            if r[0].value == "Analyst name":
                r[1].value = analyst
    wb.save(path)


# ---------------------------------------------------------------- writing

def test_workbook_has_every_parameter_with_dropdown_and_preview(tmp_path):
    engine, entity, resolved, docs = _vendor()
    path, result = _write(tmp_path, engine, entity, resolved, docs)
    ws = load_workbook(path)[S_REVIEW]
    pids = [r[C_PID - 1].value for r in ws.iter_rows() if isinstance(r[C_PID - 1].value, str)
            and re.fullmatch(r"[a-z][a-z0-9_]*", r[C_PID - 1].value)]
    assert pids == [p.parameter_id for c in result.categories for p in c.params]
    previews = [r[C_PREVIEW - 1].value for r in ws.iter_rows() if isinstance(r[C_PID - 1].value, str)
                and r[C_PID - 1].value in pids]
    assert all(str(p).startswith("=IF(") for p in previews)
    # one validation rule per parameter row (+ none on headers)
    assert len(ws.data_validations.dataValidation) == len(pids)
    # a category param's dropdown lists its buckets plus 'unresolved'
    dv = next(d for d in ws.data_validations.dataValidation if "owned" in str(d.formula1))
    assert '"owned,leased,rented,address_mismatch"' == dv.formula1     # Proof of Address rows: no 'unresolved'
    com_row = next(r for r in ws.iter_rows() if r[C_PID - 1].value == "com_gstin_active")
    com_dv = next(d for d in ws.data_validations.dataValidation if str(com_row[C_ANALYST - 1].coordinate) in str(d.sqref))
    assert com_dv.formula1.endswith(',unresolved"')                       # other sections keep it
    # numeric params get a decimal rule and a nested-IF preview
    specs = param_specs(engine.model)
    assert specs["com_gst_delay_days"].numeric and specs["com_gst_vintage"].numeric
    delay_row = next(r for r in ws.iter_rows() if r[C_PID - 1].value == "com_gst_delay_days")
    assert "ISNUMBER(" in delay_row[C_PREVIEW - 1].value and "AND(" in delay_row[C_PREVIEW - 1].value


def test_excel_condition_translation_covers_every_expr_form():
    assert _excel_condition("value > 5", "J9") == "J9>5"
    assert _excel_condition("value <= 10", "J9") == "J9<=10"
    assert _excel_condition("value IN [2, 5]", "J9") == "AND(J9>=2,J9<=5)"
    assert _excel_condition("value IN (10, 20]", "J9") == "AND(J9>10,J9<=20)"
    assert _excel_condition("value IN [1, 2)", "J9") == "AND(J9>=1,J9<2)"
    assert _excel_condition("value == 100", "J9") == "J9=100"
    assert _excel_condition('value == "owned"', "J9") is None


def test_summary_and_section_formulas_reference_the_table(tmp_path):
    engine, entity, resolved, docs = _vendor()
    path, _ = _write(tmp_path, engine, entity, resolved, docs)
    ws = load_workbook(path)[S_REVIEW]
    total_row = next(r for r in ws.iter_rows() if r[0].value == "TOTAL (/100)")
    assert str(total_row[4].value).startswith("=SUM(E")          # summary: A:B section, C system, D max, E preview
    section = next(r for r in ws.iter_rows() if r[C_CODE - 1].value == "COM" and r[C_NAME - 1].value == "COMPLIANCE")
    assert str(section[C_PREVIEW - 1].value).startswith(f"=SUM({get_column_letter(C_PREVIEW)}")


def test_only_analyst_cells_are_unlocked(tmp_path):
    engine, entity, resolved, docs = _vendor()
    path, _ = _write(tmp_path, engine, entity, resolved, docs)
    ws = load_workbook(path)[S_REVIEW]
    assert ws.protection.sheet
    row = next(r for r in ws.iter_rows() if r[C_PID - 1].value == "addr_ownership_type")
    assert not row[C_ANALYST - 1].protection.locked and not row[C_NOTE - 1].protection.locked
    assert row[C_PID - 1].protection.locked and row[C_PREVIEW - 1].protection.locked


# ---------------------------------------------------------------- round trip

def test_untouched_sheet_applies_nothing(tmp_path):
    engine, entity, resolved, docs = _vendor()
    path, _ = _write(tmp_path, engine, entity, resolved, docs)
    sheet = read_review_workbook(path)
    assert sheet.vendor_name == "Example Trading Co"
    assert sheet.param_overrides == {} and sheet.entity_overrides == {} and sheet.doc_overrides == {}
    applied = apply_review_sheet(entity, resolved, sheet, engine.model, analyst="x")
    assert applied.records == [] and applied.audit_rows == []
    assert engine.score_no_consent(applied.resolved).total == engine.score_no_consent(resolved).total


def test_analyst_edits_change_the_score_and_are_audited(tmp_path):
    engine, entity, resolved, docs = _vendor()
    path, _ = _write(tmp_path, engine, entity, resolved, docs)
    before = engine.score_no_consent(resolved).total
    _edit(path, {"addr_ownership_type": ("rented", "Rental agreement received by email"),
                 "com_gst_delay_days": (25, "Portal shows 25-day delay on GSTR-3B for June"),
                 "ident_pan_name_match": ("unresolved", "PAN card illegible")},
          entity_values={"date_of_registration": ("02/01/2024", "from the GST certificate"),
                         "declared_hsn": ("7204 - Ferrous waste; 7215 - Steel bars", "second HSN added on the portal")},
          doc_values={"Screenshot 1.png": "client_photo"}, analyst="R. Analyst")
    sheet = read_review_workbook(path)
    assert sheet.analyst_name == "R. Analyst"
    assert validate_review_sheet(sheet, engine.model) == []
    applied = apply_review_sheet(entity, resolved, sheet, engine.model, analyst=sheet.analyst_name, when="2026-09-18 10:00")

    assert applied.resolved["addr_ownership_type"].value == "rented"
    assert applied.resolved["addr_ownership_type"].source.startswith("analyst: R. Analyst")
    assert applied.resolved["addr_ownership_type"].note == "Rental agreement received by email"
    assert applied.resolved["com_gst_delay_days"].value == 25.0
    assert applied.resolved["ident_pan_name_match"].unresolved
    assert applied.entity["date_of_registration"] == "02/01/2024"
    assert applied.entity["declared_hsn"] == [("7204", "Ferrous waste"), ("7215", "Steel bars")]
    assert sheet.doc_overrides == {"Screenshot 1.png": "client_photo"}

    after = engine.score_no_consent(applied.resolved).total

    def pts(pid, expr):
        return next(sm["assignedScore"] for c in engine.model["categories"] for p in c["parameters"]
                    if p["parameterId"] == pid for sm in p["scoreMappings"] if sm["expr"] == expr)
    # owned -> rented; 8 days -> 25 days; pan match -> unresolved (0); and the
    # rental-validation row re-derives to rented_matching (bill address matched)
    expected_delta = ((pts("addr_ownership_type", 'value == "rented"') - pts("addr_ownership_type", 'value == "owned"'))
                      + (pts("com_gst_delay_days", "value IN (20, 30]") - pts("com_gst_delay_days", "value <= 10"))
                      - pts("ident_pan_name_match", 'value == "match"')
                      + pts("addr_rental_validation", 'value == "rented_matching"'))
    assert after == before + expected_delta
    assert applied.resolved["addr_rental_validation"].value == "rented_matching"
    kinds = [(a["kind"], a["id"]) for a in applied.audit_rows]
    assert ("parameter", "addr_ownership_type") in kinds and ("field", "declared_hsn") in kinds \
        and ("document", "Screenshot 1.png") in kinds
    assert all(a["who"] == "R. Analyst" and a["when"] == "2026-09-18 10:00" for a in applied.audit_rows)


def test_excel_date_cells_come_back_as_dd_mm_yyyy(tmp_path):
    from datetime import datetime
    engine, entity, resolved, docs = _vendor()
    path, _ = _write(tmp_path, engine, entity, resolved, docs)
    wb = load_workbook(path)
    for r in wb[S_ENTITY].iter_rows(min_row=3):
        if r[0].value == "date_of_registration":
            r[2].value = datetime(2024, 1, 2)   # Excel turned the typed date into a real date
    wb.save(path)
    sheet = read_review_workbook(path)
    assert sheet.entity_overrides["date_of_registration"][0] == "02/01/2024"


def test_invalid_values_are_rejected_with_every_problem_listed(tmp_path):
    engine, entity, resolved, docs = _vendor()
    path, _ = _write(tmp_path, engine, entity, resolved, docs)
    _edit(path, {"addr_ownership_type": ("freehold", ""), "com_gst_delay_days": ("ten", "")},
          entity_values={"legal_name": ("X", "")}, doc_values={"Screenshot 1.png": "selfie"})
    wb = load_workbook(path)   # smuggle in a field the report doesn't read
    ws = wb[S_ENTITY]
    ws.cell(row=ws.max_row + 1, column=1, value="gst_since")
    ws.cell(row=ws.max_row, column=3, value="2024")
    wb.save(path)
    sheet = read_review_workbook(path)
    problems = validate_review_sheet(sheet, engine.model)
    assert any("addr_ownership_type must be one of owned, leased, rented" in p for p in problems)
    assert any("com_gst_delay_days needs a number" in p for p in problems)
    assert any("'gst_since' is not a field" in p for p in problems)
    assert any("'selfie' is not a document type" in p for p in problems)
    with pytest.raises(ReviewSheetError, match="freehold"):
        apply_review_sheet(entity, resolved, sheet, engine.model, analyst="x")


def test_llm_corrections_travel_in_the_sheet_and_are_reapplied(tmp_path):
    engine, entity, resolved, docs = _vendor()
    # the reviewed run corrected the vintage; the deterministic run has the old value
    reviewed = dict(resolved)
    reviewed["com_gst_vintage"] = Resolved.ok(9.2, "llm-review", note="live GSTIN fetch: registered 2017")
    path, _ = _write(tmp_path, engine, entity, reviewed, docs, original_resolved=resolved)
    sheet = read_review_workbook(path)
    assert sheet.system_values["com_gst_vintage"][1] == "llm-review"
    fresh = dict(resolved)   # finalise re-runs deterministically: back to 2.7
    applied = apply_review_sheet(entity, fresh, sheet, engine.model, analyst="x")
    assert applied.resolved["com_gst_vintage"].value == 9.2
    assert applied.resolved["com_gst_vintage"].source.startswith("llm-review")
    assert [r["source"] for r in applied.records] == ["llm-review"]
    # ...unless the analyst overrides that row, which wins
    _edit(path, {"com_gst_vintage": (1.5, "GST certificate says registered 2025")})
    applied = apply_review_sheet(entity, dict(resolved), read_review_workbook(path), engine.model, analyst="x")
    assert applied.resolved["com_gst_vintage"].value == 1.5 and applied.resolved["com_gst_vintage"].source.startswith("analyst")


def test_stale_override_is_flagged_when_the_fresh_run_disagrees(tmp_path):
    engine, entity, resolved, docs = _vendor()
    path, _ = _write(tmp_path, engine, entity, resolved, docs)
    _edit(path, {"addr_ownership_type": ("rented", "no sale deed")})
    fresh = dict(resolved)
    fresh["addr_ownership_type"] = Resolved.ok("leased", "doc:rental_agreement present")  # new document arrived
    applied = apply_review_sheet(entity, fresh, read_review_workbook(path), engine.model, analyst="x")
    assert applied.resolved["addr_ownership_type"].value == "rented"   # analyst still wins
    assert applied.stale and "addr_ownership_type" in applied.stale[0] and "'leased'" in applied.stale[0]
    assert "system now resolves" in applied.audit_rows[0]["status"]


def test_ownership_override_rederives_rental_validation_and_landlord_noc(tmp_path):
    engine, entity, resolved, docs = _vendor()
    # deterministic run: ownership unresolved -> both derived rows unresolved
    resolved["addr_ownership_type"] = Resolved.missing("GAP: connection in a third party's name")
    resolved["addr_rental_validation"] = Resolved.missing("ownership unresolved")
    resolved["addr_landlord_declaration"] = Resolved.missing("ownership unresolved")
    path, _ = _write(tmp_path, engine, entity, resolved, docs)
    _edit(path, {"addr_ownership_type": ("rented", "rental agreement received")})
    applied = apply_review_sheet(entity, resolved, read_review_workbook(path), engine.model, analyst="x")
    assert applied.resolved["addr_rental_validation"].value == "rented_matching"   # bill address matched
    assert applied.resolved["addr_landlord_declaration"].value == "absent"          # rented, no NOC on file
    assert {a["id"] for a in applied.audit_rows} == {"addr_ownership_type", "addr_rental_validation", "addr_landlord_declaration"}
    # an explicit analyst value on a derived row wins over the re-derivation
    _edit(path, {"addr_landlord_declaration": ("present", "NOC attached")})
    applied = apply_review_sheet(entity, resolved, read_review_workbook(path), engine.model, analyst="x")
    assert applied.resolved["addr_landlord_declaration"].value == "present"


def test_audit_rows_carry_forward_into_the_next_workbook(tmp_path):
    engine, entity, resolved, docs = _vendor()
    prior = [{"when": "2026-09-17 09:00", "who": "A", "kind": "parameter", "id": "addr_ownership_type",
              "before": "owned", "after": "rented", "note": "n", "status": "applied"}]
    path, _ = _write(tmp_path, engine, entity, resolved, docs, audit_rows=prior, analyst_name="A")
    sheet = read_review_workbook(path)
    assert sheet.audit_rows == prior and sheet.analyst_name == "A"


# ---------------------------------------------------------------- layout (18-Sep: "clean, readable, no frozen panes")
def test_workbook_layout_is_readable(tmp_path):
    from vdd.review_sheet import ANALYST_CELL, C_EVIDENCE, C_HIDDEN, C_ORIG, C_SOURCE, S_FLAGS, read_review_workbook
    engine, entity, resolved, docs = _vendor()
    resolved["addr_ownership_type"] = Resolved.ok(
        "owned", "doc:electricity_bill",
        note="NO SALE DEED ON FILE -- ownership is inferred from the absence of any rental/lease agreement in the "
             "document set, corroborated by: the industrial electricity connection is in the entity's own name; "
             "the Udyam registration and the GST certificate name the same premises; no landlord is mentioned "
             "anywhere in the folder. Confirm with a sale deed or property-tax receipt if available.")
    path, _ = _write(tmp_path, engine, entity, resolved, docs)
    wb = load_workbook(path)
    # no frozen panes anywhere
    assert all(ws.freeze_panes is None for ws in wb.worksheets)
    # sheets in the order the analyst works through them
    assert wb.sheetnames[:4] == [S_REVIEW, S_ENTITY, S_DOCS, S_FLAGS]
    ws = wb[S_REVIEW]
    # the software's columns are hidden, the analyst's are not
    for c in C_HIDDEN:
        assert ws.column_dimensions[get_column_letter(c)].hidden
    for c in (C_NAME, C_EVIDENCE, C_ANALYST, C_NOTE):
        assert not ws.column_dimensions[get_column_letter(c)].hidden
    assert C_PID in C_HIDDEN and C_SOURCE in C_HIDDEN and C_ORIG in C_HIDDEN
    # a long evidence note gets a row tall enough to read it; a short one does not
    rows = [r for r in ws.iter_rows() if isinstance(r[C_PID - 1].value, str) and r[C_PID - 1].value in resolved]
    longest = max(rows, key=lambda r: len(str(r[C_EVIDENCE - 1].value or "")))
    shortest = min(rows, key=lambda r: len(str(r[C_EVIDENCE - 1].value or "")))
    assert len(str(longest[C_EVIDENCE - 1].value)) > 100
    assert ws.row_dimensions[longest[0].row].height >= 2 * 15
    assert ws.row_dimensions[shortest[0].row].height < ws.row_dimensions[longest[0].row].height
    # the analyst's name is read from the Review sheet, Meta stays as the fallback
    ws[ANALYST_CELL].value = "R. Iyer"
    wb.save(path)
    assert read_review_workbook(path).analyst_name == "R. Iyer"
    assert not ws[ANALYST_CELL].protection.locked


def test_validator_rejects_unresolved_on_proof_of_address_rows(tmp_path):
    engine, entity, resolved, docs = _vendor()
    path, _ = _write(tmp_path, engine, entity, resolved, docs)
    _edit(path, {"addr_electricity_bill": ("unresolved", "cannot tell"), "com_gstin_active": ("unresolved", "portal down")})
    problems = validate_review_sheet(read_review_workbook(path), engine.model)
    assert len(problems) == 1 and "addr_electricity_bill" in problems[0] and "cannot be 'unresolved'" in problems[0]

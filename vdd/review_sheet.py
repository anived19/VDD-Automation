"""
The analyst review workbook: an Excel file emitted next to every report that
an analyst edits in Excel and sends back, and from which the final report is
rebuilt. It is the hand-off between the automated pipeline and the human
reviewer -- no terminal, no web form, no server-side state.

    run_vendor  -> PDF + review trace + <VENDOR>_review.xlsx
    analyst     -> fills the yellow cells in Excel, saves, sends the file
    finalise    -> re-runs the deterministic pipeline (LLM review OFF), applies
                   the sheet, renders the PDF, writes the updated workbook

Sheets
  Review         one row per scored parameter: the system's value, meaning,
                 score, source and evidence, then the analyst's value (a
                 dropdown of that parameter's valid buckets) and note. Live
                 preview scores per row, per section and total, as formulas.
  Entity fields  the display fields the report reads, same two analyst columns.
  Documents      each file, the type the system assigned, a "correct type"
                 dropdown -- applied at classification time on finalise.
  Flags          what the run was unsure about: cross-check items, unresolved
                 fields, API errors, extraction warnings, LLM findings.
  Choices        the points table behind the dropdowns and preview formulas.
  Audit          every change ever applied from this workbook: who, when,
                 before, after, note. Carried forward across rounds.
  Meta           vendor, run date, scoring-model hash, the analyst's name cell.

Precedence on finalise: analyst value > LLM-review correction carried in the
sheet > the fresh deterministic value. The LLM review is not re-run -- it
already happened, and re-running it would only re-raise what the analyst
just settled -- so its corrections travel in the sheet (Source: llm-review)
and are re-applied unless the analyst overrides them.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill, Protection
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from vdd.extract.classify import DOC_TYPE_ORDER, ClassifiedDocs
from vdd.report.build_context import REPORT_ENTITY_FIELDS, _CATEGORY_CODE_PREFIX
from vdd.resolve.resolvers import Resolved
from vdd.score.engine import NO_CONSENT_CATEGORY_IDS, ScoreResult, _CMP, _EQ_NUM, _EQ_STR, _RANGE

FONT = "Arial"
UNRESOLVED = "unresolved"
IGNORE_DOC = "ignore"
SOURCE_LLM = "llm-review"
SOURCE_ANALYST = "analyst"

S_REVIEW, S_ENTITY, S_DOCS, S_FLAGS, S_CHOICES, S_AUDIT, S_META = (
    "Review", "Entity fields", "Documents", "Flags", "Choices", "Audit", "Meta")

# Review sheet columns (1-based)
C_CODE, C_PID, C_NAME, C_VALUE, C_MEANING, C_SCORE, C_MAX, C_SOURCE, C_EVIDENCE, C_ANALYST, C_NOTE, C_PREVIEW, C_ORIG = range(1, 14)
REVIEW_HEADERS = ["Code", "Parameter ID", "Parameter", "System value", "Meaning", "Score", "Max", "Source",
                  "Evidence", "Analyst value", "Analyst note", "Preview score", "Original system value"]

_LIST_FIELDS = ("partners", "declared_hsn")
_DATE_FIELDS = ("date_of_registration", "date_of_incorporation")

_FILL_INPUT = PatternFill("solid", fgColor="FFF2CC")
_FILL_HEADER = PatternFill("solid", fgColor="1F3A5F")
_FILL_SECTION = PatternFill("solid", fgColor="D9E2F3")
_FILL_SUMMARY = PatternFill("solid", fgColor="EDEDED")
_FONT_HEADER = Font(name=FONT, size=10, bold=True, color="FFFFFF")
_FONT_INPUT = Font(name=FONT, size=10, color="0000FF")
_FONT_BODY = Font(name=FONT, size=10)
_FONT_BOLD = Font(name=FONT, size=10, bold=True)
_FONT_TITLE = Font(name=FONT, size=13, bold=True)
_FONT_MUTED = Font(name=FONT, size=9, italic=True, color="666666")
_WRAP = Alignment(wrap_text=True, vertical="top")
_UNLOCKED = Protection(locked=False)


# ---------------------------------------------------------------- scoring-model view
@dataclass
class ParamSpec:
    parameter_id: str
    name: str
    category_id: str
    max_score: float
    conditional: bool
    buckets: list          # [(value, condition label, points)] -- empty for numeric params
    exprs: list            # [(expr, condition label, points)] -- for numeric params

    @property
    def numeric(self) -> bool:
        return not self.buckets


def param_specs(model: dict) -> dict[str, ParamSpec]:
    """{parameterId -> ParamSpec} for the no-consent categories, in model order."""
    out: dict[str, ParamSpec] = {}
    for cat in model["categories"]:
        if cat["categoryId"] not in NO_CONSENT_CATEGORY_IDS:
            continue
        for p in cat["parameters"]:
            buckets, exprs = [], []
            for sm in p["scoreMappings"]:
                m = _EQ_STR.match(sm["expr"])
                if m:
                    buckets.append((m.group(1).strip().lower(), sm["condition"], sm["assignedScore"]))
                else:
                    exprs.append((sm["expr"], sm["condition"], sm["assignedScore"]))
            if buckets and exprs:
                # mixed models are not something the dropdown can express; treat as numeric-free text
                buckets = []
            out[p["parameterId"]] = ParamSpec(p["parameterId"], p["parameterName"], cat["categoryId"],
                                              float(p["maxParameterScore"]), bool(p.get("conditional")),
                                              buckets, exprs)
    return out


def scoring_model_hash(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:12]


def _excel_condition(expr: str, ref: str) -> Optional[str]:
    """Translate one scoring-model expr into an Excel boolean over cell `ref`
    -- the same four forms engine.eval_expr accepts, nothing else."""
    m = _EQ_NUM.match(expr)
    if m:
        return f"{ref}={m.group(1)}"
    m = _CMP.match(expr)
    if m:
        return f"{ref}{m.group(1)}{m.group(2)}"
    m = _RANGE.match(expr)
    if m:
        lo_b, lo, hi, hi_b = m.groups()
        return f"AND({ref}{'>=' if lo_b == '[' else '>'}{lo},{ref}{'<=' if hi_b == ']' else '<'}{hi})"
    return None


def _numeric_preview(spec: ParamSpec, ref: str) -> str:
    """Nested IF over the model's numeric bands, innermost default 0."""
    formula = "0"
    for expr, _label, points in reversed(spec.exprs):
        cond = _excel_condition(expr, ref)
        if cond is None:
            continue
        formula = f"IF({cond},{points},{formula})"
    return formula


# ---------------------------------------------------------------- writing
def _fmt_value(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, (list, tuple)):
        return "; ".join(" - ".join(str(x) for x in item) if isinstance(item, (list, tuple)) else str(item) for item in v)
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _style_header(ws, row: int, ncols: int) -> None:
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.font = _FONT_HEADER
        cell.fill = _FILL_HEADER
        cell.alignment = Alignment(vertical="center", wrap_text=True)


def _input_cell(cell) -> None:
    cell.fill = _FILL_INPUT
    cell.font = _FONT_INPUT
    cell.protection = _UNLOCKED
    cell.alignment = _WRAP


def write_review_workbook(path: str, *, vendor_name: str, entity: dict, resolved: dict, result: ScoreResult,
                          docs: Optional[ClassifiedDocs], model: dict, scoring_model_path: str,
                          original_resolved: Optional[dict] = None, cross_check_items: list = (),
                          unresolved_fields: list = (), missing_documents: list = (), api_errors: list = (),
                          warnings: list = (), llm_findings: list = (), llm_corrections: list = (),
                          review_summary: str = "", report_date: Optional[str] = None,
                          audit_rows: list = (), analyst_name: str = "", document_reads: list = ()) -> str:
    """Write the workbook. `resolved` is the state the report was rendered from
    (post-LLM-review); `original_resolved` the pre-review deterministic state,
    used to show what the LLM changed and to detect stale overrides later."""
    specs = param_specs(model)
    original_resolved = original_resolved or resolved
    wb = Workbook()
    ws = wb.active
    ws.title = S_REVIEW

    # ---- title + legend
    ws["A1"] = f"VDD review -- {vendor_name}"
    ws["A1"].font = _FONT_TITLE
    ws["A2"] = ("How to use: change only the yellow cells. 'Analyst value' is a dropdown of the valid choices for that "
                "row (or a number for the two numeric rows); pick 'unresolved' to mark a parameter as not "
                "determinable. Write why in 'Analyst note' -- it is printed in the report as the evidence for your "
                "change. Put your name on the Meta sheet. Preview scores update as you edit; the final score is "
                "recomputed when the sheet is submitted.")
    ws["A2"].font = _FONT_MUTED
    ws["A2"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=C_ORIG)
    ws.row_dimensions[2].height = 48

    # ---- summary block (filled with formulas once the table rows are known)
    summary_top = 4
    ws.cell(row=summary_top, column=1, value="Section").font = _FONT_BOLD
    ws.cell(row=summary_top, column=2, value="System score").font = _FONT_BOLD
    ws.cell(row=summary_top, column=3, value="Max").font = _FONT_BOLD
    ws.cell(row=summary_top, column=4, value="Preview score").font = _FONT_BOLD
    for c in range(1, 5):
        ws.cell(row=summary_top, column=c).fill = _FILL_SUMMARY

    # ---- parameter table
    table_header = summary_top + len(result.categories) + 3
    for c, h in enumerate(REVIEW_HEADERS, start=1):
        ws.cell(row=table_header, column=c, value=h)
    _style_header(ws, table_header, len(REVIEW_HEADERS))
    ws.freeze_panes = ws.cell(row=table_header + 1, column=C_NAME + 1)

    row = table_header + 1
    category_rows: list[tuple[str, int, int, int]] = []   # (category name, header row, first param row, last param row)
    for cat in result.categories:
        prefix = _CATEGORY_CODE_PREFIX[cat.category_id]
        cat_row = row
        ws.cell(row=row, column=C_CODE, value=prefix)
        ws.cell(row=row, column=C_NAME, value=cat.category_name)
        for c in range(1, len(REVIEW_HEADERS) + 1):
            ws.cell(row=row, column=c).fill = _FILL_SECTION
            ws.cell(row=row, column=c).font = _FONT_BOLD
        row += 1
        first = row
        for i, p in enumerate(cat.params, start=1):
            spec = specs.get(p.parameter_id)
            r = resolved.get(p.parameter_id)
            o = original_resolved.get(p.parameter_id)
            source = (r.source if r is not None else "") or ""
            ws.cell(row=row, column=C_CODE, value=f"{prefix}-{i:02d}")
            ws.cell(row=row, column=C_PID, value=p.parameter_id)
            ws.cell(row=row, column=C_NAME, value=p.parameter_name)
            ws.cell(row=row, column=C_VALUE, value=_fmt_value(None if p.unresolved else p.value))
            ws.cell(row=row, column=C_MEANING, value=(UNRESOLVED if p.unresolved else (p.matched_condition or "")))
            ws.cell(row=row, column=C_SCORE, value=p.assigned_score)
            ws.cell(row=row, column=C_MAX, value=(spec.max_score if spec else p.max_score))
            ws.cell(row=row, column=C_SOURCE, value=source)
            ws.cell(row=row, column=C_EVIDENCE, value=(p.note or ""))
            ws.cell(row=row, column=C_ORIG, value=_fmt_value(None if (o is None or o.unresolved) else o.value))
            for c in (C_ANALYST, C_NOTE):
                _input_cell(ws.cell(row=row, column=c))
            ref = f"{get_column_letter(C_ANALYST)}{row}"
            if spec is not None and spec.numeric:
                preview = (f'=IF({ref}="",{get_column_letter(C_SCORE)}{row},IF(ISNUMBER({ref}),'
                           f'{_numeric_preview(spec, ref)},0))')
                dv = DataValidation(type="decimal", operator="greaterThanOrEqual", formula1="0", allow_blank=True)
                dv.error = "Enter a number (or leave blank to keep the system value)."
                dv.prompt = "A number, e.g. 3.5"
            else:
                choices = [b[0] for b in (spec.buckets if spec else [])] + [UNRESOLVED]
                preview = (f'=IF({ref}="",{get_column_letter(C_SCORE)}{row},IF(LOWER({ref})="{UNRESOLVED}",0,'
                           f'IFERROR(INDEX({S_CHOICES}!$E:$E,MATCH(${get_column_letter(C_PID)}{row}&"|"&LOWER({ref}),'
                           f'{S_CHOICES}!$A:$A,0)),0)))')
                dv = DataValidation(type="list", formula1='"' + ",".join(choices) + '"', allow_blank=True)
                dv.error = "Pick one of: " + ", ".join(choices)
                dv.prompt = "Pick a value, or leave blank to keep the system value"
            dv.errorTitle = "Not a valid value"
            dv.showErrorMessage = True
            dv.showInputMessage = True
            ws.add_data_validation(dv)
            dv.add(ref)
            ws.cell(row=row, column=C_PREVIEW, value=preview)
            for c in range(1, len(REVIEW_HEADERS) + 1):
                cell = ws.cell(row=row, column=c)
                if c not in (C_ANALYST, C_NOTE):
                    cell.font = _FONT_BODY
                    cell.alignment = _WRAP
            row += 1
        last = row - 1
        sc, mx, pv = get_column_letter(C_SCORE), get_column_letter(C_MAX), get_column_letter(C_PREVIEW)
        ws.cell(row=cat_row, column=C_SCORE, value=f"=SUM({sc}{first}:{sc}{last})")
        ws.cell(row=cat_row, column=C_MAX, value=f"=SUM({mx}{first}:{mx}{last})")
        ws.cell(row=cat_row, column=C_PREVIEW, value=f"=SUM({pv}{first}:{pv}{last})")
        category_rows.append((cat.category_name, cat_row, first, last))

    # ---- summary formulas
    srow = summary_top + 1
    for name, cat_row, _f, _l in category_rows:
        ws.cell(row=srow, column=1, value=name).font = _FONT_BODY
        ws.cell(row=srow, column=2, value=f"={get_column_letter(C_SCORE)}{cat_row}")
        ws.cell(row=srow, column=3, value=f"={get_column_letter(C_MAX)}{cat_row}")
        ws.cell(row=srow, column=4, value=f"={get_column_letter(C_PREVIEW)}{cat_row}")
        srow += 1
    ws.cell(row=srow, column=1, value="TOTAL (/100)").font = _FONT_BOLD
    ws.cell(row=srow, column=2, value=f"=SUM(B{summary_top + 1}:B{srow - 1})").font = _FONT_BOLD
    ws.cell(row=srow, column=3, value=f"=SUM(C{summary_top + 1}:C{srow - 1})").font = _FONT_BOLD
    ws.cell(row=srow, column=4, value=f"=SUM(D{summary_top + 1}:D{srow - 1})").font = _FONT_BOLD
    ws.cell(row=srow + 1, column=1, value="On-Site Verification, 3B/2B and ITR are Pending (not scored in v1).").font = _FONT_MUTED

    widths = {C_CODE: 9, C_PID: 26, C_NAME: 34, C_VALUE: 18, C_MEANING: 30, C_SCORE: 7, C_MAX: 6, C_SOURCE: 26,
              C_EVIDENCE: 60, C_ANALYST: 20, C_NOTE: 40, C_PREVIEW: 9, C_ORIG: 16}
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.protection.sheet = True
    ws.protection.formatColumns = False
    ws.protection.formatRows = False

    _write_choices(wb, specs)
    _write_entity(wb, entity)
    _write_documents(wb, docs, list(document_reads))
    _write_flags(wb, cross_check_items, unresolved_fields, missing_documents, api_errors, warnings,
                 llm_findings, llm_corrections)
    _write_audit(wb, audit_rows)
    _write_meta(wb, vendor_name, scoring_model_path, report_date, review_summary, analyst_name)

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    wb.save(path)
    return path


def _write_choices(wb: Workbook, specs: dict[str, ParamSpec]) -> None:
    ws = wb.create_sheet(S_CHOICES)
    for c, h in enumerate(["Key", "Parameter ID", "Value", "Meaning", "Points"], start=1):
        ws.cell(row=1, column=c, value=h)
    _style_header(ws, 1, 5)
    row = 2
    for spec in specs.values():
        for value, label, points in spec.buckets:
            ws.cell(row=row, column=1, value=f"{spec.parameter_id}|{value}")
            ws.cell(row=row, column=2, value=spec.parameter_id)
            ws.cell(row=row, column=3, value=value)
            ws.cell(row=row, column=4, value=label)
            ws.cell(row=row, column=5, value=points)
            row += 1
        for expr, label, points in spec.exprs:
            ws.cell(row=row, column=1, value=f"{spec.parameter_id}|{expr}")
            ws.cell(row=row, column=2, value=spec.parameter_id)
            ws.cell(row=row, column=3, value=expr)
            ws.cell(row=row, column=4, value=label)
            ws.cell(row=row, column=5, value=points)
            row += 1
    for r in ws.iter_rows(min_row=2, max_row=row):
        for cell in r:
            cell.font = _FONT_BODY
    for col, w in zip("ABCDE", (40, 26, 24, 44, 8)):
        ws.column_dimensions[col].width = w
    ws.protection.sheet = True


def _write_entity(wb: Workbook, entity: dict) -> None:
    ws = wb.create_sheet(S_ENTITY)
    ws["A1"] = "Display fields the report reads. Change only the yellow cells; dates as dd/mm/yyyy; lists separated by ';'."
    ws["A1"].font = _FONT_MUTED
    for c, h in enumerate(["Field", "System value", "Analyst value", "Analyst note"], start=1):
        ws.cell(row=2, column=c, value=h)
    _style_header(ws, 2, 4)
    row = 3
    for f in REPORT_ENTITY_FIELDS:
        ws.cell(row=row, column=1, value=f).font = _FONT_BODY
        v = ws.cell(row=row, column=2, value=_fmt_value(entity.get(f)))
        v.font = _FONT_BODY
        v.alignment = _WRAP
        for c in (3, 4):
            cell = ws.cell(row=row, column=c)
            _input_cell(cell)
            cell.number_format = "@"
        row += 1
    for col, w in zip("ABCD", (34, 60, 40, 40)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A3"
    ws.protection.sheet = True
    ws.protection.formatColumns = False


def _write_documents(wb: Workbook, docs: Optional[ClassifiedDocs], reads: Optional[list] = None) -> None:
    ws = wb.create_sheet(S_DOCS)
    ws["A1"] = ("Each file, the document type the system assigned, and how well it was read. To correct a type, "
                "pick the right one under 'Correct type' ('ignore' = not a KYC document). Applied when the sheet "
                "is submitted. A POOR/PARTIAL read lists the fields that could not be recovered -- re-scan or "
                "re-upload if they matter.")
    ws["A1"].font = _FONT_MUTED
    for c, h in enumerate(["File", "Detected type", "Correct type", "Note", "Read quality", "Read via", "Missing fields"], start=1):
        ws.cell(row=2, column=c, value=h)
    _style_header(ws, 2, 7)
    quality = {r.name: r for r in (reads or [])}
    rows = []
    if docs is not None:
        for doc_type, paths in docs.by_type.items():
            rows += [(os.path.basename(p), doc_type) for p in paths]
        rows += [(os.path.basename(p), "unmatched") for p in docs.unmatched]
    rows.sort(key=lambda r: r[0].lower())
    dv = DataValidation(type="list", formula1='"' + ",".join(list(DOC_TYPE_ORDER) + [IGNORE_DOC]) + '"',
                        allow_blank=True)
    dv.error = "Pick a document type from the list"
    dv.errorTitle = "Not a document type"
    dv.showErrorMessage = True
    ws.add_data_validation(dv)
    row = 3
    for fname, dtype in rows:
        ws.cell(row=row, column=1, value=fname).font = _FONT_BODY
        ws.cell(row=row, column=2, value=dtype).font = _FONT_BODY
        for c in (3, 4):
            _input_cell(ws.cell(row=row, column=c))
        dv.add(f"C{row}")
        r = quality.get(fname)
        if r is not None:
            q = ws.cell(row=row, column=5, value=r.quality.upper())
            q.font = Font(name=FONT, size=10, bold=r.quality != "read",
                          color={"read": "1E7F4F", "partial": "B7791F", "poor": "B42318", "unreadable": "B42318"}[r.quality])
            ws.cell(row=row, column=6, value=r.method).font = _FONT_BODY
            # For a file that could not be read at all, the reason is more
            # useful than a list of every field it would have carried.
            detail = r.reason if (r.quality == "unreadable" and r.reason) else ", ".join(r.missing)
            ws.cell(row=row, column=7, value=detail).font = _FONT_BODY
        elif dtype == "unmatched":
            ws.cell(row=row, column=5, value="UNMATCHED").font = Font(name=FONT, size=10, bold=True, color="B7791F")
        row += 1
    for col, w in zip("ABCDEFG", (60, 22, 22, 40, 13, 16, 40)):
        ws.column_dimensions[col].width = w
    ws.protection.sheet = True
    ws.protection.formatColumns = False


def _write_flags(wb: Workbook, cross_check_items, unresolved_fields, missing_documents, api_errors, warnings,
                 llm_findings, llm_corrections) -> None:
    ws = wb.create_sheet(S_FLAGS)
    ws["A1"] = "What the system was unsure about in this run. Read-only."
    ws["A1"].font = _FONT_MUTED
    row = 3
    sections = [
        ("Missing document types", list(missing_documents)),
        ("Unresolved scoring fields", list(unresolved_fields)),
        ("Needs manual cross-check", list(cross_check_items)),
        ("LLM review -- corrections applied",
         [f"{f.get('parameter_id') or f.get('field')}: {f.get('issue', '')} -- {f.get('proposed_note', '')}" for f in llm_corrections]),
        ("LLM review -- escalated for analyst",
         [f"{f.get('parameter_id') or f.get('field')}: {f.get('issue', '')}" for f in llm_findings]),
        ("API errors", list(api_errors)),
        ("Extraction warnings", list(warnings)),
    ]
    for title, items in sections:
        ws.cell(row=row, column=1, value=title).font = _FONT_BOLD
        ws.cell(row=row, column=1).fill = _FILL_SECTION
        row += 1
        if not items:
            ws.cell(row=row, column=1, value="(none)").font = _FONT_MUTED
            row += 1
        for item in items:
            cell = ws.cell(row=row, column=1, value=str(item))
            cell.font = _FONT_BODY
            cell.alignment = _WRAP
            row += 1
        row += 1
    ws.column_dimensions["A"].width = 140
    ws.protection.sheet = True


AUDIT_HEADERS = ["When", "Who", "Kind", "Id", "Before", "After", "Note", "Status"]


def _write_audit(wb: Workbook, audit_rows) -> None:
    ws = wb.create_sheet(S_AUDIT)
    for c, h in enumerate(AUDIT_HEADERS, start=1):
        ws.cell(row=1, column=c, value=h)
    _style_header(ws, 1, len(AUDIT_HEADERS))
    for i, r in enumerate(audit_rows, start=2):
        for c, h in enumerate(AUDIT_HEADERS, start=1):
            cell = ws.cell(row=i, column=c, value=_fmt_value(r.get(h.lower())))
            cell.font = _FONT_BODY
            cell.alignment = _WRAP
    for col, w in zip("ABCDEFGH", (18, 18, 10, 28, 22, 22, 50, 22)):
        ws.column_dimensions[col].width = w
    ws.protection.sheet = True


META_KEYS = ("Vendor", "Run date", "Report date", "Scoring model hash", "LLM review", "Analyst name")


def _write_meta(wb: Workbook, vendor_name: str, scoring_model_path: str, report_date: Optional[str],
                review_summary: str, analyst_name: str) -> None:
    ws = wb.create_sheet(S_META)
    values = {
        "Vendor": vendor_name,
        "Run date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "Report date": report_date or date.today().strftime("%d %B %Y"),
        "Scoring model hash": scoring_model_hash(scoring_model_path) if os.path.exists(scoring_model_path) else "",
        "LLM review": review_summary or "not run",
        "Analyst name": analyst_name,
    }
    for i, k in enumerate(META_KEYS, start=1):
        ws.cell(row=i, column=1, value=k).font = _FONT_BOLD
        cell = ws.cell(row=i, column=2, value=values[k])
        cell.font = _FONT_BODY
        if k == "Analyst name":
            _input_cell(cell)
    ws.cell(row=len(META_KEYS) + 2, column=1,
            value="Yellow cells are yours to edit. Everything else is regenerated when the sheet is submitted.").font = _FONT_MUTED
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 60
    ws.protection.sheet = True


# ---------------------------------------------------------------- reading
@dataclass
class ReviewSheet:
    path: str
    vendor_name: str = ""
    analyst_name: str = ""
    model_hash: str = ""
    # parameter id -> (analyst value as entered, note)
    param_overrides: dict = field(default_factory=dict)
    # parameter id -> (system value at issue, source at issue, original deterministic value at issue)
    system_values: dict = field(default_factory=dict)
    entity_overrides: dict = field(default_factory=dict)     # field -> (value, note)
    entity_system: dict = field(default_factory=dict)        # field -> system value at issue
    doc_overrides: dict = field(default_factory=dict)        # filename -> type
    audit_rows: list = field(default_factory=list)


class ReviewSheetError(ValueError):
    """The sheet cannot be applied as-is; the message lists every problem."""


def _cell_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%d/%m/%Y")
    if isinstance(v, date):
        return v.strftime("%d/%m/%Y")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def read_review_workbook(path: str) -> ReviewSheet:
    wb = load_workbook(path)   # formulas kept as strings; analyst cells are plain values either way
    sheet = ReviewSheet(path=path)
    if S_META in wb.sheetnames:
        meta = {_cell_str(r[0].value): _cell_str(r[1].value) for r in wb[S_META].iter_rows(min_row=1, max_row=len(META_KEYS), max_col=2)}
        sheet.vendor_name = meta.get("Vendor", "")
        sheet.analyst_name = meta.get("Analyst name", "")
        sheet.model_hash = meta.get("Scoring model hash", "")
    ws = wb[S_REVIEW]
    for r in ws.iter_rows(min_row=1, max_col=C_ORIG):
        pid = _cell_str(r[C_PID - 1].value)
        # Only parameter rows carry an id here; the summary block's formulas and
        # the header text also land in this column and must be skipped.
        if not re.fullmatch(r"[a-z][a-z0-9_]*", pid):
            continue
        sheet.system_values[pid] = (r[C_VALUE - 1].value, _cell_str(r[C_SOURCE - 1].value), r[C_ORIG - 1].value)
        raw = r[C_ANALYST - 1].value
        if raw is not None and _cell_str(raw) != "":
            sheet.param_overrides[pid] = (raw, _cell_str(r[C_NOTE - 1].value))
    if S_ENTITY in wb.sheetnames:
        for r in wb[S_ENTITY].iter_rows(min_row=3, max_col=4):
            f = _cell_str(r[0].value)
            if not f:
                continue
            sheet.entity_system[f] = _cell_str(r[1].value)
            if r[2].value is not None and _cell_str(r[2].value) != "":
                sheet.entity_overrides[f] = (_cell_str(r[2].value), _cell_str(r[3].value))
    if S_DOCS in wb.sheetnames:
        for r in wb[S_DOCS].iter_rows(min_row=3, max_col=4):
            fname = _cell_str(r[0].value)
            if fname and r[2].value is not None and _cell_str(r[2].value) != "":
                sheet.doc_overrides[fname] = _cell_str(r[2].value).lower()
    if S_AUDIT in wb.sheetnames:
        for r in wb[S_AUDIT].iter_rows(min_row=2, max_col=len(AUDIT_HEADERS)):
            if all(c.value is None for c in r):
                continue
            sheet.audit_rows.append({h.lower(): _cell_str(c.value) for h, c in zip(AUDIT_HEADERS, r)})
    return sheet


# ---------------------------------------------------------------- validating + applying
def validate_review_sheet(sheet: ReviewSheet, model: dict) -> list[str]:
    specs = param_specs(model)
    problems = []
    for pid, (raw, _note) in sheet.param_overrides.items():
        spec = specs.get(pid)
        if spec is None:
            problems.append(f"Review: '{pid}' is not a scored parameter")
            continue
        text = _cell_str(raw).lower()
        if text == UNRESOLVED:
            continue
        if spec.numeric:
            try:
                float(text)
            except ValueError:
                problems.append(f"Review: {pid} needs a number (or 'unresolved'), got {raw!r}")
        elif text not in [b[0] for b in spec.buckets]:
            problems.append(f"Review: {pid} must be one of {', '.join(b[0] for b in spec.buckets)} or "
                            f"'unresolved', got {raw!r}")
    for f in sheet.entity_overrides:
        if f not in REPORT_ENTITY_FIELDS:
            problems.append(f"Entity fields: '{f}' is not a field the report reads")
    for fname, dtype in sheet.doc_overrides.items():
        if dtype not in DOC_TYPE_ORDER and dtype != IGNORE_DOC:
            problems.append(f"Documents: '{fname}' -> '{dtype}' is not a document type")
    return problems


def _coerce_entity_value(field_name: str, text: str) -> Any:
    if field_name == "partners":
        return [p.strip() for p in text.split(";") if p.strip()]
    if field_name == "declared_hsn":
        pairs = []
        for item in text.split(";"):
            item = item.strip()
            if not item:
                continue
            code, _, desc = item.partition(" - ")
            pairs.append((code.strip(), desc.strip()))
        return pairs
    return text


_DERIVED_FROM_OWNERSHIP = ("addr_rental_validation", "addr_landlord_declaration")


def _rederive_address_chain(resolved: dict, sheet: ReviewSheet, out: "AppliedSheet", audit) -> None:
    """addr_rental_validation and addr_landlord_declaration are derived from
    addr_ownership_type (+ the electricity-bill match), so an override of
    either input re-derives them with the same resolvers the pipeline uses --
    unless the analyst set the derived row explicitly, which wins."""
    from vdd.resolve.resolvers import resolve_addr_landlord_declaration, resolve_addr_rental_validation
    if not any(pid in sheet.param_overrides for pid in ("addr_ownership_type", "addr_electricity_bill")):
        return
    ownership = resolved.get("addr_ownership_type") or Resolved.missing("")
    electricity = resolved.get("addr_electricity_bill") or Resolved.missing("")
    has_landlord_doc = (resolved.get("addr_landlord_declaration") or Resolved.missing("")).value == "present"
    derived = {"addr_rental_validation": resolve_addr_rental_validation(ownership, electricity),
               "addr_landlord_declaration": resolve_addr_landlord_declaration(ownership, has_landlord_doc)}
    for pid, new in derived.items():
        if pid in sheet.param_overrides:
            continue
        old = resolved.get(pid)
        old_value = None if old is None or old.unresolved else old.value
        new_value = None if new.unresolved else new.value
        if _fmt_value(old_value) == _fmt_value(new_value):
            continue
        resolved[pid] = new
        audit("parameter", pid, old_value, new_value, "re-derived from the analyst's ownership / address change", "applied")


@dataclass
class AppliedSheet:
    entity: dict
    resolved: dict
    records: list = field(default_factory=list)      # same shape as the LLM's corrections_applied records
    audit_rows: list = field(default_factory=list)
    stale: list = field(default_factory=list)        # cross-check lines


def apply_review_sheet(entity: dict, resolved: dict, sheet: ReviewSheet, model: dict, *,
                       analyst: str, when: Optional[str] = None) -> AppliedSheet:
    """Analyst value > LLM correction carried in the sheet > fresh deterministic
    value. `entity`/`resolved` are the fresh deterministic state and are not
    mutated. Raises ReviewSheetError if validate_review_sheet finds anything."""
    problems = validate_review_sheet(sheet, model)
    if problems:
        raise ReviewSheetError("The review sheet has problems that must be fixed before it can be applied:\n  - "
                               + "\n  - ".join(problems))
    when = when or datetime.now().strftime("%Y-%m-%d %H:%M")
    specs = param_specs(model)
    entity = dict(entity)
    resolved = dict(resolved)
    out = AppliedSheet(entity=entity, resolved=resolved)

    def audit(kind, ident, before, after, note, status):
        out.audit_rows.append({"when": when, "who": analyst, "kind": kind, "id": ident, "before": _fmt_value(before),
                               "after": _fmt_value(after), "note": note, "status": status})

    for pid, (issued_value, issued_source, issued_original) in sheet.system_values.items():
        spec = specs.get(pid)
        fresh = resolved.get(pid)
        fresh_value = None if fresh is None or fresh.unresolved else fresh.value
        if pid in sheet.param_overrides:
            raw, note = sheet.param_overrides[pid]
            text = _cell_str(raw).lower()
            source = f"{SOURCE_ANALYST}: {analyst}, {when}"
            if text == UNRESOLVED:
                new = Resolved.missing(note or "Marked unresolved by the analyst", source=source)
                new_value = None
            else:
                value = float(text) if spec and spec.numeric else text
                new = Resolved.ok(value, source, note=note or "Set by the analyst")
                new_value = value
            status = "applied"
            if _fmt_value(fresh_value) != _fmt_value(issued_original if issued_original not in (None, "") else issued_value):
                status = f"applied; system now resolves {_fmt_value(fresh_value)!r} (was {_fmt_value(issued_value)!r} when the sheet was issued)"
                out.stale.append(f"{pid}: analyst set {_fmt_value(new_value)!r}, but the fresh run resolves "
                                 f"{_fmt_value(fresh_value)!r} -- the override may be stale; re-check")
            resolved[pid] = new
            out.records.append({"parameter_id": pid, "issue": note or "analyst override", "proposed_value": new_value,
                                "proposed_note": note, "action": "correct", "confidence": "verified",
                                "before": {"value": fresh_value, "note": fresh.note if fresh else ""}, "source": SOURCE_ANALYST})
            audit("parameter", pid, fresh_value, new_value, note, status)
        elif issued_source.startswith(SOURCE_LLM) and issued_value not in (None, ""):
            value = float(issued_value) if spec and spec.numeric else _cell_str(issued_value).lower()
            if _fmt_value(fresh_value) == _fmt_value(value):
                continue
            note = fresh.note if fresh else ""
            resolved[pid] = Resolved.ok(value, f"{SOURCE_LLM} (carried from the reviewed run)", note=note)
            out.records.append({"parameter_id": pid, "issue": "LLM review correction carried forward",
                                "proposed_value": value, "proposed_note": note, "action": "correct",
                                "confidence": "verified", "before": {"value": fresh_value, "note": note}, "source": SOURCE_LLM})
            audit("parameter", pid, fresh_value, value, "carried from the LLM-reviewed run", "applied")

    _rederive_address_chain(resolved, sheet, out, audit)

    for f, (text, note) in sheet.entity_overrides.items():
        before = entity.get(f)
        value = _coerce_entity_value(f, text)
        entity[f] = value
        out.records.append({"field": f, "issue": note or "analyst override", "proposed_value": value, "proposed_note": note,
                            "action": "correct", "confidence": "verified", "before": {"value": before}, "source": SOURCE_ANALYST})
        audit("field", f, before, value, note, "applied")

    for fname, dtype in sheet.doc_overrides.items():
        audit("document", fname, "", dtype, "", "applied at classification")
    return out

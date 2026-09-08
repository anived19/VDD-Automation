"""
Assemble the context dict `render.build()` expects, from resolved
scoring-model values + doc-extracted entity data -- the replacement for
the original `finoscale-vdd-report.skill`'s docx-parsing `parse()` step.

Honesty rule: never claim something is "Verified" unless we actually have
a resolved, positive value for it. Fields the pipeline couldn't resolve
render as an explicit "Not yet verified" / "Pending automated check" note,
never as a silently-assumed pass -- this is a KYC/compliance report.
"""
import re
from datetime import date, datetime
from typing import Optional

from vdd.report.render import esc, location as _location, clean_bank
from vdd.score.engine import ScoreResult, CategoryScore, ParamScore

_CATEGORY_CODE_PREFIX = {
    "compliance": "COM",
    "proof_of_address": "POA",
    "proof_of_identity": "POI",
    "legal_aml": "AML",
}

_ENTITY_TYPE_LABEL = {
    "pvt_public_listed": "Private/Public Limited",
    "llp": "LLP",
    "partnership_proprietorship": "Partnership/Proprietorship",
}


_EVIDENCE_STYLE = ("display:block;margin-top:3px;font-size:9.5px;line-height:1.45;"
                    "color:rgba(255,255,255,.42);font-weight:400")


def _headline(note: str) -> str:
    """First sentence of a resolver note, used as the row's headline when there
    is no scored condition label to show."""
    note = note.strip()
    m = re.match(r'(.{0,120}?[.;])\s', note)
    return (m.group(1) if m else (note[:120] + ("..." if len(note) > 120 else ""))).strip()


def _code_rows(cat: CategoryScore) -> list:
    """(code, parameter, result-html). `result-html` is a headline plus, when the
    resolver recorded one, a muted evidence/reason sub-line -- the audit trail is
    the point of this annex, so a row never shows a bare verdict when the
    resolver had a caveat to attach (e.g. POA-01 prints the scoring model's
    "Owned -- confirmed via sale deed" label, and the sub-line has to say that
    no sale deed is actually on file)."""
    prefix = _CATEGORY_CODE_PREFIX[cat.category_id]
    rows = []
    for i, p in enumerate(cat.params, start=1):
        code = f"{prefix}-{i:02d}"
        detail = ""
        if p.unresolved:
            if p.note:
                head, rest = _headline(p.note), ""
                if len(p.note.strip()) > len(head):
                    rest = p.note.strip()[len(head):].strip()
                result = esc(head)
                detail = rest
            else:
                result = ("N/A &mdash; not applicable" if p.max_score == 0 else
                           "Not yet verified &mdash; pending automated check")
        else:
            result = esc(p.matched_condition or str(p.value))
            detail = p.note or ""
        if detail:
            result += f'<span style="{_EVIDENCE_STYLE}">{esc(detail)}</span>'
        rows.append((code, esc(p.parameter_name), result))
    return rows


def _cat_by_id(result: ScoreResult, cat_id: str) -> CategoryScore:
    return next(c for c in result.categories if c.category_id == cat_id)


def _param(cat: CategoryScore, param_id: str) -> Optional[ParamScore]:
    return next((p for p in cat.params if p.parameter_id == param_id), None)


def _status_pill(label: str, ok: Optional[bool]) -> str:
    if ok is True:
        return f'<span class="status-pill pill-green">{esc(label)}</span>'
    if ok is False:
        return f'<span class="status-pill pill-amber">{esc(label)}</span>'
    return f'<span class="status-pill pill-amber">{esc(label)} &middot; Not Verified</span>'


def _findings(result: ScoreResult, entity: dict = None) -> list:
    """(kind, text) pairs -- 'c' = confirmed (green), 'n' = note/caution (amber)."""
    entity = entity or {}
    udyam_number = entity.get("udyam_number")
    com, poa, poi, aml = (_cat_by_id(result, c) for c in
                           ("compliance", "proof_of_address", "proof_of_identity", "legal_aml"))
    out = []

    def add(cat: CategoryScore, param_id: str, confirmed_tpl: str, note_tpl: str, pending_tpl: str = None):
        p = _param(cat, param_id)
        if p is None:
            return
        if p.unresolved:
            if p.max_score > 0:  # only surface genuinely-required-but-missing fields as notes
                out.append(("n", pending_tpl or f"{esc(p.parameter_name)} &mdash; not yet automatically verified"))
            return
        # NB: compare on an explicit boolean, not `tpl is confirmed_tpl`. When the
        # two templates are identical string literals CPython interns them into a
        # single object, so the identity test was silently reporting 0-scoring
        # parameters (e.g. "Cancelled registrations present") as green ticks.
        ok = p.assigned_score >= p.max_score * 0.6
        out.append(("c" if ok else "n",
                    (confirmed_tpl if ok else note_tpl).format(v=esc(p.matched_condition or p.value))))

    _since = entity.get("date_of_registration")
    _tt = entity.get("taxpayer_type")
    add(com, "com_gstin_active",
        "Active GST registration" + (f" ({esc(_since)})" if _since else "")
        + (f", {esc(_tt)} taxpayer status" if _tt else "") + " &mdash; statutory compliance confirmed",
        "GST registration status: {v}")
    _vp = _vintage_phrase(_since or "")
    add(com, "com_gst_vintage", "GST vintage: " + (esc(_vp) if _vp else "{v}"),
        "Limited GST track record &mdash; {v}")
    add(com, "com_gst_filing_compliance", "GST 3B &amp; R1 filings: {v}", "GST 3B &amp; R1 filing shows {v}")
    add(com, "com_filing_frequency", "Filing frequency: {v}", "Filing frequency: {v} &mdash; quarterly/QRMP")
    mr = _param(com, "com_multiple_registrations")
    if mr is not None and not mr.unresolved and mr.value == "cancelled_present":
        out.append(("n", "Cancelled GST registrations found on the entity PAN &mdash; "
                          + esc(mr.note or "see scoring detail")))
    else:
        add(com, "com_multiple_registrations", "No additional registrations on entity PAN &mdash; "
            "single clean registration", "GST registrations on PAN: {v}",
            pending_tpl="Additional GST registrations on the entity PAN could not be classified as "
                        "cancelled or merely inactive &mdash; needs a manual gst.gov.in check")
    add(com, "com_hsn_match", "HSN codes match the declared product category", "HSN code check: {v}")
    add(com, "com_gst_delay_days", "No GST filing delay (&le;10 days) &mdash; satisfactory compliance behaviour",
        "Maximum GST filing delay: {v} days")
    add(com, "com_bank_verification", "Bank penny-drop successful and account matches GST records",
        "Bank account verification: {v}",
        pending_tpl="Bank account not independently verified &mdash; no penny drop performed and the GST "
                    "portal's own account-validation status is not positive; see COM-07 for the evidence trail")
    add(com, "com_pf_filing_status", "PF/EPFO returns filed on time &mdash; labour compliance", "PF/EPFO status: {v}")
    own = _param(poa, "addr_ownership_type")
    if own is not None and not own.unresolved:
        if own.value == "owned" and "NO SALE DEED" in (own.note or ""):
            # Don't repeat the scoring model's "confirmed via sale deed" wording when
            # no sale deed is actually on file -- say what was really relied on.
            out.append(("c", "Business premises owned &mdash; inferred (no rental/lease agreement on file; "
                              "industrial electricity connection held in the entity's own name). Sale deed "
                              "not on file."))
        else:
            out.append(("c" if own.assigned_score >= own.max_score * 0.6 else "n",
                        f"Business premises: {esc(own.matched_condition or own.value)}"))
    add(poa, "addr_electricity_bill", "Electricity bill address matches the GST-registered address",
        "Electricity bill address check: {v}")
    add(poa, "addr_msme", "MSME (Udyam) registration valid &amp; active"
        + (f" &mdash; {esc(entity_udyam)}" if (entity_udyam := udyam_number) else ""), "MSME/Udyam status: {v}")
    pa, pnm = _param(poi, "ident_pan_active"), _param(poi, "ident_pan_name_match")
    both_ok = (pa is not None and not pa.unresolved and pa.value == "active"
               and pnm is not None and not pnm.unresolved and pnm.value == "match")
    if both_ok:
        out.append(("c", "PAN active and name matches GST legal name across sources"))
    else:
        add(poi, "ident_pan_active", "PAN active and valid", "PAN status: {v}")
        add(poi, "ident_pan_name_match", "PAN name matches the GST legal name across sources",
            "PAN name vs GST legal name: {v} &mdash; discrepancy, warrants clarification")

    aml_resolved = [p for p in aml.params if not p.unresolved]
    aml_missing = [p for p in aml.params if p.unresolved]
    # See _chips()'s identical check -- a Zigram hit outside the 5 scored AML
    # parameters (e.g. an ESIC Defaulters List match) never lowers any
    # parameter's score, but "clean" language must not be shown over it.
    aml_other_finding = any("ADDITIONAL ZIGRAM FINDING" in (p.note or "") for p in aml.params)
    if aml_other_finding:
        other_note = next(p.note for p in aml.params if "ADDITIONAL ZIGRAM FINDING" in (p.note or ""))
        extra = other_note.split("ADDITIONAL ZIGRAM FINDING", 1)[1].split(":", 1)[-1].strip(" .:")
        out.append(("n", f"Zigram screening found an adverse record outside this report's 5 scored "
                          f"AML parameters &mdash; NOT reflected in the AML score above: {esc(extra[:280])}"))
    if aml_resolved and not aml_missing and not aml_other_finding \
            and all(p.assigned_score >= p.max_score for p in aml_resolved if p.max_score):
        out.append(("c", "Clean Legal/AML &mdash; no sanctions, PEP, wilful-defaulter, eCourt or DRT/SARFAESI "
                          f"records ({int(aml.earned)}/{int(aml.max_score) if aml.max_score else 5})"))
    elif aml_resolved and all(p.assigned_score >= p.max_score for p in aml_resolved if p.max_score):
        names = ", ".join(esc(p.parameter_name.split("&mdash;")[0].split("—")[0].strip())
                          for p in aml_resolved)
        out.append(("c", f"Clean on the Legal/AML registers actually screened ({names})"))
    if aml_missing:
        out.append(("n", f"{len(aml_missing)} of {len(aml.params)} Legal/AML registers could not be screened "
                          "automatically this run &mdash; NOT counted as clean; see AML rows for why"))

    return out


def _parse_dmy(s: str) -> Optional[date]:
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except (ValueError, AttributeError):
            continue
    return None


def _vintage_phrase(from_date: str, as_of: date = None) -> Optional[str]:
    """'~9 years 2 months' -- the human-readable vintage the reference report
    shows, rather than the scoring band label ('> 5 years')."""
    d = _parse_dmy(from_date or "")
    if d is None:
        return None
    today = as_of or date.today()
    months = (today.year - d.year) * 12 + (today.month - d.month) - (1 if today.day < d.day else 0)
    if months < 0:
        return None
    y, m = divmod(months, 12)
    parts = []
    if y:
        parts.append(f"{y} year{'s' if y != 1 else ''}")
    if m or not y:
        parts.append(f"{m} month{'s' if m != 1 else ''}")
    return "~" + " ".join(parts)


def _nature_of_business(entity: dict) -> str:
    """Prefer the Udyam NIC 5-digit description (the most specific statutory
    statement of what the enterprise makes) over MAJOR ACTIVITY's coarse
    'Manufacturing', and cross-reference the electricity bill's independently
    declared connection activity when the two agree."""
    nic_desc = (entity.get("nic_5_description") or "").strip().rstrip('.')
    nic_code = entity.get("nic_5_code")
    activity = (entity.get("electricity_bill_activity") or "").strip()
    if not nic_desc:
        return (entity.get("gst_nature_of_business_activity") or entity.get("nature_of_business")
                or entity.get("major_activity") or "N/A")
    base = nic_desc[0].upper() + nic_desc[1:]
    if nic_code:
        base += f" (NIC {nic_code})"
    if activity:
        base += f", matching the electricity bill's declared '{activity.title()}' activity"
    return base


def _profile_paragraph(entity: dict, trade: str, entity_type: str, nature: str, loc: str,
                        vintage_text: str, poa: CategoryScore) -> str:
    """Narrative profile. Deliberately richer than a field dump: it states the
    NIC-code activity and its independent corroboration from the electricity
    bill, and keeps the GST-anchored vintage clearly distinct from the entity's
    true formation date (Custom Instructions rule 3 -- "phrase narrative text so
    'incorporated in X' isn't conflated with the GST vintage year")."""
    parts = []
    activity = (entity.get("electricity_bill_activity") or "").strip()
    nic_desc = (entity.get("nic_5_description") or "").strip().rstrip('.')
    lead = f"{esc(trade)} is a {esc(entity_type).lower()} engaged in "
    if nic_desc:
        lead += esc(nic_desc.lower())
        if entity.get("nic_5_code"):
            lead += f" (NIC {esc(entity['nic_5_code'])})"
        if activity:
            lead += (f", matching the electricity bill's independently declared "
                     f"'{esc(activity.title())}' connection activity")
    else:
        lead += esc(str(nature).lower())
    parts.append(lead + ".")

    state = entity.get("state") or loc
    reg = entity.get("date_of_registration")
    vintage_sentence = f"The entity is based in {esc(state)}"
    if reg and vintage_text and vintage_text != "N/A":
        vintage_sentence += f" and has held this GSTIN for {esc(vintage_text)} (GST valid from {esc(reg)})"
    formation = entity.get("date_of_incorporation")
    if formation and formation != reg:
        age = _vintage_phrase(formation)
        vintage_sentence += (f"; the underlying firm itself dates to {esc(formation)} per its Udyam "
                             f"registration" + (f" ({esc(age)})" if age else ""))
    parts.append(vintage_sentence + ".")

    msme = _param(poa, "addr_msme")
    if msme is not None and not msme.unresolved and msme.value == "valid_active":
        cls = entity.get("enterprise_type")
        parts.append("MSME (Udyam) registration is valid and active"
                      + (f" under {esc(cls)} classification." if cls else "."))

    turnover = entity.get("gst_annual_aggregate_turnover")
    if turnover:
        yr = entity.get("gst_annual_aggregate_turnover_year")
        slab = re.sub(r'^\s*slab\s*:\s*', '', str(turnover), flags=re.I).strip()
        parts.append(f"GST records place annual aggregate turnover in the {esc(slab)} band"
                      + (f" for {esc(yr)}." if yr else "."))

    tail = f"The entity operates under GSTIN {esc(entity.get('gstin', 'N/A'))}"
    tail += (f" with {esc(entity.get('taxpayer_type'))} taxpayer status."
             if entity.get("taxpayer_type") else ".")
    parts.append(tail)
    return " ".join(parts)


def _chips(entity: dict, result: ScoreResult, loc: str, seller_type: str) -> list:
    """(kind, label) header chips. Every claim here has to be earned from a
    resolved value -- these were previously hardcoded to 'AML Cleared / GST
    Active / Bank Verified' and so asserted all three on every report,
    including ones where the pipeline had resolved the opposite."""
    com, poa, poi, aml = (_cat_by_id(result, c) for c in
                           ("compliance", "proof_of_address", "proof_of_identity", "legal_aml"))
    chips = []

    aml_missing = [p for p in aml.params if p.unresolved]
    aml_hits = [p for p in aml.params if not p.unresolved and p.max_score and p.assigned_score < p.max_score]
    # A Zigram hit outside this pipeline's 5 scored AML parameters (e.g. an ESIC
    # Defaulters List match -- real, government-sourced, but not a sanctions/PEP/
    # wilful-defaulter/eCourts/DRT hit specifically) never lowers any parameter's
    # score (see resolve_aml()), but a "Cleared" badge must never be shown over a
    # known real adverse finding just because it doesn't fit one of those 5 slots.
    aml_other_finding = any("ADDITIONAL ZIGRAM FINDING" in (p.note or "") for p in aml.params)
    if aml_hits or aml_other_finding:
        chips.append(("amber", "! AML Adverse Finding"))
    elif aml_missing:
        chips.append(("amber", f"AML Partially Screened ({len(aml.params) - len(aml_missing)}/{len(aml.params)})"))
    else:
        chips.append(("teal", "&#10003; AML Cleared"))

    gst = _param(com, "com_gstin_active")
    if gst is not None and not gst.unresolved and gst.value == "active":
        chips.append(("teal", "&#10003; GST Active"))
    else:
        chips.append(("amber", "GST Status Unconfirmed" if gst is None or gst.unresolved
                       else f"GST {esc(str(gst.value).capitalize())}"))

    bank = _param(com, "com_bank_verification")
    if bank is not None and not bank.unresolved and bank.assigned_score > 0:
        chips.append(("blue", "&#10003; Bank Verified"))
    else:
        chips.append(("amber", "Bank Not Verified"))

    pan = _param(poi, "ident_pan_active")
    if pan is not None and not pan.unresolved and pan.value == "active":
        chips.append(("white", "&#10003; PAN Active"))
    chips.append(("white", f"@LOC@{esc(loc)}"))
    chips.append(("white", esc(seller_type)))
    return chips


def build_context(entity: dict, result: ScoreResult, report_date: str = None) -> dict:
    com = _cat_by_id(result, "compliance")
    poa = _cat_by_id(result, "proof_of_address")
    poi = _cat_by_id(result, "proof_of_identity")
    aml = _cat_by_id(result, "legal_aml")

    trade = entity.get("trade_name") or entity.get("legal_name") or "UNKNOWN ENTITY"
    legal = entity.get("legal_name") or trade
    constitution_param = _param(poi, "ident_constitution")
    # Show the entity's *actual* constitution from the GST certificate ("Partnership")
    # rather than the scoring bucket's label ("Partnership/Proprietorship"), which
    # names a category of three unrelated entity types.
    entity_type = entity.get("constitution") or (
        _ENTITY_TYPE_LABEL.get(constitution_param.value, "")
        if constitution_param and not constitution_param.unresolved else "") or "N/A"

    vintage_param = _param(com, "com_gst_vintage")
    vintage_band = (vintage_param.matched_condition if vintage_param and not vintage_param.unresolved
                     else None)
    vintage_text = _vintage_phrase(entity.get("date_of_registration", "")) or vintage_band or "N/A"

    # Custom Instructions rule 3: the Year Incorporated field and the vintage
    # score stay anchored to the GST registration date. The true formation date
    # (Udyam / PAN) is recorded separately and only referenced in the narrative.
    year_incorp = ""
    if entity.get("date_of_registration"):
        year_incorp = entity["date_of_registration"].split("/")[-1].split("-")[0]

    addr = entity.get("address", "")
    loc = _location(addr) if addr else (entity.get("state") or "N/A")

    seller_param = _param(poi, "ident_seller_type")
    seller_type = (seller_param.value.capitalize() if seller_param and not seller_param.unresolved else "Trader")

    nature = _nature_of_business(entity)

    gstin_status_param = _param(com, "com_gstin_active")
    gst_status_ok = (gstin_status_param.value == "active") if gstin_status_param and not gstin_status_param.unresolved else None

    bank_name = entity.get("bank_name") or entity.get("bank_name_msme") or ""
    bank_param = _param(com, "com_bank_verification")
    # Tri-state: True only when COM-07 actually resolved to a verified bucket.
    bank_verified_ok = (None if (bank_param is None or bank_param.unresolved)
                         else bank_param.assigned_score > 0)

    entity_rows = [
        ("Legal Name", esc(legal)), ("Trade Name", esc(trade)), ("Entity Type", esc(entity_type)),
        ("Year Incorporated", esc(year_incorp or "N/A")), ("Business Vintage", esc(vintage_text)),
        ("Location", esc(addr or loc)), ("Nature of Business", esc(nature)),
    ]
    if entity.get("date_of_incorporation") and entity["date_of_incorporation"] != entity.get("date_of_registration"):
        entity_rows.append(("Formation Date (Udyam)", esc(entity["date_of_incorporation"])))
    if entity.get("partners"):
        entity_rows.append(("Partners / Signatories", esc(", ".join(entity["partners"]))))
    reg_rows = [
        ("PAN Number", esc(entity.get("pan", "Not Available")), "m"),
        ("GSTIN", esc(entity.get("gstin", "Not Available")), "m"),
        ("Udyam Reg No", esc(entity.get("udyam_number", "Not Available")), "m"),
        ("GST Status", _status_pill(gstin_status_param.value.capitalize() if gstin_status_param and not gstin_status_param.unresolved else "Unknown", gst_status_ok), ""),
        ("Taxpayer Type", esc(entity.get("taxpayer_type", "N/A")), ""),
        ("GST Since", esc(entity.get("date_of_registration", "N/A")), ""),
        ("Bank", _status_pill(clean_bank(bank_name) if bank_name else "Not Available", bank_verified_ok), ""),
    ]

    profile = _profile_paragraph(entity, trade, entity_type, nature, loc, vintage_text, poa)

    com_i, poa_i, poi_i = round(com.earned), round(poa.earned), round(poi.earned)
    aml_i = round(aml.earned)
    score = com_i + poa_i + poi_i + aml_i

    extra_unlock = []
    r3 = _param(com, "com_gst_filing_compliance")
    r8 = _param(com, "com_gst_delay_days")
    filing_delayed = r3 and not r3.unresolved and r3.value != "timely"
    days_delayed = r8 and not r8.unresolved and r8.value is not None and r8.value > 10
    if filing_delayed or days_delayed:
        extra_unlock.append("__COMPLIANCE_PARTIAL__")
    if poa_i < 6:
        extra_unlock.append("__POA_PARTIAL__")
    pnm = _param(poi, "ident_pan_name_match")
    if pnm and not pnm.unresolved and pnm.value == "not_match":
        extra_unlock.append("__IDENTITY_PARTIAL__")

    return {
        "firm": esc(trade), "legal": esc(legal),
        "date": report_date or date.today().strftime("%d %B %Y"),
        "location": esc(loc), "seller_type": esc(seller_type),
        "chips": _chips(entity, result, loc, seller_type),
        "score": score, "com": com_i, "poa": poa_i, "poi": poi_i, "aml": aml_i,
        "entity": entity_rows, "reg": reg_rows, "profile": profile,
        "hsn": [(esc(c), esc(d)) for c, d in entity.get("declared_hsn", [])],
        "findings": _findings(result, entity),
        "com_rows": _code_rows(com), "poa_rows": _code_rows(poa),
        "poi_rows": _code_rows(poi), "aml_rows": _code_rows(aml),
        "extra_unlock": extra_unlock,
    }

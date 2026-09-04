"""
Resolve every "No-Consent" ScoringModel.json parameter (Compliance,
Proof of Address, Proof of Identity, Legal/AML -- 24 parameters, 50 pts)
to its canonical value, using document-extracted fields + Finoscale Data
API responses.

Response shapes below are confirmed against real PPE calls (2026-09-03,
Dinesh Polymers / GSTIN 27AACFD4279J1Z7) for: ongrid fetch-detailed,
ongrid fetch-by-pan, ongrid msme fetch-by-pan, probe42 PnP, and zigram
screening. `digitap/pan-and-gst` returned "Http Exception" for all three
sub-calls in that same test run (vendor-side outage, not a bug here) --
its resolver paths are kept as a secondary fallback only. `probe42
fetch-by-page(..., "compliance")` 403'd ("No access to the data") on this
API key -- PF/EPFO stays unresolved until that's enabled.

Never guess a value the API/docs genuinely don't support -- return an
`unresolved` Resolved instead, so the pipeline's gap report can surface it
per the project's existing "flag everything, don't fabricate" convention.
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, List, Optional


@dataclass
class Resolved:
    value: Optional[Any] = None
    source: str = ""
    note: str = ""
    unresolved: bool = False

    @classmethod
    def ok(cls, value, source, note=""):
        return cls(value=value, source=source, note=note, unresolved=False)

    @classmethod
    def missing(cls, reason, source=""):
        return cls(value=None, source=source, note=reason, unresolved=True)


@dataclass
class ApiBundle:
    ongrid_detailed: Optional[dict] = None      # ongrid_gstin_fetch_detailed(gstin)
    ongrid_by_pan: Optional[dict] = None         # ongrid_gstin_fetch_by_pan(pan)
    ongrid_msme: Optional[dict] = None           # ongrid_msme_fetch_by_pan(pan)
    digitap: Optional[dict] = None               # digitap_pan_and_gst(pan, ...) -- currently unreliable, vendor-side
    probe42_compliance: Optional[dict] = None    # probe42_fetch_by_page(id, "compliance") -- 403 on this API key
    probe42_pnp: Optional[dict] = None           # probe42_fetch_pnp(pan) -- confirmed working
    zigram: Optional[dict] = None                # zigram_screening(entity) -- see resolve_aml docstring: composite verdict is unexplained/unverified, not wired into any resolver
    zigram_partners: Optional[list] = None        # [{"name": str, "result": dict|None}, ...] -- one Individual screen per partner, same caveat


def _dig(d: Any, path: str) -> Any:
    cur = d
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _first(d: Any, *paths: str) -> Any:
    for p in paths:
        v = _dig(d, p)
        if v not in (None, ""):
            return v
    return None


def _years_since(date_str: str) -> Optional[float]:
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            d = datetime.strptime(date_str, fmt).date()
            return round((date.today() - d).days / 365.25, 2)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- keyword classifiers (ported from PROMPT.md)
SELLER_TYPE_KEYWORDS = [
    ("manufacturer", ("manufactur", "factory", "production", "fabricat", "mill", "plant")),
    ("processor", ("process", "recycl", "shredding", "granulat", "washing")),
    ("trader", ("trad", "wholesale", "retail", "dealer", "supplier", "merchant", "distributor")),
]


def _classify_seller_type(*texts: str) -> Optional[str]:
    blob = " ".join(t.lower() for t in texts if t)
    for label, kws in SELLER_TYPE_KEYWORDS:
        if any(kw in blob for kw in kws):
            return label
    return None


def _classify_constitution(raw: str) -> Optional[str]:
    if not raw:
        return None
    low = raw.lower()
    if "llp" in low or "limited liability partnership" in low:
        return "llp"
    if "private" in low or "public" in low or "listed" in low:
        return "pvt_public_listed"
    if "partnership" in low or "proprietor" in low or "huf" in low:
        return "partnership_proprietorship"
    return None


def _name_match(a: str, b: str) -> Optional[bool]:
    if not a or not b:
        return None
    norm = lambda s: set(re.sub(r'[^a-z0-9 ]', '', s.lower()).split())
    sa, sb = norm(a), norm(b)
    if not sa or not sb:
        return None
    overlap = len(sa & sb) / max(len(sa), len(sb))
    return overlap >= 0.6


# ==================================================================== COMPLIANCE
_MONTH_NUM = {"January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
              "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12}


def _period_to_date(tax_period: str, financial_year: str):
    """'financial_year' is Indian FY format "2026-2027" (Apr 2026 - Mar 2027)."""
    month = _MONTH_NUM.get(tax_period)
    if month is None:  # "Annual" (GSTR9/9C) or unrecognized -- not relevant to 3B/R1 due dates
        return None
    try:
        fy_start = int(financial_year.split("-")[0])
    except (ValueError, AttributeError, IndexError):
        return None
    year = fy_start if month >= 4 else fy_start + 1
    return date(year, month, 1)


def _due_date(period_start: date, return_type: str) -> date:
    """User-specified rule: GSTR3B due 20th of the following month, GSTR1 due 11th."""
    day = 20 if return_type == "GSTR3B" else 11
    y, m = period_start.year, period_start.month
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return date(ny, nm, day)


def _filing_delays(filing_data: list, return_type: str, months_back: int = 12) -> List[int]:
    """Delay-in-days per period (0 if on-time/early) for one return type, last N months."""
    from datetime import timedelta
    cutoff = date.today() - timedelta(days=months_back * 31)
    delays = []
    for entry in filing_data or []:
        if entry.get("return_type") != return_type:
            continue
        period_start = _period_to_date(entry.get("tax_period", ""), entry.get("financial_year", ""))
        if period_start is None or period_start < cutoff:
            continue
        try:
            filed = datetime.strptime(entry["date_of_filing"], "%d/%m/%Y").date()
        except (KeyError, ValueError, TypeError):
            continue
        due = _due_date(period_start, return_type)
        delays.append(max(0, (filed - due).days))
    return delays


def resolve_com_gstin_active(api: ApiBundle) -> Resolved:
    raw = _first(api.ongrid_detailed, "gstin_data.status")
    if raw is None:
        return Resolved.missing("Ongrid fetch-detailed did not return gstin_data.status",
                                 source="ongrid.fetch-detailed")
    low = str(raw).strip().lower()
    # "Inactive" is deliberately NOT folded into "cancelled": see
    # _CANCELLED_STATUS_TOKENS below. Cancelled and Suspended each score 0 here
    # so the distinction is score-neutral for this parameter, but the report must
    # not print "Cancelled / Suo Motu Cancelled" for a registration whose real
    # status is merely inactive.
    mapped = {"active": "active", "suspended": "suspended", "cancelled": "cancelled",
              "canceled": "cancelled", "not found": "not_found", "invalid": "not_found"}.get(low)
    if mapped is None and any(t in low for t in ("cancel", "suo motu")):
        mapped = "cancelled"
    if mapped is None:
        return Resolved.missing(f"Ongrid reports GSTIN status '{raw}', which is not one of the scoring "
                                 f"model's buckets (active / suspended / cancelled / not_found) -- "
                                 f"needs a manual gst.gov.in check rather than an assumed mapping",
                                 source="ongrid.fetch-detailed")
    return Resolved.ok(mapped, "ongrid.fetch-detailed.gstin_data.status")


def resolve_com_gst_vintage(api: ApiBundle, gst_cert_date: Optional[str]) -> Resolved:
    reg_date = _first(api.ongrid_detailed, "gstin_data.date_of_registration")
    source = "ongrid.fetch-detailed.gstin_data.date_of_registration"
    if not reg_date:
        reg_date = _first(api.digitap, "companyVintage.oldestRegistrationDate") or gst_cert_date
        source = "digitap.companyVintage" if reg_date and reg_date != gst_cert_date else "doc:gst_certificate"
    if reg_date:
        y = _years_since(reg_date)
        if y is not None:
            # Vintage stays GST-anchored per "Recykal VDD - Custom Instructions.md"
            # rule 3, even when an earlier PAN/Udyam formation date exists.
            return Resolved.ok(y, source, note=f"{y} years since GST registration on {reg_date}"
                                                " (GST-anchored per project convention, not the PAN/Udyam"
                                                " formation date)")
    return Resolved.missing("No GST registration date available from Ongrid, Digitap, or the GST certificate")


def resolve_com_gst_filing_compliance(api: ApiBundle) -> Resolved:
    filing_data = _first(api.ongrid_detailed, "gstin_data.filing_data")
    if not filing_data:
        return Resolved.missing("No filing_data available from Ongrid fetch-detailed")
    delays_3b = _filing_delays(filing_data, "GSTR3B")
    delays_r1 = _filing_delays(filing_data, "GSTR1")
    all_delays = delays_3b + delays_r1
    if not all_delays:
        return Resolved.missing("No GSTR3B/GSTR1 periods found in the last 12 months of filing_data")
    late_count = sum(1 for d in all_delays if d > 0)
    bucket = ("timely" if late_count == 0 else "minor_delays" if late_count <= 3
              else "five_delays" if late_count <= 5 else "significant_delays")
    return Resolved.ok(bucket, "ongrid.fetch-detailed.gstin_data.filing_data (due-date rule: "
                                "3B=20th/R1=11th of following month)",
                        note=f"{late_count} late filings out of {len(all_delays)} periods checked")


def resolve_com_filing_frequency(api: ApiBundle) -> Resolved:
    entries = _first(api.ongrid_detailed, "gstin_data.filing_frequency")
    if not entries:
        return Resolved.missing("No filing_frequency data from Ongrid fetch-detailed")
    latest = sorted(entries, key=lambda e: (e.get("financial_year", ""), e.get("quarter", "")))[-1]
    raw = str(latest.get("frequency", "")).strip().lower()
    if "month" in raw:
        return Resolved.ok("monthly", "ongrid.fetch-detailed.gstin_data.filing_frequency")
    if "quarter" in raw or "qrmp" in raw:
        return Resolved.ok("quarterly", "ongrid.fetch-detailed.gstin_data.filing_frequency")
    return Resolved.missing(f"Unrecognized filing frequency value '{raw}'")


# GSTN's real registration-status vocabulary distinguishes several non-Active
# states, and only the cancelled family is what ScoringModel.json penalises
# (`cancelled_present` = 0 pts; `active_multi_state` scores the same 2 pts as
# `no_additional`). Ongrid's fetch-by-pan collapses every non-Active state to
# the single label "Inactive", so that label alone is NOT evidence of
# cancellation -- an inactive/surrendered/provisional registration is a
# materially different (and unpenalised) thing.
_CANCELLED_STATUS_TOKENS = ("cancel", "suo motu", "suo-motu", "suomotu")
_AMBIGUOUS_STATUS_TOKENS = ("inactive", "suspend", "provisional", "surrender", "migrat")


def _status_class(status: str) -> str:
    low = (status or "").strip().lower()
    if not low:
        return "unknown"
    if low == "active":
        return "active"
    if any(t in low for t in _CANCELLED_STATUS_TOKENS):
        return "cancelled"
    if any(t in low for t in _AMBIGUOUS_STATUS_TOKENS):
        return "ambiguous"
    return "unknown"


def resolve_com_multiple_registrations(api: ApiBundle, subject_gstin: Optional[str] = None) -> Resolved:
    """PAN-wide GSTIN sweep. Ongrid fetch-by-pan supplies the *list*; Probe42
    PnP's `gst_details[].status` supplies the authoritative per-GSTIN status
    (it reports the real GSTN string, e.g. "Cancelled", where Ongrid only says
    "Inactive"). We only ever emit `cancelled_present` when a source states
    cancellation outright -- otherwise a non-Active-but-unclassifiable
    registration is left unresolved for a human rather than silently scored 0.
    """
    registrations: dict = {}
    sources = []

    results = _first(api.ongrid_by_pan, "results")
    if results:
        sources.append("ongrid.fetch-by-pan.results")
        for r in results:
            gstin = str(r.get("document_id") or "").strip()
            if gstin:
                registrations[gstin] = {"status": r.get("status"), "state": r.get("state")}

    for g in (_first(api.probe42_pnp, "gst_details") or []):
        gstin = str(g.get("gstin") or "").strip()
        if not gstin:
            continue
        entry = registrations.setdefault(gstin, {"state": g.get("state")})
        # Probe42's status string wins: it preserves GSTN's own wording.
        if g.get("status"):
            entry["status"] = g["status"]
            entry["status_source"] = "probe42.pnp.gst_details[].status"
    if any("status_source" in e for e in registrations.values()):
        sources.append("probe42.pnp.gst_details[].status")

    if not registrations:
        return Resolved.missing("No PAN-wide GSTIN list available from Ongrid fetch-by-pan or Probe42 PnP")

    source = " + ".join(sources)
    subject = (subject_gstin or "").strip().upper()
    others = {g: e for g, e in registrations.items() if g.upper() != subject}
    if not others:
        return Resolved.ok("no_additional", source,
                            note=f"{len(registrations)} GSTIN on this PAN (the subject registration only)")

    def _label(g, e):
        return f"{g} ({e.get('state') or '?'}: {e.get('status') or 'status unknown'})"

    classes = {g: _status_class(e.get("status")) for g, e in others.items()}
    cancelled = [g for g, c in classes.items() if c == "cancelled"]
    if cancelled:
        return Resolved.ok(
            "cancelled_present", source,
            note=f"{len(registrations)} GSTINs on PAN; {len(cancelled)} confirmed cancelled: "
                 + "; ".join(_label(g, others[g]) for g in cancelled))
    if all(c == "active" for c in classes.values()):
        return Resolved.ok("active_multi_state", source,
                            note=f"{len(registrations)} GSTINs on this PAN, all Active: "
                                 + "; ".join(_label(g, e) for g, e in others.items()))
    unclear = [g for g, c in classes.items() if c != "active"]
    return Resolved.missing(
        f"{len(unclear)} additional GSTIN(s) on this PAN report a non-Active status that no source "
        f"classifies as cancelled -- " + "; ".join(_label(g, others[g]) for g in unclear)
        + ". 'Inactive'/'Suspended'/'Provisional' score 2 pts (active_multi_state) while 'Cancelled' "
          "scores 0, so this needs a manual gst.gov.in -> Search by PAN check rather than an assumption.",
        source=source)


# HSN/SAC chapter (first 2 digits) -> keywords describing the goods that chapter
# covers, used to test the scoring model's actual question: do the declared HSN
# codes correspond to the declared product category? Chapter 46 is included in
# the plastics family on purpose: HSN Chapter 46's own legal note defines
# "plaiting materials" to include "monofilament and strip and the like, of
# plastics", so woven/plaited plastic sheet and matting is correctly declared
# under 4601 by a plastics manufacturer. Only chapters relevant to this
# platform's seller base (plastics / recyclables / packaging / metals) are
# mapped -- an unmapped chapter is reported as unclassifiable, never as a
# mismatch.
_HSN_CHAPTER_KEYWORDS = {
    "39": ("plastic", "polymer", "polythene", "polyethylene", "polypropylene", "pet", "hdpe",
            "ldpe", "lldpe", "pvc", "mould", "granul", "masterbatch", "resin", "film", "packaging"),
    "40": ("rubber", "tyre", "tube", "latex", "elastomer"),
    "46": ("plastic", "polymer", "strip", "monofilament", "mat", "matting", "woven", "plait",
            "basket", "weav"),
    "47": ("paper", "pulp", "waste paper", "recycl"),
    "48": ("paper", "paperboard", "carton", "corrugat", "packaging", "print"),
    "54": ("yarn", "filament", "man-made", "synthetic", "plastic", "polymer", "textile"),
    "55": ("fibre", "fiber", "staple", "synthetic", "polyester", "textile", "plastic"),
    "63": ("sack", "bag", "textile", "woven", "packaging", "plastic"),
    "70": ("glass",),
    "72": ("iron", "steel", "metal", "scrap", "ferrous"),
    "73": ("iron", "steel", "metal", "fabricat", "scrap", "ferrous"),
    "74": ("copper", "metal", "scrap", "non-ferrous"),
    "76": ("aluminium", "aluminum", "metal", "scrap", "non-ferrous"),
    "78": ("lead", "metal", "scrap", "battery", "non-ferrous"),
    "79": ("zinc", "metal", "scrap", "non-ferrous"),
    "84": ("machine", "machinery", "equipment", "plant", "engineering"),
    "85": ("electric", "electronic", "e-waste", "cable", "wire", "battery", "appliance"),
    "87": ("vehicle", "automobile", "auto", "motor"),
    "99": ("service", "job work", "consult", "transport", "logistic"),
}


def resolve_com_hsn_match(api: ApiBundle, entity: dict = None) -> Resolved:
    """Does the declared HSN/SAC set correspond to the declared product category?

    Previously this returned "match" whenever *any* HSN code existed, which
    asserted a positive without performing the comparison the parameter names.
    Now the HSN chapter's goods family is tested against the entity's declared
    activity text (Udyam NIC description, MSME major activity, the utility
    bill's connection activity, and GST's own nature-of-business field).
    """
    entity = entity or {}
    goods = _first(api.ongrid_detailed, "gstin_data.hsn_data.goods") or []
    services = _first(api.ongrid_detailed, "gstin_data.hsn_data.services") or []
    source = "ongrid.fetch-detailed.gstin_data.hsn_data vs declared activity (Udyam NIC / MSME / utility bill)"
    if not goods and not services:
        return Resolved.ok("not_available", "ongrid.fetch-detailed.gstin_data.hsn_data",
                            note="No HSN/SAC codes on record for this GSTIN")

    activity_blob = " ".join(str(entity.get(k) or "") for k in (
        "nic_5_description", "nic_4_description", "major_activity", "electricity_bill_activity",
        "gst_nature_of_business_activity", "nature_of_business", "trade_name")).lower()
    if not activity_blob.strip():
        return Resolved.missing("HSN/SAC codes are on record but no declared-activity text (Udyam NIC code, "
                                 "MSME major activity or utility-bill activity) is available to compare them "
                                 "against", source=source)

    codes = []
    for item in list(goods) + list(services):
        if not isinstance(item, dict):
            continue
        code = str(item.get("hsn") or item.get("sac") or "").strip()
        if code:
            codes.append((code, str(item.get("description") or "")))

    matched, unmapped, mismatched = [], [], []
    for code, desc in codes:
        chapter = code[:2]
        kws = _HSN_CHAPTER_KEYWORDS.get(chapter)
        if kws is None:
            unmapped.append(code)
        elif any(kw in activity_blob for kw in kws):
            hit = next(kw for kw in kws if kw in activity_blob)
            matched.append(f"{code} (chapter {chapter} ~ '{hit}')")
        else:
            mismatched.append(f"{code} (chapter {chapter})")

    counts = f"{len(goods)} goods HSN + {len(services)} services SAC on record"
    if matched:
        return Resolved.ok("match", source,
                            note=f"{counts}; consistent with the declared activity: {', '.join(matched)}"
                                 + (f"; unclassifiable chapters: {', '.join(unmapped)}" if unmapped else "")
                                 + (f"; not consistent: {', '.join(mismatched)}" if mismatched else ""))
    if mismatched and not unmapped:
        return Resolved.ok("not_match", source,
                            note=f"{counts}; none of the declared HSN chapters correspond to the declared "
                                 f"activity ({', '.join(mismatched)}) -- warrants clarification")
    return Resolved.missing(f"{counts}, but the declared HSN chapter(s) {', '.join(unmapped + mismatched)} "
                             f"are not in this pipeline's chapter->activity map, so a match cannot be "
                             f"asserted or ruled out automatically -- needs manual review",
                             source=source)


def resolve_com_gst_delay_days(api: ApiBundle) -> Resolved:
    filing_data = _first(api.ongrid_detailed, "gstin_data.filing_data")
    if not filing_data:
        return Resolved.missing("No filing_data available from Ongrid fetch-detailed")
    all_delays = _filing_delays(filing_data, "GSTR3B") + _filing_delays(filing_data, "GSTR1")
    if not all_delays:
        return Resolved.missing("No GSTR3B/GSTR1 periods found in the last 12 months of filing_data")
    return Resolved.ok(float(max(all_delays)), "ongrid.fetch-detailed.gstin_data.filing_data (due-date rule)")


def resolve_com_pf_filing_status(api: ApiBundle) -> Resolved:
    epf_entities = _first(api.probe42_compliance, "epf_entities")
    # Probe42's PnP (PAN) document carries the same EPFO establishment list and
    # is available on this API key, unlike the `compliance` page (403 -- "No
    # access to the data"). Check both before concluding there is no EPFO
    # registration.
    pnp_epf = _first(api.probe42_pnp, "establishments_registered_with_epfo")
    if not epf_entities and not pnp_epf:
        checked = []
        if api.probe42_pnp is not None:
            checked.append("Probe42 PnP establishments_registered_with_epfo (empty)")
        if api.probe42_compliance is not None:
            checked.append("Probe42 compliance-page epf_entities (empty)")
        detail = ("; ".join(checked) if checked
                  else "no Probe42 response available this run (compliance page returns 403 on this API key)")
        return Resolved.missing("N/A -- not applicable. No EPFO establishment is registered against this "
                                 "entity, so PF filing regularity does not apply. Scored as CONDITIONAL/omit "
                                 "per ScoringModel.json ('omit (N/A) if entity has no PF-registered "
                                 f"employees'), not as non-compliant. Sources checked: {detail}.")
    if not epf_entities:
        return Resolved.missing("An EPFO establishment is registered against this entity (Probe42 PnP), but "
                                 "PF filing regularity is only exposed on Probe42's `compliance` page, which "
                                 "returns 403 (\"No access to the data\") on this API key -- enable that "
                                 "entitlement or check the EPFO portal manually.",
                                 source="probe42.pnp.establishments_registered_with_epfo")
    compliant = _first(api.probe42_compliance, "epf_behav.compliant", "epf_yearly_data.0.compliant")
    if compliant is None:
        return Resolved.missing("EPFO establishment found but no compliance flag in the fields checked -- "
                                 "verify actual Probe42 compliance-page shape.", source="probe42.compliance")
    return Resolved.ok("compliant" if compliant else "non_compliant", "probe42.compliance.epf_*")


# ==================================================================== PROOF OF ADDRESS
def resolve_addr_ownership_type(has_rental_doc: bool, has_electricity_doc: bool,
                                 has_sale_deed: bool = False, entity: dict = None) -> Resolved:
    entity = entity or {}
    if has_sale_deed:
        return Resolved.ok("owned", "doc:sale_deed present", note="Ownership confirmed by sale deed on file")
    if has_rental_doc:
        return Resolved.ok("rented", "doc:rental_agreement present",
                            note="Cannot distinguish 'rented' vs 'leased' from document presence alone")
    if has_electricity_doc:
        # No sale deed in the document set, so ownership is inferred. Spell out the
        # corroborating evidence rather than calling it a bare assumption -- the
        # report prints ScoringModel.json's own condition label here ("Owned --
        # confirmed via sale deed"), which would otherwise overstate what we hold.
        corroboration = []
        if entity.get("electricity_bill_consumer_name"):
            corroboration.append(f"the industrial electricity connection is in the entity's own name "
                                  f"({entity['electricity_bill_consumer_name'].strip()})")
        if entity.get("electricity_bill_connection_date"):
            corroboration.append(f"held continuously since {entity['electricity_bill_connection_date']}")
        if entity.get("electricity_bill_sanctioned_load"):
            corroboration.append(f"with a {entity['electricity_bill_sanctioned_load']} sanctioned load "
                                  f"(fixed plant, not a short-tenancy fit-out)")
        note = ("NO SALE DEED ON FILE -- ownership is inferred from the absence of any rental/lease "
                 "agreement in the document set")
        if corroboration:
            note += ", corroborated by: " + "; ".join(corroboration)
        note += ". Obtain the sale deed or property-tax receipt to convert this from inferred to confirmed."
        return Resolved.ok("owned", "doc:electricity_bill present, no rental/lease agreement in document set",
                            note=note)
    return Resolved.missing("Neither a rental agreement nor an electricity bill is on file")


def resolve_addr_landlord_declaration(ownership: Resolved, has_landlord_doc: bool = False) -> Resolved:
    """CONDITIONAL parameter (1 pt). ScoringModel.json: "omit (N/A) for owned
    premises" -- a landlord NOC is meaningless when the occupier is the owner.
    The engine excludes conditional-and-unresolved params from the denominator,
    so returning an explicitly-labelled N/A here is score-identical to the old
    static `missing(...)` -- but the report row now says *why* it's N/A instead
    of leaking an implementation detail about extract/classify.py."""
    if has_landlord_doc:
        return Resolved.ok("present", "doc:landlord_declaration present")
    if not ownership.unresolved and ownership.value == "owned":
        return Resolved.ok("not_applicable", "derived from addr_ownership_type=owned",
                            note="N/A -- not applicable. The premises are owned/occupied by the business "
                                 "itself, so there is no landlord and no owner NOC to obtain "
                                 "(ScoringModel.json: CONDITIONAL -- omit for owned premises).")
    if not ownership.unresolved and ownership.value in ("rented", "leased"):
        # A genuine documentation gap, not an N/A -- rented premises are expected
        # to have the landlord's NOC for commercial use at the registered address.
        return Resolved.ok("absent", "no landlord-declaration document in the document set",
                            note=f"GAP: premises are {ownership.value}, so a landlord NOC permitting business "
                                 f"use at the registered address is required, but none is on file.")
    return Resolved.missing("Ownership type is unresolved, so it cannot be determined whether a landlord "
                             "declaration is required (N/A for owned premises) or missing (rented/leased).")


# Address-comparison helpers. A pincode is only one field of an address and is
# the field most often mis-keyed by a utility's own billing system, so it can't
# be the sole arbiter -- the premises identifier (plot/door number), the
# locality/city, and the consumer name on the bill are together far stronger
# evidence that the bill covers the GST-registered premises.
_ADDR_STOP_WORDS = {
    "plot", "plno", "pl", "no", "nos", "number", "door", "flat", "block", "building",
    "premises", "bldg", "gala", "unit", "shed", "room", "floor", "road", "street",
    "lane", "marg", "village", "vtc", "town", "city", "dist", "district", "state",
    "pin", "code", "near", "opp", "opposite", "behind", "at", "post", "po", "the",
    "and", "of", "ms", "mrs", "mr", "shri", "india",
}
_ADDR_STATE_WORDS = {s.lower() for s in (
    "maharashtra", "gujarat", "karnataka", "kerala", "punjab", "haryana", "rajasthan",
    "delhi", "telangana", "assam", "goa", "bihar", "jharkhand", "odisha", "sikkim",
    "tripura", "mizoram", "manipur", "nagaland", "meghalaya", "uttarakhand",
    "chandigarh", "puducherry", "pradesh", "bengal", "nadu", "kashmir", "chhattisgarh",
)}


def _norm_addr(s: str) -> str:
    """Collapse punctuation *inside* tokens before splitting, so 'M.I.D.C.' -> 'midc'
    and 'G-100' -> 'g100' instead of shattering into single letters."""
    s = (s or "").lower()
    s = re.sub(r'[.\-/\\]', '', s)
    return re.sub(r'[^a-z0-9]+', ' ', s).strip()


def _addr_token_sets(s: str):
    """-> (premises identifiers, locality/place words, pincodes)"""
    toks = [t for t in _norm_addr(s).split() if t and t not in _ADDR_STOP_WORDS]
    pins = {t for t in toks if len(t) == 6 and t.isdigit()}
    premises = {t for t in toks
                if t not in pins and any(c.isdigit() for c in t) and any(c.isalpha() for c in t)}
    premises |= {t for t in toks if t.isdigit() and 1 <= len(t) <= 4}
    places = {t for t in toks
              if t.isalpha() and len(t) >= 3 and t not in _ADDR_STATE_WORDS}
    return premises, places, pins


def resolve_addr_electricity_bill(gst_address: Optional[str], bill_address: Optional[str],
                                   bill_pincode: Optional[str], bill_village: Optional[str] = None,
                                   bill_consumer_name: Optional[str] = None,
                                   entity_name: Optional[str] = None) -> Resolved:
    if not bill_address and not bill_pincode:
        return Resolved.missing("No electricity bill address/pincode extracted")
    if not gst_address:
        return Resolved.missing("No GST certificate address to compare against")

    src = "electricity_bill address/pincode/consumer-name vs gst_certificate principal address"
    gst_prem, gst_place, gst_pins = _addr_token_sets(gst_address)
    bill_prem, bill_place, bill_pins = _addr_token_sets(" ".join(
        x for x in (bill_address, bill_village, bill_pincode) if x))

    gst_pin = next(iter(gst_pins), None)
    bill_pin = (bill_pincode or next(iter(bill_pins), None))
    premises_match = bool(gst_prem & bill_prem)
    place_hits = gst_place & bill_place
    # Does the bill's own consumer name identify the entity we're assessing?
    consumer = re.sub(r'^\s*m\s*/?\s*s\.?\s+', '', (bill_consumer_name or ""), flags=re.I)
    name_match = _name_match(consumer, entity_name or "") if consumer and entity_name else None

    evidence = []
    if premises_match:
        evidence.append(f"premises identifier matches ({'/'.join(sorted(gst_prem & bill_prem))})")
    if place_hits:
        evidence.append(f"locality/city matches ({', '.join(sorted(place_hits))})")
    if name_match is True:
        evidence.append(f"bill consumer name '{(bill_consumer_name or '').strip()}' matches the entity")

    if gst_pin and bill_pin and gst_pin == bill_pin:
        evidence.append(f"PIN {gst_pin} matches")
        return Resolved.ok("match", src, note="; ".join(evidence))

    if gst_pin and bill_pin and gst_pin != bill_pin:
        pin_note = (f"NOTE: the bill's printed PIN ({bill_pin}) differs from the GST-registered PIN "
                     f"({gst_pin})")
        # Premises identifier + locality + consumer name all agreeing identifies the
        # same physical premises beyond reasonable doubt; a lone wrong PIN on the
        # utility's record is a data-quality defect in the bill, not a different
        # address. Requiring all three keeps this from firing on a genuinely
        # different premises (where the plot number would not agree).
        if premises_match and len(place_hits) >= 2 and name_match is True:
            return Resolved.ok("match", src, note="; ".join(evidence) + f". {pin_note} "
                                "-- treated as a utility-record data error, not an address discrepancy, "
                                "because plot number, locality/city and consumer name all agree.")
        if premises_match or place_hits:
            return Resolved.ok("minor_discrepancy", src,
                                note="; ".join(evidence or ["partial address overlap only"]) + f". {pin_note}")
        return Resolved.ok("not_match", src,
                            note=f"{pin_note}, and neither the premises identifier nor the locality "
                                 f"text overlaps the GST-registered address")

    # Only one side has a usable PIN -- fall back to the component comparison.
    if premises_match and place_hits:
        return Resolved.ok("match", src, note="; ".join(evidence) + " (no PIN available on both sides to cross-check)")
    if place_hits:
        return Resolved.ok("minor_discrepancy", src,
                            note="; ".join(evidence) + " but the premises identifier could not be matched")
    return Resolved.missing("Could not confidently compare the electricity bill address to the GST address "
                             "(no PIN on both sides, and no premises/locality token overlap)")


def resolve_addr_msme(api: ApiBundle, doc_udyam_number: Optional[str] = None) -> Resolved:
    udyam = _first(api.ongrid_msme, "udyam_number")
    if udyam:
        return Resolved.ok("valid_active", "ongrid.msme.fetch-by-pan.udyam_number")
    if api.ongrid_msme is not None:
        # API was actually called and found nothing for this PAN -- authoritative.
        return Resolved.ok("not_valid", "ongrid.msme.fetch-by-pan", note="No Udyam registration found for this PAN")
    if doc_udyam_number:
        # API wasn't called this run -- the MSME certificate itself is still real evidence,
        # just not live-verified against the Udyam registry.
        return Resolved.ok("valid_active", "doc:msme_certificate.udyam_number",
                            note="Not cross-checked against Ongrid MSME verification this run")
    return Resolved.missing("No Ongrid MSME lookup performed and no Udyam number found on any document")


# ==================================================================== PROOF OF IDENTITY
def resolve_ident_pan_active(api: ApiBundle, gstin_active: Optional[str] = None) -> Resolved:
    raw = _first(api.digitap, "panDetailsPlus.result.pan_status")
    if raw is not None:
        low = str(raw).strip().lower()
        if low == "valid":
            return Resolved.ok("active", "digitap.panDetailsPlus.result.pan_status")
        if low in ("invalid", "not_found", "notfound"):
            return Resolved.ok("not_found", "digitap.panDetailsPlus.result.pan_status")
        return Resolved.ok("inactive", "digitap.panDetailsPlus.result.pan_status")
    # Digitap panDetailsPlus is currently unreliable (vendor-side "Http Exception" in
    # testing). Fall back to inference: GSTN cannot register a GSTIN against an
    # inoperative/invalid PAN, so an Active GSTIN implies the underlying PAN is valid.
    if gstin_active == "active":
        return Resolved.ok("active", "inferred from ongrid GSTIN status=active",
                            note="Digitap PAN check unavailable this run -- inferred, not directly verified")
    return Resolved.missing("No PAN status from Digitap, and GSTIN status wasn't 'active' to infer from")


def resolve_ident_seller_type(nature_of_business: str, major_activity: str) -> Resolved:
    guess = _classify_seller_type(nature_of_business or "", major_activity or "")
    if guess is None:
        return Resolved.missing("Nature-of-business text didn't match any seller-type keyword")
    return Resolved.ok(guess, "keyword match on nature-of-business / MSME major activity")


def resolve_ident_constitution(gst_constitution: str) -> Resolved:
    guess = _classify_constitution(gst_constitution or "")
    if guess is None:
        return Resolved.missing(f"GST certificate constitution '{gst_constitution}' didn't match a known bucket")
    return Resolved.ok(guess, "gst_certificate.constitution")


def resolve_ident_pan_name_match(pan_name: Optional[str], gst_legal_name: Optional[str]) -> Resolved:
    m = _name_match(pan_name or "", gst_legal_name or "")
    if m is None:
        return Resolved.missing("PAN card name or GST legal name unavailable for comparison")
    return Resolved.ok("match" if m else "not_match", "PAN card name vs GST legal name (token overlap)")


# ==================================================================== LEGAL / AML
AML_PARAM_IDS = ("legal_sanctions", "legal_pep", "legal_rbi_wilful_defaulter",
                  "legal_ecourts", "legal_drt_sarfaesi")


def resolve_legal_sanctions(entity_name: str) -> Resolved:
    """OFAC/UN/EU/World Bank/OpenSanctions sweep (see vdd/aml/screening.py).
    CORRECTION (see resolve_aml's docstring): an earlier debug session
    wrongly concluded Zigram was only subscribed to an irrelevant "Angola
    Watchlists" list -- that was misread from a truncated print, not a real
    subscription gap. Zigram is confirmed live on this API key with 59 check
    blocks including OFAC/PEP/adverse-media/India-specific lists (CIBIL,
    NIA, ED, MCA). This free sweep is still used here as an independent
    corroborating cross-check (it caught a real EU-sanctions cache bug
    Zigram's own response wouldn't have revealed) -- not because Zigram is
    unusable. Making Zigram the primary source for this field is a real,
    not-yet-implemented follow-up (see resolve_aml). Coverage caveats,
    surfaced in the note rather than assumed away: OpenSanctions now
    requires a paid API key (401) so it screens nothing, and UAPA-NIA
    (India-specific) is not covered by any of these four free sources."""
    from vdd.aml.screening import run_sanctions_sweep, Severity
    try:
        findings = run_sanctions_sweep(entity_name)
    except Exception as e:
        return Resolved.missing(f"Sanctions sweep failed to run: {e}")
    hits = [f for f in findings if f.severity in (Severity.HIGH, Severity.ELEVATED)]
    unscreened = [f.source_name for f in findings if f.severity == Severity.WATCH]
    if hits:
        return Resolved.ok("listed", "aml.screening.run_sanctions_sweep",
                            note="; ".join(f"{f.source_name}: {f.finding_summary}" for f in hits))
    clean = [f.source_name for f in findings if f.severity == Severity.NONE]
    if not clean:
        return Resolved.missing("No sanctions source could be screened this run ("
                                 + ", ".join(unscreened) + ") -- not counted as clean.",
                                 source="aml.screening.run_sanctions_sweep")
    note = "Clean across " + ", ".join(clean)
    if unscreened:
        note += f". Unscreened this run and NOT counted as clean: {', '.join(unscreened)}"
    note += (". Note: India's UAPA-NIA list is not separately covered by any of these sources.")
    return Resolved.ok("not_listed", "aml.screening.run_sanctions_sweep", note=note)


def resolve_legal_pep(person_names: List[str]) -> Resolved:
    """PEP screening -- proprietor and all partners (AML-02).

    Screens each natural person associated with the entity (partners /
    authorised signatories, from the GST registration and Probe42) against
    MyNeta/ADR (Election Commission candidate affidavits, which cover anyone
    who has contested a state or national election) and the Wikidata Query
    Service's office-holder records. Both are free and key-free; see
    vdd/aml/india_legal.py for why OpenSanctions' PEP *API* is not usable (401
    without a paid key) and why its free bulk dump is not shipped (CC-BY-NC).

    The firm's own name is deliberately not screened here -- PEP status
    attaches to people, and the model's parameter says "proprietor and all
    partners".
    """
    from vdd.aml.india_legal import screen_pep
    from vdd.aml.screening import Severity
    if not person_names:
        return Resolved.missing("No proprietor/partner/signatory names are on record (GST registration and "
                                 "Probe42 both returned none), so PEP status could not be screened. "
                                 "Not counted as clean.")
    try:
        f = screen_pep(person_names)
    except Exception as e:
        return Resolved.missing(f"PEP screen failed to run: {e}")
    if f.severity in (Severity.HIGH, Severity.ELEVATED):
        return Resolved.ok("pep_identified", "aml.india_legal.screen_pep", note=f.finding_summary)
    if f.severity == Severity.WATCH:
        return Resolved.missing(f.finding_summary, source="aml.india_legal.screen_pep")
    return Resolved.ok("no_pep", "aml.india_legal.screen_pep (MyNeta/ECI + Wikidata WDQS)",
                        note=f.finding_summary)


def resolve_legal_drt_sarfaesi(names: List[str]) -> Resolved:
    """DRT / SARFAESI -- no debt recovery tribunal cases (AML-05).

    Queries drt.gov.in's own JSON API across all 39 DRTs and 5 DRATs (the
    URL the scoring model's parameterDescription already names). No login and
    no server-side captcha -- the on-screen captcha is generated and compared
    entirely in the browser. Covers SARFAESI s.17 Securitisation Applications
    (casetype "SA") as well as bank recovery Original Applications.
    """
    from vdd.aml.india_legal import screen_drt_sarfaesi
    from vdd.aml.screening import Severity
    if not names:
        return Resolved.missing("No entity/partner names available to screen against DRT/DRAT")
    try:
        f = screen_drt_sarfaesi(names)
    except Exception as e:
        return Resolved.missing(f"DRT/SARFAESI screen failed to run: {e}")
    if f.severity in (Severity.HIGH, Severity.ELEVATED):
        return Resolved.ok("active_drt", "aml.india_legal.screen_drt_sarfaesi", note=f.finding_summary)
    if f.severity == Severity.WATCH:
        return Resolved.missing(f.finding_summary, source="aml.india_legal.screen_drt_sarfaesi")
    return Resolved.ok("no_drt", "aml.india_legal.screen_drt_sarfaesi (drt.gov.in/drtapi, all 44 tribunals)",
                        note=f.finding_summary)


# Sources investigated and deliberately NOT automated. Kept as explicit constants
# so the report says *why* a register is unscreened rather than implying nobody
# has looked, and so a future re-check has the precise blocker to re-test against.
_ECOURTS_BLOCKER = (
    "NOT SCREENED -- no automatable public source. eCourts' party-name search "
    "(services.ecourts.gov.in) is gated by a server-side Securimage image captcha whose answer is held "
    "in the portal's PHP session and never sent to the client, behind a second rotating `app_token` "
    "request-validation layer that rejects even fully-formed POSTs with \"Invalid Request\". The National "
    "Judicial Data Grid has no party-name search at all, and no eCourts party-search API is published on "
    "API Setu. Automating this would require a captcha-solving service, which is not acceptable in a "
    "compliance product. MANUAL ANALYST STEP: search the party name on services.ecourts.gov.in and record "
    "the CNR numbers. Not counted as clean.")

_WILFUL_DEFAULTER_BLOCKER = (
    "NOT SCREENED -- no automatable public source. RBI no longer publishes the wilful-defaulter list: "
    "under the RBI (Treatment of Wilful Defaulters and Large Defaulters) Directions of Nov 2025, lenders "
    "report defaulters to credit information companies and there is no public-website publication clause "
    "(rbi.org.in's defaulters-list page is a 1994-era descriptive gist with no dataset behind it). The one "
    "free public search, TransUnion CIBIL's suit.cibil.com \"Public Access\", is behind a Cloudflare "
    "Turnstile managed challenge and its own terms expressly forbid automated access and commercial reuse. "
    "MANUAL ANALYST STEP: run the firm and each partner through the CIBIL Suit-Filed/Wilful-Defaulter public "
    "search, or license a CIC feed. Not counted as clean.")


def resolve_aml(api: ApiBundle, entity_name: str = None, partner_names: List[str] = None) -> dict:
    """All five legal_* parameters.

    Zigram status -- deliberately NOT wired into any resolver below, and this
    needs a direct answer from whoever manages the Finoscale/Zigram account
    before it should be. Two different debugging passes reached two different
    conclusions about it (first: "only subscribed to one irrelevant watchlist";
    second, a retraction: "fully live, 59 check blocks incl. CIBIL/NIA/ED/MCA").
    Neither survived a third, independent round of live verification (2026-09-03):
    four fresh, uncached `zigram_screening` calls (Kingfisher Airlines as
    Organization, Dawood Ibrahim as both Individual and Organization, and the
    real test vendor with its actual PAN attached) EVERY time returned exactly
    one `entitychecks` block ("Angola Watchlists"), `Subscribed: {}`, and
    `pdfUrl: "NA"` -- no CIBIL/NIA/ED/MCA/SanctionCheck/PepCheck data was
    observed in any response. More concerning: for Kingfisher Airlines, that
    one real check came back `match_status: "Green", match_score: 0` (no
    match) while the top-level `Case_Outcome.Status` still said "Red"/"10/10"
    -- the composite verdict does not reconcile with the only check that
    actually ran, and the mechanism producing it is unexplained. The composite
    score did correctly separate known-bad names (Red) from the clean real
    vendor (Green) across these tests, so it may be *some* signal -- but not
    one to build scored resolvers on until that contradiction is explained
    (possibly a PPE/sandbox-tier artifact, possibly something else). Flag this
    to whoever manages the API key rather than re-attempt reverse-engineering
    it from more probe calls. Known, separately-real bugs worth fixing
    regardless of the above: `pipeline.py`'s Zigram call used the vendor
    folder's basename instead of the GST legal name and only ever screened
    the entity once as `type: "Organization"`, never the individual
    partners -- fixed. Until Zigram's composite verdict is explained, the
    free OFAC/UN/EU/World-Bank sweep below remains the source for
    legal_sanctions, and legal_pep/legal_drt_sarfaesi use the india_legal.py
    screeners; legal_rbi_wilful_defaulter and legal_ecourts stay unresolved."""
    names = [n for n in ([entity_name] + list(partner_names or [])) if n]
    # De-duplicate while preserving order (entity name first -- it labels the Finding).
    seen, ordered = set(), []
    for n in names:
        k = n.strip().lower()
        if k and k not in seen:
            seen.add(k)
            ordered.append(n.strip())

    # PEP is a person-level check; DRT/SARFAESI is entity + persons.
    persons = [n for n in ordered if not entity_name or n.strip().lower() != entity_name.strip().lower()]

    return {
        "legal_sanctions": (resolve_legal_sanctions(entity_name) if entity_name else
                             Resolved.missing("No entity name available to screen")),
        "legal_pep": resolve_legal_pep(persons),
        "legal_drt_sarfaesi": resolve_legal_drt_sarfaesi(ordered),
        "legal_rbi_wilful_defaulter": Resolved.missing(_WILFUL_DEFAULTER_BLOCKER, "manual"),
        "legal_ecourts": Resolved.missing(_ECOURTS_BLOCKER, "manual"),
    }


def resolve_com_bank_verification(entity: dict) -> Resolved:
    """Bank Account Verification -- Penny Drop + GST Match (COM-07, 5 pts).

    Every bucket in ScoringModel.json asserts an outcome of an *actual* penny
    drop ("penny drop successful / unsuccessful"), and the Finoscale Data API
    exposes no bank-account-verification endpoint (Ongrid GSTIN/MSME, Digitap
    PAN+GST, Probe42 and Zigram are the whole surface -- checked against
    vendors-data-api-reference.md), so this pipeline cannot run one.

    There is exactly one document-derived signal that is genuinely equivalent:
    the GST portal's own Bank Account Status. GSTN validates a taxpayer's bank
    account through the NPCI/PFMS account-validation rail against the
    registered taxpayer's name -- i.e. a penny-drop-equivalent *with* a GST
    name match. So:

      * portal status = Validated, and the account agrees with the cancelled
        cheque -> `penny_success_gst_match` (a real, sourced positive).
      * portal status = NotValidated / Failed -> NOT scored as
        `penny_unsuccessful`. GSTN validation routinely fails for benign
        reasons (CC/OD account types aren't reliably supported on the NPCI
        rail, name-format differences), so a negative portal flag is not
        evidence that a penny drop would fail. Left unresolved with the full
        evidence trail for manual entry.

    Cross-source account-number agreement (cheque = Udyam = KYC form = portal
    listing) is recorded as evidence but is deliberately NOT sufficient on its
    own: consistent paperwork proves the vendor consistently *declares* an
    account, not that the account exists, is open, or belongs to them.

    Note on "Recykal VDD - Custom Instructions.md" rule 1: its "re-score as
    verified (not 0)" clause is scoped to resolving a *cheque-vs-MSME
    mismatch* when a GST portal screenshot agrees with the cheque. There is no
    cheque-vs-MSME mismatch here (they agree), so that clause is not triggered
    and does not authorise treating a NotValidated portal status as verified.
    """
    accounts = {
        "cancelled cheque": entity.get("cheque_account_number") or entity.get("account_number"),
        "Udyam/MSME certificate": entity.get("account_number_msme"),
        "customer KYC form": entity.get("kyc_form_account_number"),
        "GST portal screenshot": entity.get("gst_portal_account_number"),
    }
    present = {label: str(v).strip() for label, v in accounts.items() if v}
    distinct = set(present.values())
    bank = entity.get("bank_name") or entity.get("bank_name_msme") or "the stated bank"
    ifsc = entity.get("ifsc") or entity.get("ifsc_msme")

    ev = []
    if len(distinct) == 1 and len(present) >= 2:
        acct = next(iter(distinct))
        ev.append(f"Account {acct} at {bank}"
                  + (f" (IFSC {ifsc})" if ifsc else "")
                  + f" agrees across {len(present)} independent sources: {', '.join(sorted(present))}")
    elif len(distinct) > 1:
        ev.append("CRITICAL: the account number differs between sources -- "
                  + "; ".join(f"{label}: {num}" for label, num in sorted(present.items())))
    elif present:
        label, num = next(iter(present.items()))
        ev.append(f"Account {num} at {bank} appears on only one source ({label}) -- not cross-checked")
    else:
        ev.append("No bank account number could be extracted from any document")

    mark = entity.get("cheque_cancellation_mark_present")
    if mark is True:
        ev.append("the cancelled cheque carries a genuine diagonal cancellation mark (visually confirmed)")
    elif mark is False:
        ev.append("WARNING: the 'cancelled cheque' document shows no visible cancellation mark -- it may be "
                  "a blank/unused leaf and is not valid proof (Custom Instructions rule 2)")

    verified = entity.get("gst_portal_bank_verified")
    portal_status = entity.get("gst_portal_account_status")
    acct_type = entity.get("gst_portal_account_type")

    if verified is True and len(distinct) == 1 and len(present) >= 2:
        return Resolved.ok(
            "penny_success_gst_match",
            "doc:gst_portal screenshot (GSTN NPCI account validation) + cancelled-cheque agreement",
            note="; ".join(ev) + ". The GST portal reports this account as Validated -- GSTN validates "
                 "bank accounts on the NPCI rail against the registered taxpayer name, which is "
                 "penny-drop-equivalent with a GST name match. No separate penny drop was executed.")

    if verified is False:
        detail = (f"the GST portal's own Bank Account Status for this GSTIN shows "
                  f"{portal_status or 'NotValidated'} (red cross, 'Revalidate' action pending)"
                  + (f" for a {acct_type}-type account" if acct_type else ""))
        if acct_type in ("CC", "OD", "OCC"):
            detail += (" -- CC/OD accounts are a known benign failure mode for GSTN validation rather than "
                        "evidence of a bad account")
        ev.append(detail)
        return Resolved.missing(
            "Not scored -- no penny drop performed. " + "; ".join(ev)
            + ". A NotValidated portal flag is not scored as 'penny drop unsuccessful' (-5) because it is "
              "not a penny-drop result; equally it cannot be read as verified. Requires either a live "
              "penny-drop/bank-account-verification call (no such endpoint on the Finoscale Data API) or "
              "a refreshed GST portal screenshot after the vendor completes 'Revalidate'.",
            source="doc:gst_portal + doc:cancelled_cheque + doc:msme_certificate")

    return Resolved.missing(
        "Not scored -- no penny drop performed and no GST portal bank-verification screenshot on file to "
        "read GSTN's own account-validation status from. " + "; ".join(ev)
        + ". Cross-source paperwork agreement alone is not treated as bank verification.",
        source="doc:cancelled_cheque + doc:msme_certificate")


# ==================================================================== orchestration
def resolve_all(entity: dict, docs, api: ApiBundle) -> dict:
    """entity: merged doc-extracted fields (see pipeline.py's merge step).
    docs: a vdd.extract.classify.ClassifiedDocs for presence checks.
    Returns {parameterId: Resolved} covering all 24 No-Consent parameters."""
    gstin_active_resolved = resolve_com_gstin_active(api)
    ownership_resolved = resolve_addr_ownership_type(
        docs.has("rental_agreement"), docs.has("electricity_bill"),
        has_sale_deed=docs.has("sale_deed"), entity=entity)
    electricity_resolved = resolve_addr_electricity_bill(
        entity.get("address"), entity.get("electricity_bill_address"), entity.get("electricity_bill_pincode"),
        bill_village=entity.get("electricity_bill_village"),
        bill_consumer_name=entity.get("electricity_bill_consumer_name"),
        entity_name=entity.get("legal_name") or entity.get("trade_name"))

    if not ownership_resolved.unresolved and ownership_resolved.value == "owned":
        # ScoringModel.json's own instruction: "For owned premises score full" --
        # independent of whether we could also OCR-match the electricity bill address.
        rental_validation_resolved = Resolved.ok("owned", "derived from addr_ownership_type=owned",
                                                   note="Full score for owned premises per ScoringModel.json rule")
    elif not ownership_resolved.unresolved and ownership_resolved.value in ("rented", "leased") \
            and not electricity_resolved.unresolved and electricity_resolved.value == "match":
        rental_validation_resolved = Resolved.ok("rented_matching", "addr_ownership_type + addr_electricity_bill")
    else:
        rental_validation_resolved = Resolved.missing(
            "Ownership type unresolved, or rented without a confirmed matching electricity-bill address")

    out = {
        "com_gstin_active": gstin_active_resolved,
        "com_gst_vintage": resolve_com_gst_vintage(api, entity.get("date_of_registration")),
        "com_gst_filing_compliance": resolve_com_gst_filing_compliance(api),
        "com_filing_frequency": resolve_com_filing_frequency(api),
        "com_multiple_registrations": resolve_com_multiple_registrations(api, entity.get("gstin")),
        "com_hsn_match": resolve_com_hsn_match(api, entity),
        "com_bank_verification": resolve_com_bank_verification(entity),
        "com_gst_delay_days": resolve_com_gst_delay_days(api),
        "com_pf_filing_status": resolve_com_pf_filing_status(api),
        "addr_ownership_type": ownership_resolved,
        "addr_electricity_bill": electricity_resolved,
        "addr_rental_validation": rental_validation_resolved,
        "addr_msme": resolve_addr_msme(api, entity.get("udyam_number")),
        "addr_landlord_declaration": resolve_addr_landlord_declaration(
            ownership_resolved, docs.has("landlord_declaration")),
        "ident_pan_active": resolve_ident_pan_active(api, gstin_active_resolved.value),
        "ident_seller_type": resolve_ident_seller_type(entity.get("nature_of_business", ""),
                                                        entity.get("major_activity", "")),
        "ident_constitution": resolve_ident_constitution(entity.get("constitution", "")),
        "ident_pan_name_match": resolve_ident_pan_name_match(entity.get("pan_entity_name"), entity.get("legal_name")),
    }
    # Screen the firm *and* every partner/authorised signatory: the scoring model
    # asks for "proprietor and all partners" (PEP) and "firm and all directors"
    # (wilful defaulter), so an entity-name-only screen under-covers the parameter.
    out.update(resolve_aml(api, entity.get("legal_name") or entity.get("trade_name"),
                            partner_names=entity.get("partners")))
    return out

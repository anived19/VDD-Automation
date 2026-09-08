"""
End-to-end orchestration: vendor document folder -> extracted entity data
-> Finoscale Data API calls -> resolved scoring values -> scored report.

See README.md / plan doc for the pipeline design. Each API call is
independently try/except'd -- a vendor might be a proprietorship with no
CIN, or an endpoint might be temporarily unavailable, and that should
degrade individual resolver fields to "unresolved", never abort the run.

Policy: this pipeline never pauses for human input mid-run. Every field
either resolves to a value or resolves to "unresolved" with a reason -- the
report always finishes. Anything that resolved to a real value via
inference, a documented judgment call, or a genuine documentation gap
(rather than a clean, directly-sourced fact) is still surfaced, but as a
line in the post-generation summary (`VendorRunResult.cross_check_items`,
printed by `run_vendor.py`) for a human to review *after* the report
exists -- never as a blocking question during generation.
"""
import json
import os
import re
import traceback
from dataclasses import dataclass, field
from typing import List, Optional

from vdd.extract.classify import classify_folder, classify_content, ClassifiedDocs
from vdd.extract.ocr import extract_text, detect_diagonal_strike
from vdd.extract.parsers import parse_document
from vdd.finoscale_api.client import FinoscaleClient, FinoscaleAPIError
from vdd.resolve.resolvers import resolve_all, ApiBundle, Resolved
from vdd.score.engine import ScoringEngine
from vdd.report.build_context import build_context
from vdd.report.render import generate_report
from vdd.report.render import build as render_build

_FIELD_MERGE_MAP = {
    "gstin": ["gst_certificate", "kyc_form"],
    "legal_name": ["gst_certificate", "kyc_form"],
    "trade_name": ["gst_certificate"],
    "constitution": ["gst_certificate"],
    "address": ["gst_certificate"],
    "date_of_registration": ["gst_certificate"],
    "taxpayer_type": ["gst_certificate"],
    "state": ["gst_certificate"],
    "pan": ["pan_entity", "gst_certificate", "msme_certificate", "kyc_form"],
    "udyam_number": ["msme_certificate"],
    "enterprise_type": ["msme_certificate"],
    "major_activity": ["msme_certificate"],
    "organisation_type": ["msme_certificate"],
    # Udyam's own incorporation/formation date. Per "Recykal VDD - Custom
    # Instructions.md" rule 3 this must NOT drive the vintage field or score
    # (that stays GST-anchored) -- it's carried purely so the report can state
    # the entity's true formation date alongside the GST vintage.
    "date_of_incorporation": ["msme_certificate"],
    "nic_5_code": ["msme_certificate"],
    "nic_5_description": ["msme_certificate"],
    "nic_4_code": ["msme_certificate"],
    "nic_4_description": ["msme_certificate"],
    "mobile": ["msme_certificate", "kyc_form"],
    "email": ["msme_certificate", "kyc_form"],
    "bank_name": ["cancelled_cheque", "gst_portal"],
    "ifsc": ["cancelled_cheque", "gst_portal", "kyc_form"],
    "account_number": ["cancelled_cheque", "gst_portal"],
    "bank_name_msme": ["msme_certificate"],
    "ifsc_msme": ["msme_certificate"],
    "account_number_msme": ["msme_certificate"],
}


@dataclass
class VendorRunResult:
    vendor_name: str
    html_path: Optional[str]
    pdf_path: Optional[str]
    score: int
    unresolved_fields: List[str] = field(default_factory=list)
    missing_documents: List[str] = field(default_factory=list)
    extraction_warnings: List[str] = field(default_factory=list)
    api_errors: List[str] = field(default_factory=list)
    cross_check_items: List[str] = field(default_factory=list)
    # LLM review loop (vdd/review/) -- reviewed=False/approved=False and an
    # empty corrections/escalations list means the deterministic-only report
    # was produced, either because review was disabled/no key was
    # configured, or because the review graph errored out (see review_error)
    # and this pipeline fell back rather than aborting the run.
    reviewed: bool = False
    approved: bool = False
    review_iterations: int = 0
    corrections_applied: List[dict] = field(default_factory=list)
    escalations_for_human: List[dict] = field(default_factory=list)
    review_error: Optional[str] = None
    # Final classification state (post content-based-fallback reclassification --
    # see extract_entity()). Callers wanting a per-file "was this recognized
    # correctly" view (e.g. webapp/app.py) must use this, not a fresh
    # classify_folder() call -- that alone misses any file only classify_content()
    # caught, which classify_folder() never sees.
    docs: Optional["ClassifiedDocs"] = None
    review_trace_path: Optional[str] = None
    # {"input_tokens", "output_tokens", "total_tokens", "llm_call_count"} summed
    # across every review pass -- None when review didn't run (see _run_review).
    token_usage: Optional[dict] = None


@dataclass
class VendorComputation:
    """Output of the deterministic compute phase (extract -> API -> resolve ->
    score -> build_context), before any rendering. Split out from run_vendor
    so the LLM review graph (vdd/review/) can sit between this and the final
    render -- it reads/patches `resolved`/`entity` and re-derives `context`
    itself, it never touches this dataclass again after the first read."""
    vendor_name: str
    docs: ClassifiedDocs
    entity: dict
    api_bundle: ApiBundle
    resolved: dict
    context: dict
    unresolved_fields: List[str]
    missing_documents: List[str]
    cross_check_items: List[str]
    api_errors: List[str]
    warnings: List[str]


# Judgment-call / documentation-gap markers already used deliberately throughout
# resolvers.py's evidence notes (e.g. "NO SALE DEED ON FILE -- ownership is
# inferred", "GAP: premises are rented ... none is on file", "WARNING: the
# 'cancelled cheque' document shows no visible cancellation mark"). A resolved
# value carrying one of these isn't wrong -- it's the pipeline's best honest
# read of incomplete evidence -- but it's exactly the kind of thing a human
# reviewer should double-check after the report is generated, not something
# worth pausing generation over.
_CROSS_CHECK_MARKERS = ("gap:", "warning:", "critical:", "not screened", "inferred",
                         "not cross-checked", "needs a manual", "needs manual", "additional zigram finding",
                         "treated as a utility-record data error")


def find_cross_check_items(resolved: dict) -> List[str]:
    items = []
    for pid, r in resolved.items():
        note = (r.note or "").strip()
        if note and any(m in note.lower() for m in _CROSS_CHECK_MARKERS):
            items.append(f"{pid}: {note[:220]}{'...' if len(note) > 220 else ''}")
    return items


REQUIRED_DOC_TYPES = ["gst_certificate", "pan_entity", "cancelled_cheque", "msme_certificate",
                       "electricity_bill", "gst_portal"]

# MCA Corporate Identification Number: 1 char (L/U) + 5-digit industry code +
# 2-letter state code + 4-digit year + 3-letter company-type code + 6-digit
# registration number. No required document type carries this, so it's picked
# up opportunistically from whatever text any document happens to print it on
# (letterhead, incorporation certificate, board resolution, etc.) -- see
# extract_entity(). The format is distinctive enough that a false-positive
# match on unrelated text is effectively impossible.
_CIN_RE = re.compile(r'\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b')

# 4th character of a PAN encodes the holder's legal category (Income Tax Dept
# convention, fixed since PAN's introduction) -- confirmed 2026-09-08 against
# the vendors-data-api-reference.md's own example PAN (ANGPK6122Q, category
# "P"/individual), which Probe42's PnP endpoint accepted and returned a real
# Proprietorship record for. Used only to decide which Probe42 endpoint is
# even valid to call for a given PAN -- see fetch_api_data().
_PAN_HOLDER_CATEGORY = {
    "C": "company", "F": "firm", "P": "individual", "H": "huf", "A": "aop",
    "B": "boi", "G": "govt", "J": "juridical", "L": "local_authority", "T": "trust",
}

_MANUAL_OVERRIDES_PATH = "config/manual_bank_verification_overrides.json"


def _apply_manual_overrides(vendor_name: str, resolved: dict) -> None:
    """TEMPORARY, explicit stand-in for com_bank_verification while
    ongrid.bank-verification.verify 403s on this API key -- see
    config/manual_bank_verification_overrides.json's own '_purpose' note for
    the full story. Only fires for the exact vendor names listed in that
    file; every other vendor's run is completely unaffected. Never silently
    invents a result -- if the file or a vendor's entry is missing, this is
    a no-op and the field stays whatever resolve_all() actually determined."""
    if not os.path.exists(_MANUAL_OVERRIDES_PATH):
        return
    with open(_MANUAL_OVERRIDES_PATH, encoding="utf-8") as f:
        overrides = json.load(f)
    o = overrides.get(vendor_name)
    if not isinstance(o, dict) or "value" not in o:
        return
    resolved["com_bank_verification"] = Resolved.ok(
        o["value"], "manual-override:analyst-reference-report", note=o.get("note", ""))


def extract_entity(docs: ClassifiedDocs, warnings: List[str], cache_dir: Optional[str] = None) -> dict:
    # Content-based fallback for files the filename regex couldn't place --
    # e.g. "WhatsApp Image 2026-08-13 at 2.44.58 PM.jpeg" carries zero
    # filename signal even when it's a perfectly good cancelled cheque or PAN
    # card. Mutates `docs` in place (moves the file from unmatched into
    # by_type) so the existing extraction loop below picks it up naturally,
    # and so downstream `docs.has(...)` presence checks in resolvers.py see
    # the corrected classification too. This means a file that was originally
    # unmatched gets its text extracted twice (once here, once in the loop
    # below) -- an acceptable cost for the rare unmatched case, not worth the
    # extra plumbing to dedupe for what should be a small list.
    cin_found = None

    still_unmatched = []
    for f in docs.unmatched:
        r = extract_text(f, cache_dir=cache_dir)
        if cin_found is None and r.text:
            m = _CIN_RE.search(r.text)
            if m:
                cin_found = m.group(0)
        guessed = classify_content(r.text) if r.confident else None
        if guessed:
            warnings.append(f"{os.path.basename(f)}: filename didn't match any known document type, but its "
                             f"content was recognized as '{guessed}' -- reclassified automatically")
            docs.by_type.setdefault(guessed, []).append(f)
        else:
            if not r.confident:
                warnings.append(f"{os.path.basename(f)}: unmatched by filename, and text extraction was also "
                                 f"unavailable -- could not attempt content-based classification")
            still_unmatched.append(f)
    docs.unmatched = still_unmatched

    doc_data = {}
    for doc_type, files in docs.by_type.items():
        for f in files:
            r = extract_text(f, cache_dir=cache_dir)
            if cin_found is None and r.text:
                m = _CIN_RE.search(r.text)
                if m:
                    cin_found = m.group(0)
            if not r.confident:
                warnings.append(f"{os.path.basename(f)} ({doc_type}): text extraction unavailable "
                                 f"(no digital text layer, and neither Tesseract nor vision fallback "
                                 f"produced usable output)")
            parsed = parse_document(doc_type, r.text)
            if doc_type == "cancelled_cheque":
                # Independent of whatever OCR text extraction found (or didn't --
                # this still runs even when r.text is empty): a classical
                # CV diagonal-line check on the image itself, since reading the
                # handwritten "cancelled" mark is a handwriting-recognition
                # problem no OCR engine here promises to solve. A positive from
                # either channel is enough; None (no image / no OpenCV) leaves
                # the OCR-text-based signal as-is rather than overriding it.
                visual_mark = detect_diagonal_strike(f, rotation=r.rotation)
                if visual_mark is not None:
                    parsed["cancellation_mark_present"] = parsed.get("cancellation_mark_present", False) or visual_mark
            doc_data.setdefault(doc_type, {}).update(parsed)

    entity = {}
    for field_name, priority in _FIELD_MERGE_MAP.items():
        for doc_type in priority:
            v = doc_data.get(doc_type, {}).get(field_name)
            if v:
                entity[field_name] = v
                break

    # These need to stay distinguishable by which document they came from --
    # the generic priority-merge above collapses same-named fields across doc
    # types, which is wrong here (an owner's personal PAN card name and the
    # entity's own PAN card name mean different things).
    if doc_data.get("pan_entity", {}).get("name"):
        entity["pan_entity_name"] = doc_data["pan_entity"]["name"]
    if doc_data.get("pan_owner", {}).get("name"):
        entity["pan_owner_name"] = doc_data["pan_owner"]["name"]
    if doc_data.get("pan_owner", {}).get("pan"):
        entity["pan_owner_pan"] = doc_data["pan_owner"]["pan"]
    if doc_data.get("electricity_bill", {}).get("address"):
        entity["electricity_bill_address"] = doc_data["electricity_bill"]["address"]
    if doc_data.get("electricity_bill", {}).get("pincode"):
        entity["electricity_bill_pincode"] = doc_data["electricity_bill"]["pincode"]
    for src_key, dest_key in (("activity", "electricity_bill_activity"),
                               ("consumer_name", "electricity_bill_consumer_name"),
                               ("village", "electricity_bill_village"),
                               ("date_of_connection", "electricity_bill_connection_date"),
                               ("sanctioned_load", "electricity_bill_sanctioned_load"),
                               ("category", "electricity_bill_category")):
        v = doc_data.get("electricity_bill", {}).get(src_key)
        if v:
            entity[dest_key] = v
    if doc_data.get("cancelled_cheque", {}).get("cancellation_mark_present") is not None:
        entity["cheque_cancellation_mark_present"] = doc_data["cancelled_cheque"]["cancellation_mark_present"]
    if doc_data.get("gst_portal", {}).get("bank_verified") is not None:
        entity["gst_portal_bank_verified"] = doc_data["gst_portal"]["bank_verified"]
    if doc_data.get("gst_portal", {}).get("account_status"):
        entity["gst_portal_account_status"] = doc_data["gst_portal"]["account_status"]
    if doc_data.get("gst_portal", {}).get("type_of_account"):
        entity["gst_portal_account_type"] = doc_data["gst_portal"]["type_of_account"]
    for key in ("account_number", "bank_name", "ifsc"):
        v = doc_data.get("gst_portal", {}).get(key)
        if v:
            entity[f"gst_portal_{key}"] = v
    for key in ("account_number", "bank_name", "ifsc"):
        v = doc_data.get("cancelled_cheque", {}).get(key)
        if v:
            entity[f"cheque_{key}"] = v
    if doc_data.get("kyc_form", {}).get("account_number"):
        entity["kyc_form_account_number"] = doc_data["kyc_form"]["account_number"]
    # Corroborating evidence for a SEPARATE, licensed manufacturing premises --
    # not a substitute for the GST-registered address itself. See
    # resolve_addr_electricity_bill's use of these (2026-09-07: confirmed real
    # on Skandan, whose factory is a licensed premises distinct from its
    # registered office, independently corroborated by both documents).
    if doc_data.get("factory_license", {}).get("premises_address"):
        entity["factory_premises_address"] = doc_data["factory_license"]["premises_address"]
    if doc_data.get("pcb_certificate", {}).get("premises_address"):
        entity["pcb_premises_address"] = doc_data["pcb_certificate"]["premises_address"]
    if cin_found:
        entity["cin"] = cin_found

    return entity


def _extract_partners(api: ApiBundle) -> List[str]:
    """Shared by `enrich_entity_from_api` (display) and `fetch_api_data` (needs
    partner names *during* the same API-fetch pass, before entity is enriched,
    to screen each partner individually)."""
    def _dig(d, *path):
        cur = d
        for p in path:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(p)
        return cur

    partners = _dig(api.ongrid_detailed, "gstin_data", "directors")
    if partners:
        return [str(p).strip() for p in partners if str(p).strip()]
    owners = _dig(api.probe42_pnp, "owners")
    if owners:
        return [str(p).strip() for p in owners if str(p).strip()]
    return []


def enrich_entity_from_api(entity: dict, api: ApiBundle) -> None:
    """Add API-sourced *display* fields to the entity dict (in place).

    Kept separate from `extract_entity` because these come from the Data API,
    not the document set, and are only needed once `fetch_api_data` has run.
    None of these feed a scoring resolver -- resolvers read the ApiBundle
    directly -- they exist so the rendered report can show the HSN table and
    the authoritative nature-of-business text instead of leaving them blank.
    """
    def _dig(d, *path):
        cur = d
        for p in path:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(p)
        return cur

    goods = _dig(api.ongrid_detailed, "gstin_data", "hsn_data", "goods") or []
    services = _dig(api.ongrid_detailed, "gstin_data", "hsn_data", "services") or []
    hsn_rows = [(g.get("hsn") or g.get("sac") or "", g.get("description") or "")
                for g in list(goods) + list(services) if isinstance(g, dict)]
    if hsn_rows:
        entity["declared_hsn"] = [(c, d) for c, d in hsn_rows if c]

    api_nature = _dig(api.ongrid_detailed, "gstin_data", "principal_address", "nature_of_business_activity")
    if api_nature:
        entity["gst_nature_of_business_activity"] = api_nature
    turnover = _dig(api.ongrid_detailed, "gstin_data", "annual_aggregate_turnover")
    if turnover:
        entity["gst_annual_aggregate_turnover"] = turnover
        entity["gst_annual_aggregate_turnover_year"] = _dig(
            api.ongrid_detailed, "gstin_data", "annual_aggregate_turnover_year")
    # Ongrid's MSME verification returns the NIC codes and formation date straight
    # from the Udyam registry, i.e. live-verified rather than regex-scraped off a
    # printed certificate -- so it takes priority over the document-parsed values.
    nic = _dig(api.ongrid_msme, "nic_data") or {}
    for width, key in ((5, "nic_5_digit"), (4, "nic_4_digit"), (2, "nic_2_digit")):
        raw = nic.get(key)
        if not raw:
            continue
        m = re.match(r'\s*(\d+)\s*-\s*(.+)', str(raw))
        if m:
            entity[f"nic_{width}_code"], entity[f"nic_{width}_description"] = m.group(1), m.group(2).strip()
    ent = _dig(api.ongrid_msme, "enterprise_data") or {}
    if ent.get("date_of_incorporation"):
        d = str(ent["date_of_incorporation"])
        m = re.match(r'(\d{4})-(\d{2})-(\d{2})$', d)
        entity["date_of_incorporation"] = f"{m.group(3)}/{m.group(2)}/{m.group(1)}" if m else d
    if ent.get("enterprise_type"):
        entity["enterprise_type"] = ent["enterprise_type"]
    if ent.get("classification_year"):
        entity["msme_classification_year"] = ent["classification_year"]
    if _dig(api.ongrid_msme, "enterprise_data", "address", "pincode"):
        entity["udyam_pincode"] = api.ongrid_msme["enterprise_data"]["address"]["pincode"]

    partners = _extract_partners(api)
    if partners:
        entity["partners"] = partners


def fetch_api_data(client: Optional[FinoscaleClient], entity: dict, vendor_name: str,
                    api_errors: List[str]) -> ApiBundle:
    if client is None:
        return ApiBundle()
    bundle = ApiBundle()
    pan, gstin = entity.get("pan"), entity.get("gstin")

    def _try(label, fn):
        try:
            return fn()
        except FinoscaleAPIError as e:
            api_errors.append(f"{label}: [{e.status_code}] {e.message}")
        except Exception as e:
            api_errors.append(f"{label}: {e}")
        return None

    # Independent of PAN/GSTIN -- runs whenever a bank account number + IFSC
    # were extracted from any document (cancelled cheque / GST portal). A
    # failed call (network/auth/etc, caught by `_try`) leaves this None, which
    # resolve_com_bank_verification treats as "fall back to GST-portal
    # inference", never as a scored negative.
    account_number, ifsc = entity.get("account_number"), entity.get("ifsc")
    if account_number and ifsc:
        bundle.bank_verification = _try("ongrid.bank-verification.verify",
                                         lambda: client.ongrid_bank_verification_verify(account_number, ifsc))

    if gstin:
        bundle.ongrid_detailed = _try("ongrid.fetch-detailed",
                                       lambda: client.ongrid_gstin_fetch_detailed(gstin))
    if pan:
        bundle.ongrid_by_pan = _try("ongrid.fetch-by-pan", lambda: client.ongrid_gstin_fetch_by_pan(pan))
        bundle.ongrid_msme = _try("ongrid.msme.fetch-by-pan", lambda: client.ongrid_msme_fetch_by_pan(pan))
        client_org_id = re.sub(r'[^A-Za-z0-9]+', '-', vendor_name).strip('-').lower() or "vdd-run"
        bundle.digitap = _try("digitap.pan-and-gst",
                               lambda: client.digitap_pan_and_gst(pan, client_org_id))
        # Digitap answers HTTP 200 with per-sub-call failures in an inner `errors`
        # object, so a broken response otherwise looks like a clean empty one.
        # Surface it, or the run summary silently under-reports the data gaps.
        inner = (bundle.digitap or {}).get("errors") if isinstance(bundle.digitap, dict) else None
        if inner:
            api_errors.append("digitap.pan-and-gst: [200 with inner errors] "
                               + "; ".join(f"{k}: {v}" for k, v in inner.items()))
        # Probe42 routing depends on entity type -- confirmed 2026-09-08 with live,
        # non-cached calls: `fetch-comprehensive-details-pnp` is documented as
        # Proprietorship/Partnership-only and returned a real record for the
        # reference doc's own example Proprietorship PAN, but 500s ("null value in
        # column 'value' ... violates not-null constraint") on EVERY company PAN
        # tried, big or small (Skandan Plastrix Pvt Ltd AND Godrej Properties Ltd,
        # a top-tier listed company -- so it's not a data-coverage gap, it's the
        # wrong endpoint for that entity shape). `fetch-comprehensive-details-by-page`
        # and `-for-entity` are CIN-keyed (its own doc example uses a CIN, not a
        # PAN), so passing a PAN into `by-page` was also wrong input, independent
        # of the 403 that endpoint separately returns on this API key.
        cin = entity.get("cin")
        if cin:
            bundle.probe42_compliance = _try("probe42.compliance",
                                              lambda: client.probe42_fetch_by_page(cin, "compliance"))
            bundle.probe42_for_entity = _try("probe42.for-entity",
                                              lambda: client.probe42_fetch_for_entity(cin))
        else:
            pan_category = _PAN_HOLDER_CATEGORY.get(pan[3].upper()) if len(pan) >= 4 else None
            if pan_category in ("firm", "individual"):
                bundle.probe42_pnp = _try("probe42.pnp", lambda: client.probe42_fetch_pnp(pan))
            # else: PAN category is company/huf/aop/boi/govt/juridical/trust (or
            # unreadable) and no CIN was found anywhere in the document set -- no
            # Probe42 endpoint here is valid to call for that combination, so none
            # is attempted. Left as api.probe42_* = None; resolvers that depend on
            # this (e.g. resolve_com_pf_filing_status) already report *why* a field
            # stayed unresolved rather than silently showing nothing.

        # Zigram RE-ENABLED 2026-09-08 as the primary AML sweep. NOTE: an initial
        # hypothesis that the previously-documented "~3% reliability" finding was
        # a request-format bug (arbitrary clientId, ISO country code) on our side
        # did NOT hold up on a same-session re-check -- see
        # vdd/aml/zigram_screening.py's module docstring for the full,
        # self-corrected story. What IS confirmed from real, non-cached calls
        # today: comprehensive ~63-category responses, 3 for 3, whenever a real
        # pan=/cin= was included in the request.
        from vdd.aml.zigram_screening import zigram_full_screen
        zigram_entity_name = entity.get("legal_name") or entity.get("trade_name") or vendor_name
        bundle.zigram = _try("zigram.screening",
                              lambda: zigram_full_screen(client, zigram_entity_name, "Organization",
                                                          pan, identifier_kind="pan"))
        # Owner-level screen is best-effort and unconfirmed (see module docstring) --
        # only attempted when we actually extracted the owner's own PAN from a
        # pan_owner document, never with a placeholder identifier.
        owner_pan = entity.get("pan_owner_pan")
        if owner_pan:
            owner_name = entity.get("pan_owner_name") or "owner"
            bundle.zigram_owner = _try("zigram.screening[owner]",
                                        lambda: zigram_full_screen(client, owner_name, "Individual",
                                                                    owner_pan, identifier_kind="pan"))
    return bundle


def compute_vendor_data(docs_path: str, client: Optional[FinoscaleClient] = None,
                         scoring_model_path: str = "config/scoring_model.json",
                         ocr_cache_dir: str = "cache") -> VendorComputation:
    """Everything up to and including build_context() -- no rendering. Split
    out of run_vendor so the LLM review graph can be invoked in between this
    and the final HTML/PDF write."""
    vendor_name = os.path.basename(os.path.normpath(docs_path))
    docs = classify_folder(docs_path)

    warnings: List[str] = []
    entity = extract_entity(docs, warnings, cache_dir=ocr_cache_dir)

    missing = [dt for dt in REQUIRED_DOC_TYPES if not docs.has(dt)]

    api_errors: List[str] = []
    api_bundle = fetch_api_data(client, entity, vendor_name, api_errors)
    enrich_entity_from_api(entity, api_bundle)

    resolved = resolve_all(entity, docs, api_bundle)
    _apply_manual_overrides(vendor_name, resolved)
    unresolved_fields = [pid for pid, r in resolved.items() if r.unresolved]
    cross_check_items = find_cross_check_items(resolved)

    engine = ScoringEngine(scoring_model_path)
    result = engine.score_no_consent(resolved)
    context = build_context(entity, result)

    if docs.unmatched:
        warnings.append(f"{len(docs.unmatched)} file(s) in the folder didn't match any known document type: "
                         + ", ".join(os.path.basename(f) for f in docs.unmatched))

    return VendorComputation(
        vendor_name=vendor_name, docs=docs, entity=entity, api_bundle=api_bundle, resolved=resolved,
        context=context, unresolved_fields=unresolved_fields, missing_documents=missing,
        cross_check_items=cross_check_items, api_errors=api_errors, warnings=warnings,
    )


def _run_review(computed: VendorComputation, client: Optional[FinoscaleClient],
                 scoring_model_path: str, out_dir: str, max_review_iterations: Optional[int],
                 warnings: List[str]) -> dict:
    """Runs the LLM review graph and returns a dict of VendorRunResult fields
    to merge in. Never raises -- any failure (missing key, Gemini/OpenAI
    outage, a tool crashing, a malformed structured response, ...) is caught
    here and degrades to the deterministic-only report, exactly like every
    other external call in this pipeline (fetch_api_data's per-call
    try/except, the AML screeners' fail-closed behavior, etc). The run must
    never abort over a review failure."""
    try:
        from vdd.review.model import select_provider
        provider = select_provider()
    except Exception as e:
        # Covers langchain/langgraph not being installed yet (ImportError) as
        # well as a bad LLM_PROVIDER value -- either way, degrade rather than
        # crash every run just because the review layer isn't set up.
        warnings.append(f"LLM review skipped -- could not determine an LLM provider ({e}).")
        return {"context": computed.context}

    if provider is None:
        warnings.append("LLM review skipped -- no GEMINI_API_KEY/OPENAI_API_KEY configured.")
        return {"context": computed.context}

    try:
        from vdd.review.graph import DEFAULT_MAX_ITERATIONS, build_review_graph
        from vdd.review.trace import summarize_usage, write_review_trace

        graph = build_review_graph()
        init_state = {
            "vendor_name": computed.vendor_name,
            "scoring_model_path": scoring_model_path,
            "client": client,
            "entity": computed.entity,
            "resolved": computed.resolved,
            "context": computed.context,
            "html": render_build(computed.context),
            "cross_check_items": computed.cross_check_items,
            "iteration": 1,
            "max_iterations": max_review_iterations or DEFAULT_MAX_ITERATIONS,
            "passes": [], "findings": [], "corrections_applied": [], "escalations": [],
            "message_traces": [], "pass_usage": [],
        }
        final_state = graph.invoke(init_state)
        review_trace_path = write_review_trace(computed.vendor_name, out_dir, final_state)
        return {
            "context": final_state["context"],
            "reviewed": True,
            "approved": final_state.get("approved", False),
            "review_iterations": max(0, final_state.get("iteration", 1) - 1),
            "corrections_applied": final_state.get("corrections_applied", []),
            "escalations_for_human": final_state.get("escalations", []),
            "review_trace_path": review_trace_path,
            "token_usage": summarize_usage(final_state.get("pass_usage", [])),
        }
    except Exception as e:
        warnings.append(f"LLM review failed, falling back to the deterministic report: {e}")
        return {"context": computed.context, "review_error": str(e)}


def run_vendor(docs_path: str, out_dir: str, client: Optional[FinoscaleClient] = None,
               scoring_model_path: str = "config/scoring_model.json",
               ocr_cache_dir: str = "cache", review: bool = True,
               max_review_iterations: Optional[int] = None) -> VendorRunResult:
    computed = compute_vendor_data(docs_path, client, scoring_model_path, ocr_cache_dir)
    warnings = list(computed.warnings)

    review_fields: dict = {"context": computed.context}
    if review:
        review_fields = _run_review(computed, client, scoring_model_path, out_dir,
                                     max_review_iterations, warnings)
    context = review_fields["context"]

    html_path, pdf_path = None, None
    try:
        html_path, pdf_path = generate_report(context, out_dir)
    except Exception:
        # PDF rendering can fail purely on missing system libs (e.g. weasyprint's
        # GTK/Pango/Cairo deps aren't installed) -- still emit the HTML.
        try:
            html_path, _ = generate_report(context, out_dir, html_only=True)
            warnings.append("PDF rendering failed (weasyprint/system libs) -- HTML report generated instead: "
                             + traceback.format_exc(limit=1).splitlines()[-1])
        except Exception as e2:
            warnings.append(f"Report generation failed entirely: {e2}")

    return VendorRunResult(
        vendor_name=computed.vendor_name, html_path=html_path, pdf_path=pdf_path, score=context["score"],
        unresolved_fields=computed.unresolved_fields, missing_documents=computed.missing_documents,
        extraction_warnings=warnings, api_errors=computed.api_errors, cross_check_items=computed.cross_check_items,
        reviewed=review_fields.get("reviewed", False), approved=review_fields.get("approved", False),
        review_iterations=review_fields.get("review_iterations", 0),
        corrections_applied=review_fields.get("corrections_applied", []),
        escalations_for_human=review_fields.get("escalations_for_human", []),
        review_error=review_fields.get("review_error"), review_trace_path=review_fields.get("review_trace_path"),
        docs=computed.docs, token_usage=review_fields.get("token_usage"),
    )

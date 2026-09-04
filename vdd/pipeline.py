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
import os
import re
import traceback
from dataclasses import dataclass, field
from typing import List, Optional

from vdd.extract.classify import classify_folder, classify_content, ClassifiedDocs
from vdd.extract.ocr import extract_text
from vdd.extract.parsers import parse_document
from vdd.finoscale_api.client import FinoscaleClient, FinoscaleAPIError
from vdd.resolve.resolvers import resolve_all, ApiBundle
from vdd.score.engine import ScoringEngine
from vdd.report.build_context import build_context
from vdd.report.render import generate_report

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


# Judgment-call / documentation-gap markers already used deliberately throughout
# resolvers.py's evidence notes (e.g. "NO SALE DEED ON FILE -- ownership is
# inferred", "GAP: premises are rented ... none is on file", "WARNING: the
# 'cancelled cheque' document shows no visible cancellation mark"). A resolved
# value carrying one of these isn't wrong -- it's the pipeline's best honest
# read of incomplete evidence -- but it's exactly the kind of thing a human
# reviewer should double-check after the report is generated, not something
# worth pausing generation over.
_CROSS_CHECK_MARKERS = ("gap:", "warning:", "critical:", "not screened", "inferred",
                         "not cross-checked", "needs a manual", "needs manual",
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
    still_unmatched = []
    for f in docs.unmatched:
        r = extract_text(f, cache_dir=cache_dir)
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
            if not r.confident:
                warnings.append(f"{os.path.basename(f)} ({doc_type}): text extraction unavailable "
                                 f"(no digital text layer, and neither Tesseract nor vision fallback "
                                 f"produced usable output)")
            parsed = parse_document(doc_type, r.text)
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
        bundle.probe42_compliance = _try("probe42.compliance",
                                          lambda: client.probe42_fetch_by_page(pan, "compliance"))
        bundle.probe42_pnp = _try("probe42.pnp", lambda: client.probe42_fetch_pnp(pan))

        # Zigram is DISABLED here deliberately (2026-09-04, user decision) -- it's a
        # paid-per-call endpoint, no resolver uses its result (see resolve_aml's
        # docstring: the response is unreliable, ~1-in-30 hit rate for real data
        # in live testing, with a near-empty stub the rest of the time), and every
        # run of this pipeline was silently paying for calls whose output was
        # thrown away. Do not re-enable by just uncommenting this -- get an answer
        # from whoever manages the Finoscale/Zigram account about the reliability
        # issue first (see resolve_aml's docstring for the exact reproducible
        # numbers to hand them), then re-decide whether/how to call it.
        #
        # zigram_entity_name = entity.get("legal_name") or entity.get("trade_name") or vendor_name
        # bundle.zigram = _try("zigram.screening",
        #                       lambda: client.zigram_screening(entity_name=zigram_entity_name, client_id=client_org_id,
        #                                                        type_="Organization", country=["IN"], pan=pan))
        # partner_results = []
        # for partner in _extract_partners(bundle):
        #     res = _try(f"zigram.screening[{partner}]",
        #                 lambda partner=partner: client.zigram_screening(
        #                     entity_name=partner, client_id=client_org_id, type_="Individual", country=["IN"]))
        #     partner_results.append({"name": partner, "result": res})
        # if partner_results:
        #     bundle.zigram_partners = partner_results
    return bundle


def run_vendor(docs_path: str, out_dir: str, client: Optional[FinoscaleClient] = None,
               scoring_model_path: str = "config/scoring_model.json",
               ocr_cache_dir: str = "cache") -> VendorRunResult:
    vendor_name = os.path.basename(os.path.normpath(docs_path))
    docs = classify_folder(docs_path)

    warnings: List[str] = []
    entity = extract_entity(docs, warnings, cache_dir=ocr_cache_dir)

    missing = [dt for dt in REQUIRED_DOC_TYPES if not docs.has(dt)]

    api_errors: List[str] = []
    api_bundle = fetch_api_data(client, entity, vendor_name, api_errors)
    enrich_entity_from_api(entity, api_bundle)

    resolved = resolve_all(entity, docs, api_bundle)
    unresolved_fields = [pid for pid, r in resolved.items() if r.unresolved]
    cross_check_items = find_cross_check_items(resolved)

    engine = ScoringEngine(scoring_model_path)
    result = engine.score_no_consent(resolved)
    context = build_context(entity, result)

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

    if docs.unmatched:
        warnings.append(f"{len(docs.unmatched)} file(s) in the folder didn't match any known document type: "
                         + ", ".join(os.path.basename(f) for f in docs.unmatched))

    return VendorRunResult(
        vendor_name=vendor_name, html_path=html_path, pdf_path=pdf_path, score=context["score"],
        unresolved_fields=unresolved_fields, missing_documents=missing,
        extraction_warnings=warnings, api_errors=api_errors, cross_check_items=cross_check_items,
    )

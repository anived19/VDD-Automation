"""
Two checks on the document set as a whole, run once every file has been
read and parsed, before any resolver sees the merged entity:

1. Read quality -- for each document, how much of what that document type
   is expected to carry was actually recovered, plus the extraction method.
   A blurry PAN card that yielded no PAN number shows up as a "poorly read"
   line naming the missing fields, instead of as a mysteriously unresolved
   score three rows later.

2. Consistency -- every identifier or name a document carries is checked
   against the GST certificate's (the anchor: it is the one document every
   vendor must have, and its GSTIN embeds the PAN). Disagreements are
   warnings: a wrong-folder document, a stale certificate, or an OCR garble
   that happened to still look like a valid number all surface here as one
   plain line each, instead of silently corrupting a field.

Both are reported through the same warnings list the rest of the pipeline
uses, so they land in the run output, the Flags sheet of the review
workbook and the reviewer's prompt. Neither changes any score.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

from vdd.extract.parsers import pan_from_gstin

# What a well-read document of each type yields. Only fields a parser can
# actually produce for that type, so "expected" means "normally present".
EXPECTED_FIELDS: dict[str, tuple[str, ...]] = {
    "gst_certificate": ("gstin", "legal_name", "constitution", "address", "date_of_registration"),
    "msme_certificate": ("udyam_number", "enterprise_name", "nic_5_code", "address"),
    "cancelled_cheque": ("ifsc", "account_number"),
    "gst_portal": ("account_number", "ifsc", "account_status"),
    "pan_entity": ("pan", "name"),
    "pan_owner": ("pan", "name"),
    "kyc_form": ("gstin", "pan"),
    "certificate_of_incorporation": ("name", "cin", "pan", "date_of_incorporation"),
    "electricity_bill": ("consumer_name", "address"),
    "factory_license": ("premises_address",),
    "pcb_certificate": ("premises_address",),
}

_PAN_RE = re.compile(r'^[A-Z]{5}[0-9]{4}[A-Z]$')


@dataclass
class DocumentRead:
    path: str
    doc_type: str
    method: str          # ExtractionResult.method
    confident: bool
    parsed: dict
    reason: str = ""      # ExtractionResult.reason -- why nothing (or not everything) was read
    expected: tuple = ()
    missing: tuple = ()

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def quality(self) -> str:
        if not self.confident:
            return "unreadable"
        if not self.expected:
            return "read"
        got = len(self.expected) - len(self.missing)
        if got == len(self.expected):
            return "read"
        return "partial" if got else "poor"


@dataclass
class ConsistencyReport:
    reads: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def assess_reads(reads: list[DocumentRead]) -> None:
    """Fill each read's expected/missing from EXPECTED_FIELDS."""
    for r in reads:
        r.expected = EXPECTED_FIELDS.get(r.doc_type, ())
        r.missing = tuple(f for f in r.expected if not r.parsed.get(f))


def _norm_name(s: str) -> set:
    s = re.sub(r'^\s*m\s*/?\s*s\.?\s+', '', (s or "").lower())
    s = re.sub(r'\b(private|pvt|limited|ltd|llp|co|company|and|&)\b\.?', ' ', s)
    s = s.replace('.', ' ')   # "B.R." are two initials, not the word "br"
    return set(re.sub(r'[^a-z0-9 ]', '', s).split())


def _names_agree(a: Optional[str], b: Optional[str]) -> Optional[bool]:
    """Token-overlap agreement between two names, with the usual prefixes and
    corporate suffixes ignored. None when either side is empty."""
    if not a or not b:
        return None
    sa, sb = _norm_name(a), _norm_name(b)
    if not sa or not sb:
        return None
    shared = sa & sb
    # An initial stands for any word starting with that letter: "NAVIN K" is
    # "KANNUSAMY NAVIN" on a PAN card (director, Skandan Plastrix, 2026-09-18).
    # Initials only count alongside at least one full shared word.
    conflict = False
    for x, y in ((sa, sb), (sb, sa)):
        for tok in x:
            if len(tok) != 1:
                continue
            if any(w != tok and w[0] == tok for w in y):
                shared = shared | {tok}
            elif tok not in y:
                conflict = True   # an initial nothing on the other side can stand for
    full_shared = {t for t in shared if len(t) > 1}
    words_a, words_b = {t for t in sa if len(t) > 1}, {t for t in sb if len(t) > 1}
    if conflict and words_a and words_a == words_b:
        return False   # "A PERSON" vs "B PERSON": only the initial differs -- different people
    # One shared word is not agreement -- "EXAMPLE PARTNER" and "ANOTHER PARTNER"
    # share a word and are different people. Two shared words (an initial may be
    # one of them), or the whole of a one-word name, is; otherwise fall back to a
    # whole-string character ratio so an OCR-garbled word ("TRAIING") still counts.
    if (len(shared) >= 2 and full_shared) or (min(len(sa), len(sb)) == 1 and shared):
        return True
    from difflib import SequenceMatcher
    return SequenceMatcher(None, " ".join(sorted(sa)), " ".join(sorted(sb))).ratio() >= 0.8


def check_consistency(reads: list[DocumentRead], partners: Optional[list] = None) -> ConsistencyReport:
    """Read-quality lines and cross-document disagreements, anchored on the
    GST certificate. `partners` (names from the GST annexure or the API) let
    an owner's PAN card be recognised as a partner's rather than flagged."""
    assess_reads(reads)
    report = ConsistencyReport(reads=reads)
    w = report.warnings

    for r in reads:
        if r.quality == "unreadable":
            w.append(f"{r.name} ({r.doc_type}): could not be read -- "
                     + (r.reason or "no text layer and OCR found nothing usable; the file may be blank, encrypted, or too blurry"))
        elif r.quality in ("poor", "partial"):
            w.append(f"{r.name} ({r.doc_type}): {r.quality.upper()} READ via {r.method} -- missing "
                     f"{', '.join(r.missing)}" + (f" ({r.reason})" if r.reason else "")
                     + "; re-scan or re-upload if these matter")
        elif r.reason:
            w.append(f"{r.name} ({r.doc_type}): {r.reason}")

    gst = next((r for r in reads if r.doc_type == "gst_certificate" and r.parsed.get("gstin")), None)
    if gst is None:
        if any(r.doc_type != "gst_certificate" for r in reads):
            w.append("No readable GST certificate in the document set -- cross-document identifier checks "
                     "(PAN, GSTIN, names) could not be run")
        return report

    gstin = gst.parsed["gstin"]
    anchor_pan = gst.parsed.get("pan") or pan_from_gstin(gstin)
    legal = gst.parsed.get("legal_name")
    trade = gst.parsed.get("trade_name")
    entity_names = [n for n in (legal, trade) if n]
    partner_names = [p for p in (partners or []) if p]

    for r in reads:
        if r is gst:
            continue
        p = r.parsed
        # ---- GSTIN
        g = p.get("gstin")
        if g and g != gstin:
            w.append(f"{r.name} ({r.doc_type}): carries GSTIN {g}, but the GST certificate is {gstin} -- "
                     "this file may belong to a different vendor or registration")
        # ---- PAN
        pan = p.get("pan")
        if pan and anchor_pan and pan != anchor_pan:
            if r.doc_type == "pan_owner":
                pass  # an owner's / partner's own PAN is expected to differ
            elif r.doc_type == "pan_entity" and _PAN_RE.match(pan) and pan[3] == "P":
                w.append(f"{r.name} (pan_entity): PAN {pan} is an individual's (4th letter P), not the entity's "
                         f"{anchor_pan} -- likely an owner's card filed as the entity's")
            else:
                w.append(f"{r.name} ({r.doc_type}): carries PAN {pan}, but the GST certificate's PAN is "
                         f"{anchor_pan} -- this file may belong to a different vendor (or the number was misread)")
        # ---- names
        for key in ("legal_name", "enterprise_name", "name", "consumer_name", "account_holder"):
            n = p.get(key)
            if not n:
                continue
            if r.doc_type == "pan_owner":
                if partner_names and not any(_names_agree(n, pn) for pn in partner_names) \
                        and not any(_names_agree(n, en) for en in entity_names):
                    w.append(f"{r.name} (pan_owner): '{n}' is not one of the partners/directors on record "
                             f"({', '.join(partner_names)}) -- confirm whose PAN card this is")
                continue
            if key == "consumer_name":
                continue  # handled by resolve_addr_ownership_type with the right nuance (landlord's meter)
            agree = any(_names_agree(n, en) for en in entity_names)
            if entity_names and not agree:
                who = next((pn for pn in partner_names if _names_agree(n, pn)), None)
                if who:
                    w.append(f"{r.name} ({r.doc_type}): in the name of partner/director '{who}', not the entity "
                             f"('{legal}') -- acceptable for a proprietorship, otherwise confirm")
                else:
                    w.append(f"{r.name} ({r.doc_type}): name '{n}' does not match the GST legal/trade name "
                             f"('{legal}'{f' / {trade}' if trade and trade != legal else ''}) -- wrong vendor, "
                             "stale document, or a misread")
        # ---- GST certificate vs KYC-form / portal GSTIN handled above; Udyam PAN via generic PAN check
    return report


def read_quality_rows(reads: list[DocumentRead]) -> list[dict]:
    """For the review workbook's Documents sheet and the webapp's verification
    panel: one row per file."""
    return [{"file": r.name, "doc_type": r.doc_type, "method": r.method, "quality": r.quality,
             "missing": ", ".join(r.missing), "reason": r.reason}
            for r in sorted(reads, key=lambda x: x.name.lower())]

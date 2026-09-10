"""
Classify a vendor's document files by filename, and locate the folder that
actually holds them (vendor folders often have a same-named inner subfolder
-- see `Recykal VDD Tool Setup/PROMPT.md` step 1).
"""
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Ported from PROMPT.md step 2. Order matters -- first match wins.
DOC_TYPE_PATTERNS: Dict[str, List[str]] = {
    "gst_certificate": [r"gst[\s_-]*cert", r"\bgst\b.*\.pdf$"],
    "gst_portal": [r"gst[\s_-]*portal", r"gst[\s_-]*snap", r"gst\s*proof",
                   r"bank\s*detail", r"new\s*b[an]?ak?\s*detail"],
    "cancelled_cheque": [r"cancel+ed?[\s_-]*cheque", r"cancel\s*cheque", r"\bcheque\b"],
    "pan_owner": [r"owner[\s_-]*pan", r"director[\s_-]*pan"],
    "pan_entity": [r"entity[\s_-]*pan", r"company[\s_-]*pan", r"\bpan\s*card\b"],
    "msme_certificate": [r"\bmsme\b", r"\budyam\b"],
    "kyc_form": [r"\bkyc\b"],
    "electricity_bill": [r"electricity", r"electric[\s_-]*bill", r"e[\s_-]*bill"],
    # Ownership / occupancy proof. Classified ahead of rental_agreement below so a
    # "Proof of Premises - Sale Deed.pdf" isn't mistaken for a tenancy document.
    "sale_deed": [r"sale[\s_-]*deed", r"property[\s_-]*tax", r"index[\s_-]*ii", r"7\s*/\s*12\s*extract"],
    "landlord_declaration": [r"landlord", r"\bnoc\b", r"no[\s_-]*objection"],
    "rental_agreement": [r"rental", r"\blease\b", r"rent[\s_-]*agreement", r"proof[_\s]*of[_\s]*premises"],
    "aadhaar": [r"aadha?ar"],
    "client_photo": [r"customer[\s_-]*photo", r"customer\s*image", r"owner\s*image", r"\bphoto\b"],
    "trade_licence": [r"trade[\s_-]*licen[cs]e", r"shop[\s_-]*act", r"shop[\s_-]*establishment"],
    # Factory License (Directorate of Industrial Safety and Health) is distinct from
    # trade_licence (Shops & Establishment) -- classified ahead of the generic
    # pcb_certificate pattern so "Factory License Certificate.pdf" doesn't fall
    # through to it via an accidental "certificate" match.
    "factory_license": [r"factory[\s_-]*licen[cs]e"],
    "pcb_certificate": [r"\bpcb\b", r"pollution[\s_-]*control", r"consent[\s_-]*to[\s_-]*operate"],
}

DOC_TYPE_ORDER = [
    "gst_certificate", "gst_portal", "cancelled_cheque", "pan_owner", "pan_entity",
    "msme_certificate", "kyc_form", "electricity_bill", "sale_deed", "landlord_declaration",
    "rental_agreement", "aadhaar", "client_photo", "factory_license", "pcb_certificate", "trade_licence",
]

# ---------------------------------------------------------------- content-based fallback
# Filenames are frequently useless in practice -- a document forwarded via
# WhatsApp is saved as "WhatsApp Image 2026-08-13 at 2.44.58 PM.jpeg" with zero
# descriptive signal, and a bill exported from a state electricity board's
# portal might just be "bill_687492423904.pdf". A human doesn't give up on
# those -- they open the file and look. This does the same thing using the
# document's actual extracted text, for whatever the filename pass couldn't
# place (see extract_entity() in pipeline.py, which calls this for every file
# in `unmatched` before giving up on it). Order matters -- first match wins,
# most-specific/least-ambiguous signatures first.
# Each doc type has a list of "groups" -- a group is either a single regex
# (matches if found anywhere) or a tuple of regexes that must ALL be found
# (possibly on different lines/labels) for that group to count as a hit. The
# doc type matches if ANY of its groups is satisfied. Newlines are collapsed
# to spaces before matching (see classify_content) so a phrase split across
# lines by a scanner/vision-transcript still matches without needing DOTALL
# regexes everywhere.
CONTENT_SIGNATURES: List[tuple] = [
    ("gst_certificate", ["certificate of registration", "form gst reg-06",
                          ("goods and services tax", "registration certificate")]),
    ("msme_certificate", ["udyam registration certificate", r"udyam-[a-z]{2}-\d{2}-\d{7}"]),
    ("gst_portal", [("gst", "bank account status"), ("account status", "gstin")]),
    ("cancelled_cheque", [("ifsc", r"a\s*/?\s*c\s*no"), ("ifs code", r"a\s*/?\s*c\s*no"),
                           "payable at par at all branches"]),
    ("aadhaar", ["unique identification authority", ("aadhaar", "government of india")]),
    ("pan_entity", [("income tax department", "permanent account number"),
                    ("permanent account number", r"\b[a-z]{5}\d{4}[a-z]\b")]),
    ("electricity_bill", ["electricity bill", "vidyut vitran", "विद्युत वितरण", "वीज बिल", ("sanctioned load", "billed demand"),
                           ("kwh", "meter number"), ("kwh", "sanctioned load")]),
    ("trade_licence", ["shops and commercial establishment", "registration certificate of shop",
                        "trade licence", "trade license"]),
    ("rental_agreement", ["lessor", "lessee", "this rental agreement", "this lease agreement"]),
    ("sale_deed", ["sale deed", ("vendor", "purchaser", "consideration")]),
]


def classify_content(text: str) -> Optional[str]:
    """Best-effort doc-type guess from a document's extracted text, used only as
    a fallback when the filename didn't match anything. Returns None (not a
    guess) if nothing is confident enough to call -- an unmatched file stays
    unmatched and visibly flagged rather than being silently mis-filed."""
    if not text or not text.strip():
        return None
    low = re.sub(r'\s+', ' ', text.lower())
    for doc_type, groups in CONTENT_SIGNATURES:
        for g in groups:
            if isinstance(g, tuple):
                if all(re.search(p, low) for p in g):
                    return doc_type
            elif re.search(g, low):
                return doc_type
    return None


@dataclass
class ClassifiedDocs:
    by_type: Dict[str, List[str]] = field(default_factory=dict)
    unmatched: List[str] = field(default_factory=list)

    def get(self, doc_type: str) -> List[str]:
        return self.by_type.get(doc_type, [])

    def has(self, doc_type: str) -> bool:
        return bool(self.by_type.get(doc_type))


def resolve_docs_folder(path: str) -> str:
    """Vendor folders sometimes wrap the real files in a same-named subfolder.
    Return the folder that actually contains files, preferring the outer
    folder if it already has files."""
    entries = os.listdir(path)
    files = [e for e in entries if os.path.isfile(os.path.join(path, e))]
    if files:
        return path
    dirs = [e for e in entries if os.path.isdir(os.path.join(path, e))]
    for d in dirs:
        inner = os.path.join(path, d)
        if any(os.path.isfile(os.path.join(inner, f)) for f in os.listdir(inner)):
            return inner
    return path


def classify_folder(path: str) -> ClassifiedDocs:
    docs_folder = resolve_docs_folder(path)
    result = ClassifiedDocs()
    for fname in sorted(os.listdir(docs_folder)):
        full = os.path.join(docs_folder, fname)
        if not os.path.isfile(full):
            continue
        low = fname.lower()
        matched = None
        for doc_type in DOC_TYPE_ORDER:
            if any(re.search(p, low) for p in DOC_TYPE_PATTERNS[doc_type]):
                matched = doc_type
                break
        if matched:
            result.by_type.setdefault(matched, []).append(full)
        else:
            result.unmatched.append(full)
    return result

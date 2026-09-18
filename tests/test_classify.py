"""Document classification -- filename patterns and the content fallback.

Background (2026-09-17, Sri Laxmi Steel): an electricity bill saved as
"electrity bill sri laxmi steel.png" was left unmatched twice over -- the
misspelled filename matched no pattern, and the content fallback didn't
know a TSSPDCL bill's layout (Consumer Name / Unique Service Number /
Service Number / ERO / Current Month Bill), which never says "electricity".
The report then reported the bill missing and left four address fields
unresolved.
"""
from __future__ import annotations

import os

import pytest

from vdd.extract.classify import DOC_TYPE_ORDER, DOC_TYPE_PATTERNS, classify_content, classify_folder


def _classify_name(fname: str):
    import re
    low = fname.lower()
    for doc_type in DOC_TYPE_ORDER:
        if any(re.search(p, low) for p in DOC_TYPE_PATTERNS[doc_type]):
            return doc_type
    return None


@pytest.mark.parametrize("fname", [
    "electrity bill sri laxmi steel.png",      # the real misspelling
    "Electricty Bill.pdf",
    "Latest Electricity Bill.pdf",
    "power bill march.jpg",
    "current bill.png",
    "EB bill.jpeg",
    "TSSPDCL_bill_2026.pdf",
    "bescom.pdf",
    "Tata Power - Bill.pdf",
])
def test_electricity_bill_filename_variants(fname):
    assert _classify_name(fname) == "electricity_bill"


@pytest.mark.parametrize("fname, expected", [
    ("Electronic KYC form.pdf", "kyc_form"),        # 'elec...' must not steal a KYC form
    ("GST_CT.pdf", None),                            # no filename signal -> content fallback's job
    ("GST portal bank details.png", "gst_portal"),
    ("random_scan_0042.pdf", None),
])
def test_other_names_are_not_swept_up(fname, expected):
    assert _classify_name(fname) == expected


# Synthetic text in the shape of a Telangana/AP DISCOM bill -- the labels are
# the real layout, the values are made up.
_DISCOM_BILL = """
Consumer Name
M/S EXAMPLE FABRICATORS
Unique Service Number
100000001
Service Number
0100 00001
ERO
JEEDIMETLA
Address
PLOT 1 INDUSTRIAL ESTATE
Current Month Bill
Date
08-SEP-26
Amount
850
Total Amount Payable
Due Date
22-SEP-26
"""

_GST_CERT = """
Goods and Services Tax
Government of India
Form GST REG-06
Registration Certificate
Registration Number : 36AAAAA0000A1Z5
"""

_CHEQUE = "IFSC HDFC0000001  A/C No 50200000000000  PAYABLE AT PAR AT ALL BRANCHES"


def test_discom_layout_is_recognised_as_electricity_bill():
    assert classify_content(_DISCOM_BILL) == "electricity_bill"


def test_single_strong_markers():
    assert classify_content("Units consumed 412 kWh this cycle") == "electricity_bill"
    assert classify_content("Connected Load 47.0 KW\nBill period Aug 2026") == "electricity_bill"


def test_discom_markers_do_not_outrank_more_specific_documents():
    # More specific signatures come first in CONTENT_SIGNATURES, so a GST
    # certificate or cheque that happened to mention a consumer number would
    # still be classified as what it is.
    assert classify_content(_GST_CERT + "\nconsumer name x service number 1") == "gst_certificate"
    assert classify_content(_CHEQUE + "\nunique service number 1") == "cancelled_cheque"
    assert classify_content("Just a letter with nothing distinctive in it.") is None


def test_classify_folder_places_misspelled_bill(tmp_path):
    for name in ("electrity bill sri laxmi steel.png", "GST_CT.pdf", "ENTITY_PAN.pdf"):
        (tmp_path / name).write_bytes(b"x")
    docs = classify_folder(str(tmp_path))
    assert [os.path.basename(f) for f in docs.get("electricity_bill")] == ["electrity bill sri laxmi steel.png"]
    assert [os.path.basename(f) for f in docs.get("pan_entity")] == ["ENTITY_PAN.pdf"]
    assert [os.path.basename(f) for f in docs.unmatched] == ["GST_CT.pdf"]  # content fallback's job

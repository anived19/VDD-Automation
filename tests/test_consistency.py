"""Document read quality and cross-document consistency
(vdd/extract/consistency.py). Each case is a failure shape seen on a real
vendor folder this week, with synthetic values.
"""
from __future__ import annotations

from vdd.extract.consistency import DocumentRead, _names_agree, check_consistency, read_quality_rows

GST = {"gstin": "19AADFB0389G1ZH", "pan": "AADFB0389G", "legal_name": "EXAMPLE TRADING CO",
       "trade_name": "M/S. EXAMPLE TRADING COMPANY", "constitution": "Partnership",
       "address": "135/11/A/2 Girish Ghosh Road", "date_of_registration": "01/07/2017"}


def _read(name, dtype, parsed, method="pymupdf_text", confident=True):
    return DocumentRead(path=f"v/{name}", doc_type=dtype, method=method, confident=confident, parsed=parsed)


def _warnings(reads, partners=None):
    return check_consistency(reads, partners=partners).warnings


# ---------------------------------------------------------------- read quality

def test_fully_read_documents_produce_no_quality_warnings():
    reads = [_read("gst.pdf", "gst_certificate", GST),
             _read("pan.pdf", "pan_entity", {"pan": "AADFB0389G", "name": "EXAMPLE TRADING CO"}, method="easyocr")]
    assert _warnings(reads) == []
    assert [r["quality"] for r in read_quality_rows(reads)] == ["read", "read"]


def test_blurry_pan_card_is_reported_as_a_poor_read_naming_the_missing_field():
    # B R Trading Co's scanned firm card before the OCR merge fix: name read, PAN not
    reads = [_read("gst.pdf", "gst_certificate", GST),
             _read("BRT_pan.pdf", "pan_entity", {"name": "EXAMPLE TRAIING ?O"}, method="easyocr")]
    w = _warnings(reads)
    assert len(w) == 1 and w[0].startswith("BRT_pan.pdf (pan_entity): PARTIAL READ via easyocr -- missing pan")
    assert read_quality_rows(reads)[0]["quality"] == "partial"


def test_unreadable_file_is_reported_as_such():
    reads = [_read("gst.pdf", "gst_certificate", GST),
             _read("cheque.jpg", "cancelled_cheque", {}, method="unavailable", confident=False)]
    w = _warnings(reads)
    assert any("cheque.jpg (cancelled_cheque): could not be read" in x for x in w)
    assert read_quality_rows(reads)[0]["quality"] == "unreadable"


def test_no_gst_certificate_means_no_cross_checks_and_says_so():
    reads = [_read("pan.pdf", "pan_entity", {"pan": "AADFB0389G", "name": "X"})]
    assert any("No readable GST certificate" in x for x in _warnings(reads))


# ---------------------------------------------------------------- identifiers

def test_wrong_vendors_document_is_caught_by_gstin_and_pan():
    reads = [_read("gst.pdf", "gst_certificate", GST),
             _read("kyc.pdf", "kyc_form", {"gstin": "27ZZZZZ9999Z1Z1", "pan": "ZZZZZ9999Z"}),
             _read("udyam.pdf", "msme_certificate", {"udyam_number": "UDYAM-WB-08-0000001", "enterprise_name": "OTHER FIRM",
                                                      "nic_5_code": "46620", "address": "x", "pan": "ZZZZZ9999Z"})]
    w = _warnings(reads)
    assert any("kyc.pdf (kyc_form): carries GSTIN 27ZZZZZ9999Z1Z1, but the GST certificate is 19AADFB0389G1ZH" in x for x in w)
    assert any("kyc.pdf (kyc_form): carries PAN ZZZZZ9999Z" in x for x in w)
    assert any("udyam.pdf (msme_certificate): carries PAN ZZZZZ9999Z" in x for x in w)
    assert any("udyam.pdf (msme_certificate): name 'OTHER FIRM' does not match" in x for x in w)


def test_owners_card_filed_as_the_entitys_is_named_precisely():
    reads = [_read("gst.pdf", "gst_certificate", GST),
             _read("B.P.RAY_PAN.pdf", "pan_entity", {"pan": "AGAPR1324G", "name": "EXAMPLE PARTNER NAME"})]
    w = _warnings(reads)
    assert any("PAN AGAPR1324G is an individual's (4th letter P), not the entity's AADFB0389G" in x for x in w)


def test_partners_own_pan_card_is_fine_when_the_partner_is_on_record():
    reads = [_read("gst.pdf", "gst_certificate", GST),
             _read("partner.pdf", "pan_owner", {"pan": "AGAPR1324G", "name": "EXAMPLE PARTNER NAME"})]
    assert _warnings(reads, partners=["EXAMPLE PARTNER NAME", "ANOTHER PARTNER"]) == []
    w = _warnings(reads, partners=["ANOTHER PARTNER", "THIRD PARTNER"])
    assert any("is not one of the partners/directors on record" in x for x in w)


def test_proprietorship_documents_in_the_proprietors_name_are_explained_not_flagged_as_wrong():
    gst = dict(GST, legal_name="VEER SHETTY SHIVALLA", trade_name="SRI LAXMI STEEL", constitution="Proprietorship")
    reads = [_read("gst.pdf", "gst_certificate", gst),
             _read("udyam.pdf", "msme_certificate", {"udyam_number": "U", "enterprise_name": "SRI LAXMI STEEL",
                                                      "nic_5_code": "25119", "address": "x"}),
             _read("cheque.pdf", "cancelled_cheque", {"ifsc": "HDFC0000001", "account_number": "50200000000000",
                                                       "account_holder": "VEER SHETTY SHIVALLA"})]
    assert _warnings(reads) == []


def test_bill_consumer_name_is_left_to_the_ownership_resolver():
    reads = [_read("gst.pdf", "gst_certificate", GST),
             _read("bill.png", "electricity_bill", {"consumer_name": "M/S DEVI ENGINEERING WORKS", "address": "plot 382"})]
    assert _warnings(reads) == []   # resolve_addr_ownership_type handles the landlord's-meter case


def test_name_agreement_ignores_prefixes_and_corporate_suffixes():
    assert _names_agree("M/S. B.R. TRADING COMPANY", "B R TRADING CO")
    assert _names_agree("SKANDAN PLASTRIX PRIVATE LIMITED", "Skandan Plastrix Pvt. Ltd.")
    assert _names_agree("EXAMPLE TRAIING ?O", "EXAMPLE TRADING CO")
    assert not _names_agree("DEVI ENGINEERING WORKS", "SRI LAXMI STEEL")
    assert _names_agree("", "X") is None

"""Regressions from two real vendor files (2026-09-17): Sri Laxmi Steel and
B R Trading Co. Each test pins a field that was silently unresolved or
wrongly resolved, using synthetic text in the same shape as the real
document. Values are made up; layouts are real.
"""
from __future__ import annotations

import os

from vdd.extract.classify import ClassifiedDocs, classify_content, classify_folder
from vdd.extract.ocr import _merge_ocr_passes, _reading_order
from vdd.extract.parsers import parse_electricity_bill, parse_pan_card
from vdd.pipeline import _garbled_pan_match, _route_pan_cards
from vdd.resolve.resolvers import (
    _addr_token_sets, _name_match, _place_overlap, resolve_addr_electricity_bill, resolve_ident_pan_name_match,
)

# ---------------------------------------------------------------- PAN cards

# The current bilingual physical card as the merged OCR passes read it (Sri
# Laxmi Steel shape): the tiny "नाम / Name" caption comes out as "I Name".
_BILINGUAL_PAN_CARD = """
आयकर विभाग
भारत सरकार
INCOME TAX DEPARTMENT
GOVT OF INDIA
स्थायी लेखा संख्या कार्ड
Permanent Account Number Card
ABCDE1234F
I Name
EXAMPLE HOLDER NAME
पिता का नाम
Father'$ Name
EXAMPLE FATHER NAME
10/02/1968
Dale 0f Birth
"""

# A low-contrast scanned firm card after the merged OCR pass (B R Trading Co
# shape): garbled header, garbled PAN, name intact.
_SCANNED_FIRM_CARD = """
INCOMIZ TAX DEPARTMENI
GOYT. OF INDIA
EXAMPLE TRAIING ?O
15/08/1987
PErmanent Account Numiber
AADFE03u9Ci
"""

# A partner's card where OCR got the PAN but not the header phrases.
_PARTNER_CARD = """
????
GOYT. Of INDIA
EXAMPLE PARTNER NAME
EXAMPLE PARENT NAME
04/01/1952
AGAPR1324G
"""


def test_bilingual_card_name_is_the_latin_holder_line_not_the_hindi_title():
    out = parse_pan_card(_BILINGUAL_PAN_CARD)
    assert out == {"pan": "ABCDE1234F", "name": "EXAMPLE HOLDER NAME"}


def test_scanned_firm_card_yields_name_but_never_a_guessed_pan():
    out = parse_pan_card(_SCANNED_FIRM_CARD)
    assert out.get("name") == "EXAMPLE TRAIING ?O"
    assert "pan" not in out  # AADFE03u9Ci is not a PAN; we do not invent one


def test_older_physical_layout_still_takes_the_line_under_the_header():
    out = parse_pan_card("INCOME TAX DEPARTMENT\nGOVT. OF INDIA\nOLD STYLE TRADERS\n01/01/2001\nABCDE1234F")
    assert out["name"] == "OLD STYLE TRADERS"


def test_ocr_garbled_headers_still_classify_as_pan_card():
    assert classify_content(_SCANNED_FIRM_CARD) == "pan_entity"
    assert classify_content(_PARTNER_CARD) == "pan_entity"
    assert classify_content(_BILINGUAL_PAN_CARD) == "pan_entity"


def test_pan_filename_variants_and_underscored_udyam(tmp_path):
    for name in ("BRT_pan.pdf", "B.P.RAY_PAN.pdf", "partner_pan.jpg",
                 "B._R._Trading_Co._Udyam_Registration_Certificate.pdf", "company_pan_card.pdf", "PANDA.pdf"):
        (tmp_path / name).write_bytes(b"x")
    docs = classify_folder(str(tmp_path))
    names = lambda t: sorted(os.path.basename(f) for f in docs.get(t))
    assert names("pan_entity") == ["B.P.RAY_PAN.pdf", "BRT_pan.pdf", "company_pan_card.pdf"]
    assert names("pan_owner") == ["partner_pan.jpg"]
    assert names("msme_certificate") == ["B._R._Trading_Co._Udyam_Registration_Certificate.pdf"]
    assert [os.path.basename(f) for f in docs.unmatched] == ["PANDA.pdf"]  # 'pan' inside a word is not a PAN


def test_garbled_pan_recognised_only_when_it_is_plainly_the_same_number():
    assert _garbled_pan_match(_SCANNED_FIRM_CARD, "AADFB0389G")
    assert not _garbled_pan_match(_SCANNED_FIRM_CARD, "AGAPR1324G")
    assert not _garbled_pan_match("no identifiers here at all", "AADFB0389G")


def test_route_pan_cards_splits_entity_and_partner_cards():
    texts = {"gst.pdf": "Registration Number : 19AADFB0389G1ZH\nLegal Name\nEXAMPLE TRADING CO",
             "firm.pdf": _SCANNED_FIRM_CARD, "partner.pdf": _PARTNER_CARD}
    docs = ClassifiedDocs(by_type={"gst_certificate": ["gst.pdf"], "pan_entity": ["firm.pdf", "partner.pdf"]})
    warnings: list = []
    _route_pan_cards(docs, lambda f: texts[f], warnings)
    assert docs.get("pan_entity") == ["firm.pdf"]      # garbled read of the GSTIN's PAN -> the entity's card
    assert docs.get("pan_owner") == ["partner.pdf"]    # clean, different PAN -> a partner's card
    assert any("AGAPR1324G" in w and "owner/partner" in w for w in warnings)


def test_cin_on_a_utility_bill_or_for_a_partnership_is_not_the_vendors(tmp_path, monkeypatch):
    """The CESC bill printed CESC Limited's CIN; the old scan attributed it to the
    partnership and queried Probe42 with it."""
    from vdd import pipeline
    texts = {"gst.pdf": "Registration Number : 19AADFB0389G1ZH\nLegal Name\nEXAMPLE TRADING CO\n"
                        "Constitution of Business\nPartnership\n",
             "bill.pdf": "YOUR ELECTRICITY BILL FOR\nCIN: L31901WB1978PLC031411.\nM/S EXAMPLE TRADING COMPANY\n"
                         "135/11/A/2 GIRISH GHOSH\nHOWRAH 711201\n"}
    from vdd.extract.ocr import ExtractionResult
    monkeypatch.setattr(pipeline, "extract_text",
                        lambda f, cache_dir=None: ExtractionResult(text=texts[f], method="test", confident=True))
    monkeypatch.setattr(pipeline, "detect_diagonal_strike", lambda *a, **k: None)
    docs = ClassifiedDocs(by_type={"gst_certificate": ["gst.pdf"], "electricity_bill": ["bill.pdf"]})
    warnings: list = []
    entity = pipeline.extract_entity(docs, warnings)
    assert "cin" not in entity
    # and even on a non-utility document, a partnership never gets a CIN
    texts["gst.pdf"] += "Letterhead of our customer, CIN U12345MH2001PTC123456\n"
    entity = pipeline.extract_entity(docs, warnings)
    assert "cin" not in entity and any("has no CIN" in w for w in warnings)


def test_route_pan_cards_does_nothing_without_a_gst_certificate():
    docs = ClassifiedDocs(by_type={"pan_entity": ["a.pdf", "b.pdf"]})
    _route_pan_cards(docs, lambda f: _PARTNER_CARD, [])
    assert docs.get("pan_entity") == ["a.pdf", "b.pdf"]


def test_ocr_garbled_name_still_matches_the_gst_legal_name():
    assert _name_match("EXAMPLE TRAIING ?O", "EXAMPLE TRADING CO") is True
    assert _name_match("M/S DEVI ENGINEERING WORKS", "SRI LAXMI STEEL") is False
    assert _name_match("EXAMPLE TRADING CO", "EXAMPLE ENGINEERING WORKS") is False
    assert resolve_ident_pan_name_match("EXAMPLE TRAIING ?O", "EXAMPLE TRADING CO").value == "match"


# ---------------------------------------------------------------- OCR pass merging

def _box(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def test_merge_keeps_both_passes_and_prefers_the_confident_reading_on_overlap():
    english = [(_box(0, 0, 100, 10), "GOVT OF INDIA", 0.9), (_box(0, 40, 100, 50), "AADFE03u9Ci", 0.5)]
    regional = [(_box(0, 0, 100, 10), "GOVT OF INDlA", 0.6), (_box(0, 20, 100, 30), "आयकर विभाग", 0.8)]
    merged = _merge_ocr_passes(english, regional)
    texts = [t for (_b, t, _c) in _reading_order(merged)]
    assert texts == ["GOVT OF INDIA", "आयकर विभाग", "AADFE03u9Ci"]


def test_reading_order_is_top_to_bottom_then_left_to_right():
    toks = [(_box(50, 0, 90, 10), "right", 1), (_box(0, 30, 40, 40), "below", 1), (_box(0, 0, 40, 10), "left", 1)]
    assert [t for (_b, t, _c) in _reading_order(toks)] == ["left", "right", "below"]


# ---------------------------------------------------------------- electricity bills

_DISCOM_BILL = """
Consumer Name
M/S EXAMPLE FABRICATORS
Unique Service Number
100000001
Address
PLNO ३८२ SVCIE PHASE २ I .D AJJEEDIMETLA,
Section Name
JEEDIMETLAIIDAJ
Your Arrears as on
Date
31-AUG-26
Amount
0
Current Month Bill
Amount
850
"""

_CESC_BILL = """
Consumer No.
YOUR ELECTRICITY BILL FOR
JULY 2026
62045019009 / 07260
M/S EXAMPLE TRADING COMPANY
135/11/A/2 GIRISH GHOSH
RD BELURMATH
LP-30/14/3
HOWRAH 711201
Registered Mobile No : 70xxx6xx90
"""


def test_discom_bill_address_stops_at_the_next_label():
    out = parse_electricity_bill(_DISCOM_BILL)
    assert out["consumer_name"] == "M/S EXAMPLE FABRICATORS"
    assert out["address"].startswith("PLNO") and "Arrears" not in out["address"] and "850" not in out["address"]


def test_cesc_unlabelled_consumer_block_is_parsed():
    out = parse_electricity_bill(_CESC_BILL)
    assert out["consumer_name"] == "M/S EXAMPLE TRADING COMPANY"
    assert out["address"] == "135/11/A/2 GIRISH GHOSH, RD BELURMATH, LP-30/14/3, HOWRAH 711201"


def test_label_words_are_not_localities_and_devanagari_digits_are_digits():
    prem, place, _ = _addr_token_sets("Name Of Premises/Building: s v co op industrial estate, plot no ३८२")
    assert "name" not in place and "382" in prem


def test_ocr_run_together_place_name_still_matches():
    assert "jeedimetla" in _place_overlap({"jeedimetla", "hyderabad"}, {"ajjeedimetla", "svcie"})
    assert not _place_overlap({"road"}, {"broadway"})


def test_adjacent_plot_in_same_estate_is_minor_discrepancy_for_the_right_reason():
    gst = ("Floor No.: survey no 305 306 308 Building No./Flat No.: plot no 384 385 "
           "Name Of Premises/Building: s v co op industrial estate Locality/Sub Locality: ida jeedimetla "
           "City/Town/Village: Hyderabad State: Telangana PIN Code: 500055")
    r = resolve_addr_electricity_bill(gst, "PLNO ३८२ SVCIE PHASE २ I .D AJJEEDIMETLA,", None)
    assert r.value == "minor_discrepancy" and "jeedimetla" in r.note and "(name)" not in r.note


def test_same_premises_with_utility_pin_off_by_one_is_a_match():
    gst = ("Building No./Flat No.: 135/11/A/2 Road/Street: GIRISH GHOSH ROAD City/Town/Village: BELURMATH "
           "District: Howrah State: West Bengal PIN Code: 711202")
    bill = parse_electricity_bill(_CESC_BILL)
    r = resolve_addr_electricity_bill(gst, bill["address"], bill.get("pincode"),
                                      bill_consumer_name=bill["consumer_name"], entity_name="EXAMPLE TRADING CO")
    assert r.value == "match" and "utility-record data error" in r.note


# ---------------------------------------------------------------- three addresses, one vendor (2026-09-18)
# GST, Udyam and the electricity bill each name a different plot in the same
# industrial estate. The pipeline compared only bill-vs-GST, matched them on
# the token '2' (Road Number 2 vs Phase 2) and never looked at Udyam at all.
from vdd.extract.parsers import parse_msme_certificate
from vdd.resolve.resolvers import _plot_numbers, _plots_conflict, resolve_addr_msme, udyam_vs_gst_address_note, ApiBundle

_GST_ADDR = ("Floor No.: survey no 305 306 308 Building No./Flat No.: plot no 384 385 Name Of Premises/Building: "
             "s v co op industrial estate Road/Street: Road Number 2 Locality/Sub Locality: ida jeedimetla "
             "City/Town/Village: Hyderabad District: Medchal Malkajgiri State: Telangana PIN Code: 500055")
_BILL_ADDR = "PLNO 382 SVCIE PHASE 2,!.D.AJEEDIMETLA,"
_UDYAM_TEXT = """SRI LAXMI STEEL
OFFICAL ADDRESS OF
ENTERPRISE
Flat/Door/Block No.
sy no 301
Name of Premises/ Building
plot 558 562 563
Village/Town
ram reddy nagar
Block
-
Road/Street/Lane
ida jeedimetla
City
HYDERABAD
State
TELANGANA
District
HYDERABAD , Pin 500055
Mobile
9848016314
Email:
x@example.com
DATE OF INCORPORATION /
"""


def test_plot_numbers_follow_plot_keywords_only():
    assert _plot_numbers(_GST_ADDR) == {"305", "306", "308", "384", "385"}
    assert _plot_numbers(_BILL_ADDR) == {"382"}
    assert _plot_numbers("Road Number 2, Phase 2, Floor No. 3") == set()
    assert _plot_numbers("Building No./Flat No.: 135/11/A/2 Road/Street: GIRISH GHOSH ROAD") == set()  # alphanumeric, not a bare plot


def test_bare_small_numbers_are_not_premises_identifiers():
    prem, _, _ = _addr_token_sets("Road Number 2 Phase 2 plot no 384")
    assert "2" not in prem and "384" in prem


def test_different_plot_in_same_estate_is_a_discrepancy_not_a_match():
    conflict, gst_plots, bill_plots = _plots_conflict(_GST_ADDR, _BILL_ADDR)
    assert conflict and bill_plots == {"382"}
    r = resolve_addr_electricity_bill(_GST_ADDR, _BILL_ADDR, None)
    assert r.value == "minor_discrepancy"
    assert "DISCREPANCY" in r.note and "382" in r.note and "384/385" in r.note and "jeedimetla" in r.note
    # even with the PINs agreeing, a different plot is never a full match
    r = resolve_addr_electricity_bill(_GST_ADDR, _BILL_ADDR + " 500055", "500055")
    assert r.value == "minor_discrepancy" and "PIN 500055 matches" in r.note


def test_same_plot_still_matches():
    gst = ("Building No./Flat No.: 135/11/A/2 Road/Street: GIRISH GHOSH ROAD City/Town/Village: BELURMATH "
           "District: Howrah State: West Bengal PIN Code: 711202")
    r = resolve_addr_electricity_bill(gst, "135/11/A/2 GIRISH GHOSH, RD BELURMATH, HOWRAH 711202", "711202")
    assert r.value == "match"
    r = resolve_addr_electricity_bill("plot no 384 385 ida jeedimetla Hyderabad PIN Code: 500055",
                                      "plot 385 svcie ida jeedimetla", None)
    assert r.value == "match"


def test_udyam_address_is_parsed_from_the_certificate():
    out = parse_msme_certificate(_UDYAM_TEXT)
    assert out["address"].startswith("Flat/Door/Block No.: sy no 301 Name of Premises/ Building: plot 558 562 563")
    assert "ida jeedimetla" in out["address"] and "Block: -" not in out["address"]
    assert out["pincode"] == "500055"


def test_udyam_vs_gst_conflict_becomes_a_cross_check_warning():
    from vdd.pipeline import find_cross_check_items
    udyam = parse_msme_certificate(_UDYAM_TEXT)["address"]
    note = udyam_vs_gst_address_note(_GST_ADDR, udyam)
    assert note.startswith("WARNING:") and "558/562/563" in note and "384/385" in note and "same locality" in note
    r = resolve_addr_msme(ApiBundle(), "UDYAM-TS-20-0001997", gst_address=_GST_ADDR, udyam_address=udyam)
    assert r.value == "valid_active" and "WARNING" in r.note
    assert any(item.startswith("addr_msme:") for item in find_cross_check_items({"addr_msme": r}))
    # agreement is stated, not flagged
    assert udyam_vs_gst_address_note(_GST_ADDR, "plot no 384 ida jeedimetla Hyderabad Pin 500055") == \
        "Udyam and GST registrations name the same premises."
    assert udyam_vs_gst_address_note(_GST_ADDR, None) is None

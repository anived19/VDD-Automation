"""
Robustness item 5 (18-Sep): premises numbers come in kinds, and a PIN alone
is not an address.

* 'Sy. No. 12/3' on the GST certificate vs 'Door No. 12' on the bill is the
  same premises on two registers -- neither a match nor a discrepancy; the
  report must say "cannot be compared", not "could not be matched";
* 'Plot 382' vs 'Plot 384-385' is still a conflict (same kind, nothing shared);
* a compound number ('384-385', '12/3') no longer collapses into '384385'
  (which then read as a PIN code) or '123';
* two addresses that share only a PIN score minor_discrepancy with the
  reason stated, not 'match'.
"""
from vdd.resolve.resolvers import (
    _addr_token_sets, _compare_plots, _norm_addr, _plot_numbers, _premises_ids,
    resolve_addr_electricity_bill, udyam_vs_gst_address_note,
)


def test_compound_numbers_keep_their_parts():
    assert _norm_addr("Plot No. 384-385") == "plot no 384_385"
    assert _norm_addr("Sy. No. 12/3") == "sy no 12_3"
    assert _norm_addr("M.I.D.C. G-100") == "midc g100"          # letters still collapse
    prem, _, pins = _addr_token_sets("Plot No. 384-385, PIN 500055")
    assert pins == {"500055"} and "384385" not in prem
    assert {"384_385", "384", "385"} <= prem
    assert _plot_numbers("Plot No. 384-385, Survey No. 305/306/308") == {"384", "385", "305", "306", "308"}


def test_premises_ids_are_grouped_by_kind():
    assert _premises_ids("Sy. No. 12/3, Door No. 7, Plot 384") == \
        {"survey": {"12", "3"}, "door": {"7"}, "plot": {"384"}}
    assert _premises_ids("Road Number 2, Phase 2") == {}


def test_same_kind_different_number_is_a_conflict():
    c = _compare_plots("plot no 384 385 ida jeedimetla", "PLNO 382 SVCIE PHASE 2 jeedimetla")
    assert c.conflict and c.comparable


def test_different_kinds_are_incomparable_not_a_conflict():
    c = _compare_plots("Sy. No. 12/3 Kittampalayam", "Door No. 12 Kittampalayam")
    assert not c.conflict and not c.comparable
    assert c.kinds_a == ("survey",) and c.kinds_b == ("door",)
    # one side naming nothing at all is simply "nothing to compare"
    c = _compare_plots("Sy. No. 12/3 Kittampalayam", "Kittampalayam main road")
    assert not c.conflict and c.comparable


def test_bill_with_a_different_kind_of_number_says_cannot_compare():
    r = resolve_addr_electricity_bill("Sy. No. 12/3 Kittampalayam Coimbatore PIN 641402",
                                      "Door No 12 Kittampalayam Coimbatore", None)
    assert r.value == "minor_discrepancy"
    assert "different kinds" in r.note and "survey no. on the GST certificate" in r.note \
        and "door/flat no. on the bill" in r.note
    assert "DISCREPANCY" not in r.note and "could not be matched" not in r.note


def test_pin_alone_is_not_a_match():
    r = resolve_addr_electricity_bill("Plot 12 Kittampalayam Coimbatore PIN 641402", "xxxx 641402", "641402")
    assert r.value == "minor_discrepancy"
    assert "PIN 641402 matches but nothing else does" in r.note and "may not have been read" in r.note
    # PIN + locality, or PIN + premises, is still a match
    r = resolve_addr_electricity_bill("Plot 12 Kittampalayam Coimbatore PIN 641402",
                                      "Kittampalayam 641402", "641402")
    assert r.value == "match"


def test_udyam_note_states_incomparable_kinds():
    note = udyam_vs_gst_address_note("Sy. No. 12/3 Kittampalayam Coimbatore PIN 641402",
                                     "Door No 12 Kittampalayam Coimbatore 641402")
    assert note.startswith("Udyam and GST registrations: the premises identifiers are of different kinds")
    assert "locality agrees" in note and not note.startswith("WARNING")


def test_compound_holding_number_still_matches_whole():
    gst = ("Building No./Flat No.: 135/11/A/2 Road/Street: GIRISH GHOSH ROAD City/Town/Village: BELURMATH "
           "District: Howrah State: West Bengal PIN Code: 711202")
    r = resolve_addr_electricity_bill(gst, "135/11/A/2 GIRISH GHOSH, RD BELURMATH, HOWRAH 711202", "711202")
    assert r.value == "match" and "premises identifier matches (135/11a2)" in r.note

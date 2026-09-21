"""Branch coverage for the resolver paths the Dinesh Polymers fixture never
exercises -- rented premises, a GST-portal-validated bank account, multi-state
active registrations, and an unclassifiable non-Active registration.

Run: C:\\Python313\\python.exe -m pytest tests -q
"""
from vdd.resolve.resolvers import (
    ApiBundle, Resolved,
    resolve_addr_electricity_bill, resolve_addr_landlord_declaration, resolve_addr_ownership_type,
    resolve_com_bank_verification, resolve_com_multiple_registrations, resolve_com_hsn_match,
)


# ---------------------------------------------------------------- landlord declaration (conditional)
def test_landlord_na_when_owned():
    owned = Resolved.ok("owned", "test")
    r = resolve_addr_landlord_declaration(owned, has_landlord_doc=False)
    assert r.value == "not_applicable" and not r.unresolved
    assert "no landlord" in r.note.lower()


def test_landlord_is_a_gap_when_rented():
    rented = Resolved.ok("rented", "test")
    r = resolve_addr_landlord_declaration(rented, has_landlord_doc=False)
    assert r.value == "absent" and r.note.startswith("GAP:")


def test_landlord_present_when_doc_on_file():
    r = resolve_addr_landlord_declaration(Resolved.ok("rented", "test"), has_landlord_doc=True)
    assert r.value == "present"


def test_landlord_absent_when_ownership_unknown():
    """Proof of Address never scores 'unresolved' (analysts' rule, 21-Sep)."""
    r = resolve_addr_landlord_declaration(Resolved.missing("no docs"), has_landlord_doc=False)
    assert r.value == "absent" and not r.unresolved and "not established" in r.note


def test_ownership_owned_via_sale_deed_is_confirmed_not_inferred():
    r = resolve_addr_ownership_type(False, True, has_sale_deed=True)
    assert r.value == "owned" and "NO SALE DEED" not in r.note


def test_ownership_inferred_when_bill_is_in_the_entitys_own_name():
    entity = {"legal_name": "VEER SHETTY SHIVALLA", "trade_name": "SRI LAXMI STEEL",
              "electricity_bill_consumer_name": "M/S SRI LAXMI STEEL"}
    r = resolve_addr_ownership_type(False, True, entity=entity)
    assert r.value == "owned" and "NO SALE DEED" in r.note
    assert "in the entity's own name (M/S SRI LAXMI STEEL)" in r.note


def test_ownership_not_inferred_when_bill_is_in_a_third_partys_name():
    # 2026-09-17, Sri Laxmi Steel: the bill's consumer was another firm at the
    # same industrial estate -- the landlord's meter -- and the old code cited it
    # as proof the connection was "in the entity's own name".
    entity = {"legal_name": "VEER SHETTY SHIVALLA", "trade_name": "SRI LAXMI STEEL",
              "electricity_bill_consumer_name": "M/S DEVI ENGINEERING WORKS"}
    r = resolve_addr_ownership_type(False, True, entity=entity)
    assert r.value == "address_mismatch" and not r.unresolved   # the 0 option, never 'unresolved'
    assert "GAP:" in r.note and "DEVI ENGINEERING WORKS" in r.note and "rental agreement" in r.note


def test_ownership_inferred_without_a_consumer_name_carries_no_name_claim():
    r = resolve_addr_ownership_type(False, True, entity={"legal_name": "X"})
    assert r.value == "owned" and "own name" not in r.note


# ---------------------------------------------------------------- multiple registrations
def _by_pan(rows):
    return {"results": rows}


def test_single_registration_is_no_additional():
    api = ApiBundle(ongrid_by_pan=_by_pan([{"document_id": "27AAAAA0000A1Z5", "status": "Active"}]))
    assert resolve_com_multiple_registrations(api, "27AAAAA0000A1Z5").value == "no_additional"


def test_all_active_multi_state_scores_same_as_none():
    api = ApiBundle(ongrid_by_pan=_by_pan([
        {"document_id": "27AAAAA0000A1Z5", "status": "Active", "state": "Maharashtra"},
        {"document_id": "29AAAAA0000A1Z1", "status": "Active", "state": "Karnataka"}]))
    r = resolve_com_multiple_registrations(api, "27AAAAA0000A1Z5")
    assert r.value == "active_multi_state"


def test_inactive_alone_is_not_treated_as_cancelled():
    """Ongrid collapses every non-Active state to "Inactive". That is not evidence
    of cancellation, so the parameter must not be zeroed on it."""
    api = ApiBundle(ongrid_by_pan=_by_pan([
        {"document_id": "27AAAAA0000A1Z5", "status": "Active", "state": "Maharashtra"},
        {"document_id": "19AAAAA0000A1Z4", "status": "Inactive", "state": "West Bengal"}]))
    r = resolve_com_multiple_registrations(api, "27AAAAA0000A1Z5")
    assert r.unresolved and "cancelled" in r.note.lower()


def test_probe42_cancelled_status_wins_over_ongrid_inactive():
    api = ApiBundle(
        ongrid_by_pan=_by_pan([
            {"document_id": "27AAAAA0000A1Z5", "status": "Active", "state": "Maharashtra"},
            {"document_id": "19AAAAA0000A1Z4", "status": "Inactive", "state": "West Bengal"}]),
        probe42_pnp={"gst_details": [
            {"gstin": "27AAAAA0000A1Z5", "status": "Active", "state": "Maharashtra"},
            {"gstin": "19AAAAA0000A1Z4", "status": "Cancelled", "state": "West Bengal"}]})
    r = resolve_com_multiple_registrations(api, "27AAAAA0000A1Z5")
    assert r.value == "cancelled_present" and "19AAAAA0000A1Z4" in r.note


# ---------------------------------------------------------------- bank verification
_BASE_BANK = {
    "cheque_account_number": "1351141739", "account_number_msme": "1351141739",
    "bank_name": "Central Bank of India", "ifsc": "CBIN0280710",
    "cheque_cancellation_mark_present": True,
}


def test_bank_scores_full_when_gst_portal_says_validated():
    e = dict(_BASE_BANK, gst_portal_bank_verified=True, gst_portal_account_status="Validated",
             gst_portal_account_number="1351141739")
    r = resolve_com_bank_verification(e)
    assert r.value == "penny_success_gst_match" and not r.unresolved


def test_bank_unresolved_not_negative_when_portal_says_notvalidated():
    e = dict(_BASE_BANK, gst_portal_bank_verified=False, gst_portal_account_status="NotValidated",
             gst_portal_account_type="CC", gst_portal_account_number="1351141739")
    r = resolve_com_bank_verification(e)
    assert r.unresolved and r.value is None          # never 'penny_unsuccessful' (-5)
    assert "no penny drop performed" in r.note


def test_bank_cross_source_agreement_alone_is_not_verification():
    e = dict(_BASE_BANK)                              # no GST portal screenshot at all
    r = resolve_com_bank_verification(e)
    assert r.unresolved
    assert "paperwork agreement alone is not treated as bank verification" in r.note


def test_bank_flags_account_number_conflict():
    e = dict(_BASE_BANK, account_number_msme="9999999999")
    r = resolve_com_bank_verification(e)
    assert r.unresolved and "CRITICAL" in r.note


def test_bank_flags_uncancelled_cheque():
    e = dict(_BASE_BANK, cheque_cancellation_mark_present=False)
    r = resolve_com_bank_verification(e)
    assert "WARNING" in r.note and "cancellation mark" in r.note


# ---------------------------------------------------------------- bank verification: live penny drop
def test_bank_live_penny_drop_is_gst_match_only_when_the_portal_registers_that_account():
    """"GST match" = the penny-dropped account is the one registered on the GST
    portal. A holder-name match with the legal name alone is not it (B R
    Trading Co scored 5/5 with no portal screenshot, 2026-09-19)."""
    api = ApiBundle(bank_verification={"code": "0", "message": "Success",
                                        "bank_account_data": {"name": "DINESH POLYMERS", "bank_name": "Central Bank of India"}})
    # no portal screenshot on file -> unavailable (2), not match (5)
    r = resolve_com_bank_verification(dict(_BASE_BANK, legal_name="Dinesh Polymers"), api)
    assert r.value == "penny_success_gst_unavailable" and "no GST portal" in r.note
    # portal registers the same account -> match, even if GSTN shows it NotValidated
    e = dict(_BASE_BANK, legal_name="Dinesh Polymers", gst_portal_bank_verified=False,
             gst_portal_account_status="NotValidated", gst_portal_account_number="1351141739")
    r = resolve_com_bank_verification(e, api)
    assert r.value == "penny_success_gst_match" and "api:ongrid.bank-verification.verify" in r.source
    # portal registers a different account -> mismatch
    e["gst_portal_account_number"] = "9999999999"
    r = resolve_com_bank_verification(e, api)
    assert r.value == "penny_success_gst_mismatch" and "different account" in r.note


def test_bank_live_penny_drop_name_mismatch_is_a_critical_note_not_the_gst_mismatch_bucket():
    """'GST mismatch' means the portal registers a different account. A holder
    name matching none of the vendor's names is a CRITICAL note (findings),
    and without a portal screenshot the bucket is 'GST unavailable'."""
    e = dict(_BASE_BANK, legal_name="Dinesh Polymers", trade_name="Dinesh Polymers", partners=["RAM DINESH"])
    api = ApiBundle(bank_verification={"bank_account_data": {"name": "SOME OTHER ENTITY ENTIRELY"}})
    r = resolve_com_bank_verification(e, api)
    assert r.value == "penny_success_gst_unavailable" and not r.unresolved
    assert "CRITICAL" in r.note and "none of the vendor's names" in r.note
    # a proprietorship's account in the trade name or the proprietor's name is fine
    e = dict(_BASE_BANK, legal_name="VEER SHETTY SHIVALLA", trade_name="SRI LAXMI STEEL", partners=["SHIVALLA VEER SHETTY"])
    api = ApiBundle(bank_verification={"bank_account_data": {"name": "SRI LAXMI STEEL"}})
    r = resolve_com_bank_verification(e, api)
    assert r.value == "penny_success_gst_unavailable" and "CRITICAL" not in r.note
    # suffix spelling is not a mismatch
    e = dict(_BASE_BANK, legal_name="MANGAL IRON PRIVATE LIMITED")
    api = ApiBundle(bank_verification={"bank_account_data": {"name": "MANGAL IRON PVT.LTD."}})
    assert "CRITICAL" not in resolve_com_bank_verification(e, api).note


def test_bank_live_penny_drop_gst_unavailable_when_no_legal_name():
    e = dict(_BASE_BANK)  # no legal_name on file to compare against
    api = ApiBundle(bank_verification={"bank_account_data": {"name": "DINESH POLYMERS"}})
    r = resolve_com_bank_verification(e, api)
    assert r.value == "penny_success_gst_unavailable" and not r.unresolved


def test_bank_live_penny_drop_unsuccessful_when_no_account_data():
    e = dict(_BASE_BANK, legal_name="Dinesh Polymers")
    api = ApiBundle(bank_verification={"code": "1001", "message": "Invalid Account"})
    r = resolve_com_bank_verification(e, api)
    assert r.value == "penny_unsuccessful" and not r.unresolved


def test_bank_falls_back_to_gst_portal_when_no_live_result():
    """api.bank_verification is None (e.g. no account/IFSC extracted, or the
    live call errored) -- behaves exactly like the pre-live-API resolver."""
    e = dict(_BASE_BANK, gst_portal_bank_verified=True, gst_portal_account_status="Validated",
             gst_portal_account_number="1351141739")
    r = resolve_com_bank_verification(e, ApiBundle())
    assert r.value == "penny_success_gst_match" and not r.unresolved
    assert "api:ongrid.bank-verification.verify" not in r.source


# ---------------------------------------------------------------- electricity bill / address
_GST_ADDR = "G100, MIDC, JALGAON, Jalgaon, Maharashtra, 425003"


def test_pin_mismatch_is_at_most_a_minor_discrepancy_even_when_all_else_agrees():
    """Analyst decision 2026-09-19: a differing PIN is never a full match, even
    with plot, locality and consumer name agreeing (it was 'match' before)."""
    r = resolve_addr_electricity_bill(_GST_ADDR, "PL.NO G-100, M.I.D.C. JALGAON", "422305",
                                       bill_village="JALGAON",
                                       bill_consumer_name="M/S. DINESH POLYMERS",
                                       entity_name="DINESH POLYMERS")
    assert r.value == "minor_discrepancy" and "422305" in r.note and "never a full match" in r.note


def test_pin_mismatch_stays_minor_discrepancy_without_a_name_match():
    r = resolve_addr_electricity_bill(_GST_ADDR, "PL.NO G-100, M.I.D.C. JALGAON", "422305",
                                       bill_village="JALGAON",
                                       bill_consumer_name="SOME OTHER OCCUPIER",
                                       entity_name="DINESH POLYMERS")
    assert r.value == "minor_discrepancy"


def test_genuinely_different_premises_is_not_match():
    r = resolve_addr_electricity_bill(_GST_ADDR, "FLAT 12B, SECTOR 5, NOIDA", "201301",
                                       bill_consumer_name="DINESH POLYMERS",
                                       entity_name="DINESH POLYMERS")
    assert r.value == "not_match"


def test_exact_pin_match_is_match():
    r = resolve_addr_electricity_bill(_GST_ADDR, "PL.NO G-100, M.I.D.C. JALGAON", "425003",
                                       bill_village="JALGAON")
    assert r.value == "match"


# ---------------------------------------------------------------- HSN match
def _hsn(code, desc=""):
    return ApiBundle(ongrid_detailed={"gstin_data": {"hsn_data": {"goods": [{"hsn": code, "description": desc}]}}})


def test_hsn_match_needs_an_actual_correspondence():
    e = {"nic_5_description": "Manufacture of other plastics products n.e.c",
         "electricity_bill_activity": "PLASTIC MOULDING FACTORY"}
    assert resolve_com_hsn_match(_hsn("4601"), e).value == "match"


def test_hsn_mismatch_is_reported_as_not_match():
    e = {"nic_5_description": "Manufacture of other plastics products n.e.c"}
    assert resolve_com_hsn_match(_hsn("7013"), e).value == "not_match"   # ch.70 glass vs plastics


def test_hsn_unmapped_chapter_is_unresolved_not_asserted():
    e = {"nic_5_description": "Manufacture of other plastics products n.e.c"}
    assert resolve_com_hsn_match(_hsn("0902"), e).unresolved             # ch.09 tea/coffee, unmapped


def test_hsn_none_on_record_is_not_available():
    api = ApiBundle(ongrid_detailed={"gstin_data": {"hsn_data": {}}})
    assert resolve_com_hsn_match(api, {"nic_5_description": "plastics"}).value == "not_available"


# ---------------------------------------------------------------- address_mismatch (added to the model 18-Sep)
def test_premises_proof_for_a_different_address_scores_zero_not_rented():
    """A rental agreement (or bill) for premises other than the GST-registered
    address proves nothing about those premises: 0, not the 1 point 'rented'
    carried. Only a definite not_match triggers it -- a minor discrepancy or an
    inconclusive comparison leaves the usual verdict."""
    from vdd.resolve.resolvers import (Resolved, resolve_addr_landlord_declaration, resolve_addr_ownership_type,
                                       resolve_addr_rental_validation)
    from vdd.score.engine import ScoringEngine
    not_match = Resolved.ok("not_match", "bill vs gst")
    own = resolve_addr_ownership_type(True, True, entity={}, electricity=not_match)
    assert own.value == "address_mismatch" and "different address" in own.note
    assert resolve_addr_rental_validation(own, not_match).value == "not_match"
    assert resolve_addr_landlord_declaration(own).value == "absent"
    engine = ScoringEngine("config/scoring_model.json")
    scored = {p.parameter_id: p for c in engine.score_no_consent({"addr_ownership_type": own}).categories for p in c.params}
    assert scored["addr_ownership_type"].assigned_score == 0 and not scored["addr_ownership_type"].unresolved
    # a sale deed is explicit ownership evidence and still wins
    assert resolve_addr_ownership_type(False, True, has_sale_deed=True, entity={}, electricity=not_match).value == "owned"
    # anything short of a definite mismatch is unchanged
    assert resolve_addr_ownership_type(True, True, entity={}, electricity=Resolved.ok("minor_discrepancy", "x")).value == "rented"
    assert resolve_addr_ownership_type(False, True, entity={}, electricity=Resolved.missing("could not compare")).value == "owned"


# ---------------------------------------------------------------- seller type from the strongest source (19-Sep)
def test_seller_type_prefers_gst_registry_and_nic_over_udyam_major_activity():
    from vdd.resolve.resolvers import resolve_ident_seller_type
    # B R Trading Co: GST says Retail, NIC 46620 wholesale, Udyam form says Manufacturing -> trader, with the conflict stated
    r = resolve_ident_seller_type(gst_activity="Retail Business", nic_code="46620",
                                  nic_description="Wholesale of metals and metal ores", major_activity="Manufacturing")
    assert r.value == "trader" and "CONFLICT" in r.note and "Manufacturing" in r.note
    # Sri Laxmi Steel: GST wholesale/retail, NIC 25119 manufacturing, no major activity parsed -> trader (registry first)
    r = resolve_ident_seller_type(gst_activity="Wholesale Business, Retail Business", nic_code="25119",
                                  nic_description="Manufacture of other structural metal products")
    assert r.value == "trader" and "25119" in r.note
    # a manufacturer registered as one
    r = resolve_ident_seller_type(gst_activity="Factory / Manufacturing, Wholesale Business", nic_code="22209")
    assert r.value == "manufacturer" and "CONFLICT" not in r.note
    # only the Udyam form available -> still used
    assert resolve_ident_seller_type(major_activity="Trading").value == "trader"
    assert resolve_ident_seller_type(nic_code="38300").value == "processor"
    assert resolve_ident_seller_type().unresolved


# ---------------------------------------------------------------- a watchlist hit is scored (19-Sep)
def test_zigram_watchlist_hit_outside_the_five_slots_scores_aml01_zero():
    from vdd.resolve.resolvers import resolve_aml
    from vdd.score.engine import ScoringEngine
    fake = {"legal_sanctions": [], "other": ["B R TRADING CO: matched 'GST - Non Genuine Dealers' (category: Indian Watchlists), "
                                             "fuzzy_score=100%, status=Red -- 19 matching rows, source=https://x"],
            "_comprehensive": True}
    import vdd.aml.zigram_screening as Z
    monkey = Z.summarize_screen
    Z.summarize_screen = lambda resp, label, vendor_pan=None: dict(fake)
    try:
        out = resolve_aml(ApiBundle(zigram={"x": 1}), "B R TRADING CO", [])
    finally:
        Z.summarize_screen = monkey
    r = out["legal_sanctions"]
    assert r.value == "adverse_watchlist" and "WATCHLIST HIT" in r.note and "Non Genuine" in r.note
    assert out["legal_pep"].value == "no_pep"      # the other four are unaffected
    engine = ScoringEngine("config/scoring_model.json")
    scored = {p.parameter_id: p for c in engine.score_no_consent(out).categories for p in c.params}
    assert scored["legal_sanctions"].assigned_score == 0 and not scored["legal_sanctions"].unresolved
    assert "watchlist" in scored["legal_sanctions"].matched_condition.lower()


# ---------------------------------------------------------------- Proof of Address never 'unresolved' (21-Sep)
def test_every_proof_of_address_row_resolves_to_an_option():
    """The analysts' rule: match or no match, never 'unresolved'. Missing,
    unreadable or incomparable evidence lands on the row's 0 option with the
    reason in the note."""
    from vdd.resolve.resolvers import (resolve_addr_electricity_bill, resolve_addr_landlord_declaration,
                                       resolve_addr_msme, resolve_addr_ownership_type, resolve_addr_rental_validation)
    # POA-02: no bill, no GST address, nothing comparable
    for r in (resolve_addr_electricity_bill("GST addr", None, None),
              resolve_addr_electricity_bill(None, "bill addr", None),
              resolve_addr_electricity_bill("Plot 5 MIDC Pune 411026", "Door 12 Kittampalayam", None)):
        assert r.value == "not_match" and not r.unresolved and r.note
    # POA-01: nothing on file -> the 0 option
    own = resolve_addr_ownership_type(False, False, entity={})
    assert own.value == "address_mismatch" and not own.unresolved and "GAP" in own.note
    # POA-03 / POA-05 follow
    assert resolve_addr_rental_validation(own, Resolved.missing("")).value == "not_match"
    assert resolve_addr_landlord_declaration(own).value == "absent"
    rented = Resolved.ok("rented", "doc")
    assert resolve_addr_rental_validation(rented, Resolved.ok("minor_discrepancy", "x")).value == "minor_discrepancy"
    assert resolve_addr_rental_validation(rented, Resolved.ok("not_match", "x")).value == "not_match"
    # POA-04: no API, no certificate
    assert resolve_addr_msme(ApiBundle(), None).value == "not_valid"

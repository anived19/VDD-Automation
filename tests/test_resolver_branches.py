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


def test_landlord_unresolved_when_ownership_unknown():
    r = resolve_addr_landlord_declaration(Resolved.missing("no docs"), has_landlord_doc=False)
    assert r.unresolved


def test_ownership_owned_via_sale_deed_is_confirmed_not_inferred():
    r = resolve_addr_ownership_type(False, True, has_sale_deed=True)
    assert r.value == "owned" and "NO SALE DEED" not in r.note


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
def test_bank_live_penny_drop_match_takes_priority_over_gst_portal():
    """A live penny-drop result is the primary signal -- even a GST-portal
    'NotValidated' flag (which alone would leave this unresolved, see
    test_bank_unresolved_not_negative_when_portal_says_notvalidated) must not
    override a successful, name-matching live penny drop."""
    e = dict(_BASE_BANK, legal_name="Dinesh Polymers",
             gst_portal_bank_verified=False, gst_portal_account_status="NotValidated")
    api = ApiBundle(bank_verification={"code": "0", "message": "Success",
                                        "bank_account_data": {"name": "DINESH POLYMERS", "bank_name": "Central Bank of India"}})
    r = resolve_com_bank_verification(e, api)
    assert r.value == "penny_success_gst_match" and not r.unresolved
    assert "api:ongrid.bank-verification.verify" in r.source


def test_bank_live_penny_drop_name_mismatch():
    e = dict(_BASE_BANK, legal_name="Dinesh Polymers")
    api = ApiBundle(bank_verification={"bank_account_data": {"name": "SOME OTHER ENTITY ENTIRELY"}})
    r = resolve_com_bank_verification(e, api)
    assert r.value == "penny_success_gst_mismatch" and not r.unresolved
    assert "CRITICAL" in r.note


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


def test_pin_mismatch_is_a_match_when_plot_locality_and_name_all_agree():
    r = resolve_addr_electricity_bill(_GST_ADDR, "PL.NO G-100, M.I.D.C. JALGAON", "422305",
                                       bill_village="JALGAON",
                                       bill_consumer_name="M/S. DINESH POLYMERS",
                                       entity_name="DINESH POLYMERS")
    assert r.value == "match" and "422305" in r.note


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

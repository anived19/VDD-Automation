"""Constants describing the Finoscale Data API's fixed enum values.

Kept separate from client.py so resolvers/tests can import them without
pulling in `requests`. See vendors-data-api-reference.md for the source.
"""

PROBE42_FIELD_VALUES = {
    "company", "llp", "financials", "charge_sequence", "credit_ratings",
    "authorized_signatories", "director_network", "director_shareholdings",
    "shareholdings", "shareholdings_more_than_five_percent", "shareholdings_summary",
    "securities_allotment", "subsidiary_entities", "associate_entities", "joint_ventures",
    "related_party_transactions", "legal_cases_of_financial_disputes", "legal_history",
    "bifr_history", "cdr_history", "defaulter_list", "gst_details",
    "establishments_registered_with_epfo", "msme_supplier_payment_delays",
    "open_charges_latest_event", "description", "api_version", "last_updated",
}

# Only these 5 are valid `page` values server-side -- anything else returns null/empty.
PROBE42_PAGE_VALUES = {"directors", "shareholding", "associates", "compliance", "litigation_defaults"}

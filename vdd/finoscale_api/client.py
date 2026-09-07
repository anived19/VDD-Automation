"""
Typed client for the internal Finoscale Data API (vendors-data-api-reference.md).

Wraps Ongrid (GSTIN + MSME verification), Digitap (PAN+GST aggregate),
Probe42 (comprehensive company data), and Zigram (AML/PEP/sanctions
screening) -- all captcha-free, official, paid vendor integrations already
contracted by Finoscale. This replaces any need to scrape gst.gov.in.

Every response is cached to disk (JSON) keyed by endpoint+params so re-runs
during development don't re-spend API calls or hit rate limits.
"""
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

from .models import PROBE42_FIELD_VALUES, PROBE42_PAGE_VALUES


class FinoscaleAPIError(Exception):
    def __init__(self, status_code: int, message: str, path: str = "", body: Any = None):
        self.status_code = status_code
        self.message = message
        self.path = path
        self.body = body
        super().__init__(f"[{status_code}] {path}: {message}")


@dataclass
class FinoscaleClient:
    api_key: str
    base_url: str = "https://api-ppe.finoscale.ai"
    cache_dir: Optional[str] = None
    timeout: float = 30.0
    session: requests.Session = field(default_factory=requests.Session)

    # ---------------------------------------------------------------- core
    def _cache_path(self, cache_key: str) -> Optional[str]:
        if not self.cache_dir:
            return None
        os.makedirs(self.cache_dir, exist_ok=True)
        h = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()[:24]
        safe = "".join(c if c.isalnum() else "_" for c in cache_key)[:60]
        return os.path.join(self.cache_dir, f"{safe}_{h}.json")

    def _request(self, method: str, path: str, *, params: dict = None,
                 json_body: dict = None, cache_key: str = None) -> Any:
        cache_path = self._cache_path(cache_key) if cache_key else None
        if cache_path and os.path.exists(cache_path):
            with open(cache_path, encoding="utf-8") as f:
                return json.load(f)["data"]

        url = self.base_url.rstrip("/") + path
        headers = {"X-Api-Key": self.api_key, "Content-Type": "application/json"}
        resp = self.session.request(method, url, params=params, json=json_body,
                                     headers=headers, timeout=self.timeout)
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text}

        if resp.status_code >= 400:
            msg = data.get("message") if isinstance(data, dict) else str(data)
            raise FinoscaleAPIError(resp.status_code, msg or "request failed", path, data)

        if cache_path:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({"_fetched_at": time.time(), "_path": path, "_params": params,
                           "_json_body": json_body, "data": data}, f, indent=2)
        return data

    def _cached(self, path: str, params: dict = None, json_body: dict = None, unwrap_data: bool = True):
        raw = self._request("GET" if json_body is None else "POST", path,
                             params=params, json_body=json_body,
                             cache_key=f"{path}?{params}&{json_body}")
        # Ongrid/Probe42/Zigram all wrap their real payload in a `data` envelope
        # key (confirmed against real PPE responses); Digitap does not, so those
        # methods below call this with unwrap_data=False.
        if unwrap_data and isinstance(raw, dict) and "data" in raw:
            return raw["data"]
        return raw

    # ---------------------------------------------------------------- Zigram
    def zigram_screening(self, entity_name: str, client_id: str, type_: str,
                          country: list, cin: str = None, pan: str = None, llpin: str = None):
        body = {"entityName": entity_name, "clientId": client_id, "type": type_, "country": country}
        if cin:
            body["cin"] = cin
        if pan:
            body["pan"] = pan
        if llpin:
            body["llpin"] = llpin
        return self._cached("/api/zigram/screening", json_body=body)

    # ---------------------------------------------------------------- Probe42 (Data)
    def probe42_fetch_by_field(self, entity_id: str, field_name: str):
        top_level = field_name.split(".", 1)[0]
        if top_level not in PROBE42_FIELD_VALUES:
            raise ValueError(f"Unknown Probe42 field '{field_name}' (top-level must be one of {sorted(PROBE42_FIELD_VALUES)})")
        return self._cached("/api/v1/data/fetch-comprehensive-details-by-field",
                             params={"id": entity_id, "field": field_name})

    def probe42_fetch_by_page(self, entity_id: str, page: str):
        if page not in PROBE42_PAGE_VALUES:
            raise ValueError(f"Unknown Probe42 page '{page}' -- must be one of {sorted(PROBE42_PAGE_VALUES)}")
        return self._cached("/api/v1/data/fetch-comprehensive-details-by-page",
                             params={"id": entity_id, "page": page})

    def probe42_fetch_for_entity(self, cin_or_llpin: str):
        return self._cached(f"/api/v1/data/fetch-comprehensive-details-for-entity/{cin_or_llpin}")

    def probe42_fetch_pnp(self, pan: str, identifier_type: str = None):
        params = {"identifier_type": identifier_type} if identifier_type else None
        return self._cached(f"/api/data/fetch-comprehensive-details-pnp/{pan}", params=params)

    # ---------------------------------------------------------------- Ongrid: GSTIN verification
    def ongrid_gstin_fetch_by_mobile(self, mobile_number: str):
        return self._cached("/api/vendors/ongrid/gstin-verification/fetch-by-mobile",
                             json_body={"mobileNumber": mobile_number, "consent": "Y"})

    def ongrid_gstin_fetch_by_name(self, company_name: str):
        if len(company_name) < 5:
            raise ValueError("companyName must be at least 5 characters")
        return self._cached("/api/vendors/ongrid/gstin-verification/fetch-by-name",
                             json_body={"companyName": company_name, "consent": "Y"})

    def ongrid_gstin_fetch_by_pan(self, pan_number: str):
        return self._cached("/api/vendors/ongrid/gstin-verification/fetch-by-pan",
                             json_body={"panNumber": pan_number, "consent": "Y"})

    def _ongrid_gstin_include_flags(self, include_hsn, include_filing, include_filing_frequency):
        body = {}
        if include_hsn is not None:
            body["includeHsnData"] = include_hsn
        if include_filing is not None:
            body["includeFilingData"] = include_filing
        if include_filing_frequency is not None:
            body["includeFilingFrequency"] = include_filing_frequency
        return body

    def ongrid_gstin_fetch_lite(self, gstin: str, include_hsn: bool = None,
                                 include_filing: bool = None, include_filing_frequency: bool = None):
        body = {"gstin": gstin, "consent": "Y",
                **self._ongrid_gstin_include_flags(include_hsn, include_filing, include_filing_frequency)}
        return self._cached("/api/vendors/ongrid/gstin-verification/fetch-lite", json_body=body)

    def ongrid_gstin_fetch_detailed(self, gstin: str, include_hsn: bool = True,
                                     include_filing: bool = True, include_filing_frequency: bool = True):
        body = {"gstin": gstin, "consent": "Y",
                **self._ongrid_gstin_include_flags(include_hsn, include_filing, include_filing_frequency)}
        return self._cached("/api/vendors/ongrid/gstin-verification/fetch-detailed", json_body=body)

    def ongrid_gstin_fetch_contact_details(self, gstin: str, include_hsn: bool = None,
                                            include_filing: bool = None, include_filing_frequency: bool = None):
        body = {"gstin": gstin, "consent": "Y",
                **self._ongrid_gstin_include_flags(include_hsn, include_filing, include_filing_frequency)}
        return self._cached("/api/vendors/ongrid/gstin-verification/fetch-contact-details", json_body=body)

    def ongrid_gstin_fetch_turnover_details(self, gstin: str, financial_year: str):
        return self._cached("/api/vendors/ongrid/gstin-verification/fetch-turnover-details",
                             json_body={"gstin": gstin, "financialYear": financial_year, "consent": "Y"})

    def ongrid_gstin_fetch_mcc_codes(self, gstin: str, include_hsn: bool = None,
                                      include_filing: bool = None, include_filing_frequency: bool = None):
        body = {"gstin": gstin, "consent": "Y",
                **self._ongrid_gstin_include_flags(include_hsn, include_filing, include_filing_frequency)}
        return self._cached("/api/vendors/ongrid/gstin-verification/fetch-mcc-codes", json_body=body)

    def ongrid_gstin_fetch_certificate(self, username: str, password: str):
        """Requires the VENDOR's own GST portal credentials (consent-based). Out of scope for v1."""
        return self._cached("/api/vendors/ongrid/gstin-verification/fetch-certificate",
                             json_body={"username": username, "password": password, "consent": "Y"})

    def ongrid_gstin_trigger_report(self, username: str, password: str):
        """Requires the VENDOR's own GST portal credentials (consent-based). Out of scope for v1 --
        this is the likely unlock path for the GSTR-3B/2B 'consent' scoring categories in phase 2."""
        return self._cached("/api/vendors/ongrid/gstin-verification/trigger-report",
                             json_body={"username": username, "password": password, "consent": "Y"})

    def ongrid_gstin_fetch_report(self, transaction_id: str):
        return self._cached("/api/vendors/ongrid/gstin-verification/fetch-report",
                             params={"transactionId": transaction_id})

    # ---------------------------------------------------------------- Ongrid: MSME verification
    def ongrid_msme_fetch_by_pan(self, pan_number: str, detailed_response: bool = True):
        return self._cached("/api/vendors/ongrid/msme-verification/fetch-by-pan",
                             json_body={"panNumber": pan_number, "consent": "Y",
                                        "detailedResponse": detailed_response})

    # ---------------------------------------------------------------- Ongrid: bank verification (penny drop)
    def ongrid_bank_verification_verify(self, account_number: str, ifsc: str):
        """Real penny-drop bank-account verification. Response envelope's `data`
        carries `bank_account_data.name` (the registered account-holder name
        returned by the penny drop) on success; a failed/invalid account comes
        back as a successful HTTP call with no `bank_account_data` payload, not
        an exception -- see vdd/resolve/resolvers.py::resolve_com_bank_verification
        for how that distinction is scored."""
        return self._cached("/api/vendors/ongrid/bank-verification/verify",
                             json_body={"accountNumber": account_number, "ifsc": ifsc, "consent": "Y"})

    # ---------------------------------------------------------------- Digitap
    # Digitap's response has no `data` wrapper (confirmed against a real PPE call) --
    # unwrap_data=False keeps the payload as-is instead of stripping a nonexistent key.
    def digitap_pan_and_gst(self, pan: str, client_org_id: str):
        return self._cached("/api/vendors/digitap/pan-and-gst",
                             json_body={"pan": pan, "clientOrgId": client_org_id}, unwrap_data=False)

    def digitap_pan_and_gst_market_report(self, pan: str, client_org_id: str):
        return self._cached("/api/vendors/digitap/pan-and-gst/market-report",
                             json_body={"pan": pan, "clientOrgId": client_org_id}, unwrap_data=False)

"""
Per-document-type field parsers. Regex patterns ported from
`Recykal VDD Tool Setup/PROMPT.md` step 4-5, verified against the real
extracted text of the Dinesh Polymers GST certificate and MSME/Udyam
certificate (see project plan doc / session notes).

Every parser takes raw extracted text and returns a plain dict of the
fields it could find -- missing fields are simply absent from the dict,
never guessed. Callers (resolvers) decide what "missing" means for scoring.
"""
import re
from typing import Optional

GSTIN_RE = re.compile(r'[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][A-Z0-9]Z[A-Z0-9]')
PAN_RE = re.compile(r'\b[A-Z]{5}[0-9]{4}[A-Z]\b')
IFSC_RE = re.compile(r'\b[A-Z]{4}0[A-Z0-9]{6}\b')
UDYAM_RE = re.compile(r'UDYAM-[A-Z]{2}-\d{2}-\d{7}')
MOBILE_RE = re.compile(r'\b[6-9]\d{9}\b')
EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')

STATE_CODES = {
    "01": "Jammu & Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "27": "Maharashtra", "29": "Karnataka", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "36": "Telangana", "37": "Andhra Pradesh",
}

IFSC_BANK_PREFIX = {
    "SBIN": "State Bank of India", "ICIC": "ICICI Bank", "HDFC": "HDFC Bank",
    "KKBK": "Kotak Mahindra Bank", "PUNB": "Punjab National Bank", "CNRB": "Canara Bank",
    "UBIN": "Union Bank of India", "INDB": "IndusInd Bank", "KVBL": "Karur Vysya Bank",
    "BARB": "Bank of Baroda", "IOBA": "Indian Overseas Bank", "IDIB": "Indian Bank",
    "UTIB": "Axis Bank", "YESB": "Yes Bank", "BKID": "Bank of India", "CBIN": "Central Bank of India",
}


def state_from_gstin(gstin: str) -> Optional[str]:
    return STATE_CODES.get(gstin[:2]) if gstin and len(gstin) >= 2 else None


def pan_from_gstin(gstin: str) -> Optional[str]:
    return gstin[2:12] if gstin and len(gstin) >= 12 else None


def bank_from_ifsc(ifsc: str) -> Optional[str]:
    return IFSC_BANK_PREFIX.get(ifsc[:4]) if ifsc else None


def fix_ifsc(raw: str) -> Optional[str]:
    """5th char of a real IFSC is always literal '0' -- OCR often misreads it as 'O'."""
    m = IFSC_RE.search(raw.upper().replace(" ", ""))
    if m:
        return m.group(0)
    m2 = re.search(r'\b([A-Z]{4})[O0]([A-Z0-9]{6})\b', raw.upper().replace(" ", ""))
    return (m2.group(1) + "0" + m2.group(2)) if m2 else None


# ---------------------------------------------------------------- GST Certificate
def parse_gst_certificate(text: str) -> dict:
    out = {}
    gm = GSTIN_RE.search(text.replace(" ", ""))
    if gm:
        out["gstin"] = gm.group(0)
    m = re.search(r'Legal Name\s*\n\s*(.+)', text)
    if m:
        out["legal_name"] = m.group(1).strip()
    m = re.search(r'Trade Name,?\s*if any\s*\n\s*(.+)', text)
    if m:
        out["trade_name"] = re.sub(r'^M/S\s+', '', m.group(1).strip(), flags=re.I)
    m = re.search(r'Constitution of Business\s*\n\s*(.+)', text)
    if m:
        out["constitution"] = m.group(1).strip()
    m = re.search(r'Address of Principal Place of\s*\n?\s*Business\s*\n\s*(.+?)(?:\n\s*\d\.|\n\s*Date of Liability)',
                  text, re.S)
    if m:
        out["address"] = re.sub(r'\s+', ' ', m.group(1)).strip()
    m = re.search(r'Date of Liability\s*\n\s*(\d{2}/\d{2}/\d{4})', text)
    if m:
        out["date_of_registration"] = m.group(1)
    else:
        m = re.search(r'Period of Validity\s*\n\s*From\s*\n\s*(\d{2}/\d{2}/\d{4})', text)
        if m:
            out["date_of_registration"] = m.group(1)
    m = re.search(r'Type of Registration\s*\n\s*(\w+)', text)
    if m:
        out["taxpayer_type"] = m.group(1).strip()
    if out.get("gstin"):
        out.setdefault("state", state_from_gstin(out["gstin"]))
        out.setdefault("pan", pan_from_gstin(out["gstin"]))
    return out


# ---------------------------------------------------------------- MSME / Udyam Certificate
def parse_msme_certificate(text: str) -> dict:
    out = {}
    m = UDYAM_RE.search(text)
    if m:
        out["udyam_number"] = m.group(0)
    m = re.search(r'NAME OF ENTERPRISE\s*\n\s*(.+)', text)
    if m:
        out["enterprise_name"] = re.sub(r'^M/S\s+', '', m.group(1).strip(), flags=re.I)
    types = re.findall(r'\b(Micro|Small|Medium)\b', text)
    if types:
        out["enterprise_type"] = types[-1]
    m = re.search(r'MAJOR ACTIVITY\s*\n\s*(.+)', text)
    if m:
        activity = m.group(1).strip()
        low = activity.lower()
        out["major_activity"] = ("Manufacturing" if "manufactur" in low else
                                  "Trading" if "trad" in low else
                                  "Services" if "servic" in low else activity)
    m = re.search(r'DATE OF (?:INCORPORATION|COMMENCEMENT)[^\n]*\n[^\n]*\n\s*(\d{2}/\d{2}/\d{4})', text)
    if not m:
        m = re.search(r'Date of Incorporation\s*\n?\s*(\d{2}/\d{2}/\d{4})', text)
    if m:
        out["date_of_incorporation"] = m.group(1)
    m = MOBILE_RE.search(text)
    if m:
        out["mobile"] = m.group(0)
    m = EMAIL_RE.search(text)
    if m:
        out["email"] = m.group(0)
    m = re.search(r'Type of Organisation\s*\n?\s*(\w+)', text)
    if m:
        out["organisation_type"] = m.group(1).strip()
    # National Industry Classification codes. The Udyam printout lays these out as a
    # 4-column table (2-digit / 4-digit / 5-digit / activity), each cell on its own
    # line and each prefixed with "<code> - <description>". The 5-digit code is the
    # most specific description of what the enterprise actually makes, and is what
    # the report's "Nature of Business" field should show (a bare "MANUFACTURING"
    # from MAJOR ACTIVITY is too coarse to cross-check against anything).
    # Descriptions wrap across physical lines in the PDF text layer
    # ("22209 - Manufacture of other plastics\nproducts n.e.c"), so consume
    # following lines until a blank line or the next code/label line.
    # Stop at a blank line, the next "<code> - " cell, an ALL-CAPS section label,
    # or the table's own trailing "Activity" column value (Manufacturing /
    # Trading / Services) -- which sits on its own line right after the 5-digit
    # description and would otherwise be glued onto it.
    _NIC_STOP = r'(?=\n\s*\n|\n\s*\d+\s*-\s*[A-Za-z]|\n\s*[A-Z][A-Z ]{5,}|' \
                r'\n\s*(?:Manufacturing|Trading|Services|Manufacture and Trading)\s*(?:\n|$)|\Z)'

    def _nic(width: int):
        m = re.search(r'\b(\d{%d})\s*-\s*([A-Za-z].*?)%s' % (width, _NIC_STOP), text, re.S)
        if not m:
            return None, None
        return m.group(1), re.sub(r'\s+', ' ', m.group(2)).strip().rstrip(',')

    out["nic_5_code"], out["nic_5_description"] = _nic(5)
    out["nic_4_code"], out["nic_4_description"] = _nic(4)
    for k in ("nic_5_code", "nic_5_description", "nic_4_code", "nic_4_description"):
        if not out.get(k):
            out.pop(k, None)
    pm = PAN_RE.search(text)
    if pm:
        out["pan"] = pm.group(0)
    # Bank block sometimes present on the Udyam printout (fallback source only --
    # per project rule, GST-portal screenshot outranks this for bank verification).
    m = re.search(r'Bank Name\s*\n?\s*IFS Code\s*\n?\s*Bank Account Number\s*\n?\s*(.+?)\n\s*([A-Z]{4}0[A-Z0-9]{6})\s*\n\s*(\d{6,18})', text)
    if m:
        out["bank_name_msme"] = m.group(1).strip()
        out["ifsc_msme"] = m.group(2).strip()
        out["account_number_msme"] = m.group(3).strip()
    return out


# ---------------------------------------------------------------- Cancelled Cheque
def parse_cancelled_cheque(text: str) -> dict:
    out = {}
    ifsc = fix_ifsc(text)
    if ifsc:
        out["ifsc"] = ifsc
        out["bank_name"] = bank_from_ifsc(ifsc)
    # Prefer an explicitly labeled account number (A/c No, Account No, खा.सं.) over any
    # other long digit string on the cheque -- a redacted/highlighted box elsewhere on
    # the leaf can coincidentally be the same length and isn't necessarily the account.
    labeled = re.search(r'(?:A/?c\.?\s*NO\.?|Account\s*No\.?|खा\.?\s*सं\.?)\s*[:\-]?\s*(\d{9,18})', text, re.I)
    if labeled:
        out["account_number"] = labeled.group(1)
    else:
        candidates = [n for n in re.findall(r'\b\d{9,18}\b', text.replace(" ", ""))
                      if n != (out.get("ifsc") or "") and len(n) != 6]
        if candidates:
            out["account_number"] = max(candidates, key=len)
    m = re.search(r'FOR\s+([A-Z][A-Za-z .&]+)', text)
    if m:
        holder = re.sub(r'\b(PROPRIETOR|DIRECTOR|PVT\.?|LTD\.?|LIMITED)\b', '', m.group(1), flags=re.I).strip()
        out["account_holder"] = holder
    # Custom Instructions rule 2: a file named "Cancelled Cheque" isn't valid proof
    # unless it actually shows a diagonal strike-through / handwritten "cancelled"
    # mark -- a blank unused leaf doesn't count even if that's what the filename claims.
    low = text.lower()
    out["cancellation_mark_present"] = bool(
        re.search(r'diagonal.{0,40}(cancel|strike)', low) or re.search(r'\bcancel+ed?\b', low))
    return out


# ---------------------------------------------------------------- Electricity Bill
def parse_electricity_bill(text: str) -> dict:
    """Electricity board bill layouts vary a lot state to state -- the
    "Consumer Name:"/"Address:" labels below matched the original (MSEDCL /
    Maharashtra-style) bill this was built against, but a real Tamil Nadu
    TANGEDCO bill (Skandan Plastrix, 2026-09-07) has neither label at all,
    instead printing "Name/Address & GST of the Consumer" followed by the
    name then the address on the next two lines. Every field this parser
    can't find for a given board's format is simply absent, same as always
    -- resolvers must not guess, but this fallback recovers what a human
    would obviously read off the bill without needing every board's exact
    label vocabulary hardcoded.
    """
    out = {}
    m = re.search(r'(?:Consumer Name|ग्राहकाचे नाव|उपभोक्ता का नाम|ಗ್ರಾಹಕರ ಹೆಸರು|వినియోగదారు పేరు|நுகர்வோர் பெயர்)\s*[:\-]?\s*(.+)', text, re.I)
    if m:
        out["consumer_name"] = m.group(1).strip()
    m = re.search(r'(?:Address|पत्ता|पता|ವಿಳಾಸ|చిరునామా|முகவரி)\s*[:\-]?\s*\n?\s*(.+?)(?:\n\s*(?:Village|Pin Code|Category|गाव|पिन कोड|प्रवर्ग|गांव|ಗ್ರಾಮ|ವರ್ಗ|వర్గం|வகை)|\Z)', text, re.S | re.I)
    if m:
        addr_lines = [l.strip() for l in m.group(1).splitlines() if l.strip()]
        out["address"] = ", ".join(addr_lines)

    if "consumer_name" not in out and "address" not in out:
        m = re.search(r'Name/Address\s*&?\s*GST of the Consumer\s*\n\s*(.+?)\n\s*(.+?)\n\s*State\s*[:\-]',
                       text, re.S)
        if m:
            out["consumer_name"] = m.group(1).strip()
            out["address"] = re.sub(r'\s+', ' ', m.group(2)).strip()
    m = re.search(r'(?:Pin Code|पिन कोड|ಪಿನ್ ಕೋಡ್|పిన్ కోడ్|அஞ்சல் குறியீடு)\s*[:\-]?\s*(\d{6})', text, re.I)
    if m:
        out["pincode"] = m.group(1)
    m = re.search(r'(?:Category|प्रवर्ग|श्रेणी|ವರ್ಗ|వర్గం|வகை)\s*[:\-]?\s*(\w+)', text, re.I)
    if m:
        out["category"] = m.group(1)
    # The declared connection activity is an independent, utility-issued statement
    # of what happens at the premises -- the strongest available cross-check on the
    # Udyam NIC code, and the corroboration the reference report cites explicitly.
    m = re.search(r'(?:Activity|वापर|उपयोग|ಬಳಕೆ|వినియోగం|பயன்பாடு)\s*[:\-]?\s*(.+)', text, re.I)
    if m:
        out["activity"] = m.group(1).strip()
    m = re.search(r'(?:Village|गाव|गांव|ಗ್ರಾಮ|గ్రామం|கிராமம்)\s*[:\-]?\s*([A-Za-z .]+)', text, re.I)
    if m:
        out["village"] = m.group(1).strip()
    m = re.search(r'(?:Date of Connection|जोडणीची तारीख|कनेक्शन की तिथि|ಸಂಪರ್ಕ ದಿನಾಂಕ|కనెక్షన్ తేదీ|இணைப்பு தேதி)\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})', text, re.I)
    if m:
        out["date_of_connection"] = m.group(1)
    m = re.search(r'(?:Sanctioned Load|मंजूर भार|मंजूर वीजभार|स्वीकृत भार|ಮಂಜೂರಾದ ಲೋಡ್|మంజూరైన లోడ్|அனுமதிக்கப்பட்ட சுமை)\s*[:\-]?\s*([\d.]+\s*\w+)', text, re.I)
    if m:
        out["sanctioned_load"] = m.group(1).strip()
    return out


# ---------------------------------------------------------------- GST Portal bank-verification screenshot
def parse_gst_portal_screenshot(text: str) -> dict:
    out = {}
    ifsc = fix_ifsc(text)
    if ifsc:
        out["ifsc"] = ifsc
    m = re.search(r'Account Number\s*[:\-]?\s*(\d{9,18})', text)
    if m:
        out["account_number"] = m.group(1)
    m = re.search(r'Bank Name\s*[:\-]?\s*(.+)', text)
    if m:
        out["bank_name"] = m.group(1).strip()
    # Anchor on a colon-delimited "Account Status: X" key/value line. Matching the
    # bare label would also hit the breadcrumb heading ("... > Bank Account Status")
    # and the table's own column header, both of which are followed by unrelated
    # text -- that previously yielded account_status="Sl" (from "Sl No").
    m = re.search(r'(?<!Bank )Account Status\s*[:\-]\s*([A-Za-z][\w ]*)', text)
    if m:
        out["account_status"] = m.group(1).strip()
    # Type of Account matters for interpreting a NotValidated status: GSTN's bank
    # validation runs through NPCI, which does not reliably support CC/OD accounts,
    # so "CC" + NotValidated is a known benign pattern rather than a failed penny drop.
    m = re.search(r'^\s*\d+\s*\|\s*(CC|OD|SB|CA|OCC|BC|CD)\s*\|', text, re.M | re.I)
    if m:
        out["type_of_account"] = m.group(1).upper()
    else:
        m = re.search(r'Type of Account\s*[:\-]\s*([A-Za-z]{2,3})\b', text)
        if m:
            out["type_of_account"] = m.group(1).upper()
    # A visual-inspection transcript can state the read status unambiguously via
    # this marker; naive keyword matching on free text is fragile (e.g. "not
    # currently validated" contains the substring "validated"), so prefer this
    # explicit signal when present.
    m = re.search(r'PARSED_STATUS:\s*(VERIFIED|NOT_VERIFIED)', text)
    if m:
        out["bank_verified"] = (m.group(1) == "VERIFIED")
    else:
        low = text.lower()
        out["bank_verified"] = bool(re.search(r'\bvalidated\b', low) and not re.search(r'\bnot\b.{0,20}\bvalidated\b', low))
    return out


# ---------------------------------------------------------------- PAN Card
def parse_pan_card(text: str) -> dict:
    """Two real layouts, both seen in practice:
    - A physical/scanned PAN card has no "Name:" label -- the
      cardholder/entity name is just the first non-blank line printed
      directly under the "GOVT. OF INDIA" header line, before the
      DOB/formation-date line.
    - An e-PAN (electronically issued) has NO "GOVT. OF INDIA" header at
      all -- confirmed on MVIKAS's and Skandan's real entity e-PAN
      documents (2026-09-07), whose text layout is
      "<PAN>\\n<date>\\n<NAME>\\n<PAN>\\n<NAME, sometimes wrapped>\\n<date>\\n<footer>".
      The name is the first non-date, non-footer line right after the PAN
      number (skipping past the date line in between). This was a real,
      silent gap before this fix -- both e-PAN documents extracted a `pan`
      but never a `name`, so `ident_pan_name_match` was unresolved on both
      vendors despite the name being sitting right there in the text.
    """
    out = {}
    m = PAN_RE.search(text)
    if m:
        out["pan"] = m.group(0)
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    def _is_date(s: str) -> bool:
        return bool(re.match(r'^\d{2}/\d{2}/\d{4}$', s))

    for i, line in enumerate(lines):
        if re.search(r'govt\.?\s*of\s*india', line, re.I):
            for cand in lines[i + 1:]:
                if _is_date(cand) or 'permanent account' in cand.lower():
                    break
                out["name"] = cand
                break
            break

    if "name" not in out:
        for i, line in enumerate(lines):
            if PAN_RE.fullmatch(line.replace(" ", "")):
                j = i + 1
                while j < len(lines) and _is_date(lines[j]):
                    j += 1
                if j < len(lines):
                    cand = lines[j]
                    if (not _is_date(cand) and 'permanent account' not in cand.lower()
                            and 'electronically issued' not in cand.lower()
                            and not PAN_RE.fullmatch(cand.replace(" ", ""))):
                        out["name"] = cand
                break

    return out


# ---------------------------------------------------------------- KYC Form (fallback source for gaps)
def parse_kyc_form(text: str) -> dict:
    out = {}
    m = GSTIN_RE.search(text.replace(" ", ""))
    if m:
        out["gstin"] = m.group(0)
    m = PAN_RE.search(text)
    if m:
        out["pan"] = m.group(0)
    ifsc = fix_ifsc(text)
    if ifsc:
        out["ifsc"] = ifsc
    # Bank account row on the customer-registration form -- a third independent
    # witness to the account number, used only as cross-check evidence for the
    # bank-verification disclosure (never as a substitute for a penny drop).
    m = re.search(r'Bank Account Details[^\n]*[:\-]\s*([^\n]+)', text, re.I)
    if m:
        am = re.search(r'\b(\d{9,18})\b', m.group(1))
        if am:
            out["account_number"] = am.group(1)
        bn = re.match(r'\s*([A-Za-z][A-Za-z .&]+?)\s*,', m.group(1))
        if bn:
            out["bank_name"] = bn.group(1).strip()
    m = MOBILE_RE.search(text)
    if m:
        out["mobile"] = m.group(0)
    m = EMAIL_RE.search(text)
    if m:
        out["email"] = m.group(0)
    return out


# ---------------------------------------------------------------- Factory License / PCB Consent
def parse_factory_license(text: str) -> dict:
    """Tamil Nadu Directorate of Industrial Safety and Health "Registration
    and Licence to work a factory" (Form No.4). Only the premises
    description is extracted -- this is a corroborating-evidence source for
    a SEPARATE manufacturing premises, not a substitute for the GST-address
    match itself (see resolve_addr_electricity_bill)."""
    out = {}
    m = re.search(r'Registration Number\s*:?\s*([A-Za-z0-9]+)', text)
    if m:
        out["registration_number"] = m.group(1).strip()
    m = re.search(r'Description of Licensed Premises\s*(.+)', text, re.S)
    if m:
        out["premises_address"] = re.sub(r'\s+', ' ', m.group(1)).strip()[:400]
    return out


def parse_pcb_certificate(text: str) -> dict:
    """Tamil Nadu Pollution Control Board "Consent to Operate" (Air/Water,
    Sections 21/25). Same corroborating-evidence role as
    parse_factory_license -- confirms a licensed manufacturing premises
    address independent of the GST-registered office address."""
    out = {}
    m = re.search(r'Consent Order No\.?\s*:?\s*([A-Za-z0-9]+)', text, re.I)
    if m:
        out["consent_order_no"] = m.group(1).strip()
    m = re.search(r'(S\.?\s*F\.?\s*No\.?.+?District)', text, re.S | re.I)
    if m:
        out["premises_address"] = re.sub(r'\s+', ' ', m.group(1)).strip()[:400]
    return out


PARSERS = {
    "gst_certificate": parse_gst_certificate,
    "msme_certificate": parse_msme_certificate,
    "cancelled_cheque": parse_cancelled_cheque,
    "gst_portal": parse_gst_portal_screenshot,
    "pan_entity": parse_pan_card,
    "pan_owner": parse_pan_card,
    "kyc_form": parse_kyc_form,
    "electricity_bill": parse_electricity_bill,
    "factory_license": parse_factory_license,
    "pcb_certificate": parse_pcb_certificate,
}


def parse_document(doc_type: str, text: str) -> dict:
    parser = PARSERS.get(doc_type)
    return parser(text) if parser and text else {}

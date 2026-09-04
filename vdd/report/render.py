"""
Render a VDD report context dict into the branded Finoscale
"Seller Intelligence Report" HTML/PDF.

Ported from the existing `finoscale-vdd-report.skill` (`scripts/vdd_report.py`,
found in VDD_Project_Instructions). That version parses a human-completed
.docx into this same context shape via `parse()`; here we skip the docx
round-trip entirely and expect `build_context.py` to have already assembled
the context dict from resolved scoring-model values.

Context dict shape (`d`), unchanged from the original script:
    firm, legal, date, location, seller_type: str
    score, com, poa, poi, aml: int
    entity: list[(label, value)]
    reg: list[(label, value, "m" | "")]          # "m" -> monospace value
    profile: str (HTML-escaped)
    hsn: list[(code, description)]
    findings: list[("c" | "n", text)]             # confirmed / note
    com_rows, poa_rows, poi_rows, aml_rows: list[(code, parameter, result)]
    extra_unlock: list[str]                        # subset of the 3 tags below
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
HEAD = open(os.path.join(ASSETS, "template_head.html"), encoding="utf-8").read()
LOGO = open(os.path.join(ASSETS, "logo_block.txt"), encoding="utf-8").read()

STATES = ["Uttar Pradesh", "Andhra Pradesh", "West Bengal", "Tamil Nadu", "Madhya Pradesh", "Himachal Pradesh",
          "Arunachal Pradesh", "Bihar", "Jharkhand", "Odisha", "Karnataka", "Kerala", "Maharashtra", "Delhi",
          "Telangana", "Gujarat", "Rajasthan", "Punjab", "Haryana", "Assam", "Goa", "Chhattisgarh", "Uttarakhand"]
KEEP_UP = {"IDBI", "ICICI", "HDFC", "SBI", "RBL", "PNB", "IDFC", "SME", "NIC"}


# ---------------------------------------------------------------- text helpers
def esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def first_date(s):
    m = re.search(r'(\d{2})/(\d{2})/(\d{4})', s)
    if m:
        return m.group(0)
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', s)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    m = re.search(r'\b(19|20)\d{2}\b', s)
    return m.group(0) if m else s.strip()


def clean_bank(s):
    """Normalize a raw bank name string (e.g. from IFSC lookup or cheque OCR) for display."""
    s = re.split(r'[—/(]| - ', s)[0]
    s = re.sub(r'\b(Limited|Ltd\.?)\b', '', s, flags=re.I).strip().rstrip(',')
    out = []
    for w in s.split():
        wu = w.upper().strip('.')
        if wu in KEEP_UP:
            out.append(wu)
        elif w.lower() in ("of", "and"):
            out.append(w.lower())
        else:
            out.append(w.capitalize())
    return " ".join(out)


def location(addr):
    """Derive a short 'City, State' label from a full registered-office address string."""
    addr = re.split(r'[(]', addr)[0]
    parts = [p.strip() for p in re.split(r'[,/]', addr) if p.strip()]
    idx = st = None
    for i in range(len(parts) - 1, -1, -1):
        for s in STATES:
            if s.lower() in parts[i].lower():
                st = s
                idx = i
                break
        if st:
            break
    if st is None:
        return parts[-1] if parts else addr
    city = ""
    for j in range(idx - 1, -1, -1):
        seg = re.sub(r'[–\-].*', '', parts[j]).strip()
        seg = re.sub(r'\d', '', seg).strip()
        low = seg.lower()
        if seg and len(seg) > 1 and not low.endswith("road") and "building" not in low \
                and "floor" not in low and "block" not in low and "no." not in low:
            city = seg
            break
    city = re.sub(r'\bDistrict\b', '', city).strip()
    return (city + ", " + st) if city else st


# ---------------------------------------------------------------- CSS fix (weasyprint)
def fix_css(c):
    c = c.replace(
        "--mono: 'DM Mono', monospace; --serif: 'Playfair Display', serif; --sans: 'DM Sans', sans-serif;",
        "--mono: 'Noto Sans Mono',monospace; --sans: 'Lato',Arial,sans-serif;")
    c = c.replace("background: linear-gradient(135deg, #0F172A 0%, #1E293B 100%)", "background: #131C2E")
    c = c.replace("background: linear-gradient(135deg,#0F172A,#1E293B)", "background: #131C2E")
    c = c.replace("background:linear-gradient(135deg,#D97706,#F59E0B)", "background:#E8930A")
    c = c.replace("background: rgba(255,255,255,.05);", "background: #243044;")
    c = c.replace("background: rgba(251,191,36,.04);", "background: #1E2D3D;")
    c = c.replace("color: rgba(255,255,255,.35)", "color: #7A8A9A")
    c = c.replace("color: rgba(255,255,255,.4)", "color: #8A9AAA")
    c = c.replace("border-color: rgba(251,191,36,.2); background: rgba(251,191,36,.04);",
                  "border-color: #6B5200; background: #1C1800;")
    c = c.replace('style="color:rgba(255,255,255,.35);white-space:nowrap"',
                  'style="color:#D4A800;white-space:nowrap"')
    c = c.replace(
        "background:rgba(251,191,36,.12); border:1px solid rgba(251,191,36,.3); color:#FBBF24;",
        "background:#2A2800; border:1px solid #B8860B; color:#F59E0B;")
    c = c.replace(
        "background:repeating-linear-gradient(90deg,rgba(251,191,36,.4) 0px,rgba(251,191,36,.4) 5px,rgba(251,191,36,.1) 5px,rgba(251,191,36,.1) 10px)",
        "background:repeating-linear-gradient(90deg,#B8860B 0px,#B8860B 5px,#4A3A0A 5px,#4A3A0A 10px)")
    c = re.sub(r'\.hdr::before\s*\{[^}]*radial-gradient[^}]*\}', '.hdr::before { display: none; }', c)
    c = re.sub(r'(\.ftr\s*\{[^}]*?)background\s*:\s*var\(--slate\)', r'\1background:#1E293B', c)
    c = re.sub(r'(\.section-header-row\s+td\s*\{[^}]*?)background\s*:\s*var\(--slate\)\s*!important',
               r'\1background:#1E293B !important', c)
    c = re.sub(r'\.chip\s*\{([^}]*?)border\s*:\s*1px solid\s*;([^}]*)\}',
               lambda m: '.chip {' + m.group(1) + 'display: inline-block; white-space: nowrap;' + m.group(2) + '}',
               c)
    c = re.sub(r'\.chip-teal\s*\{[^}]*\}',
               '.chip-teal { background: #0D4A40; border: 1.5px solid #0D4A40; color: #5EEAD4; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; }',
               c)
    c = re.sub(r'\.chip-blue\s*\{[^}]*\}',
               '.chip-blue { background: #1A3A70; border: 1.5px solid #1A3A70; color: #93C5FD; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; }',
               c)
    c = re.sub(r'\.chip-white\s*\{[^}]*\}',
               '.chip-white { background: #1E2D42; border: 1.5px solid #1E2D42; color: #B8C8D8; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; }',
               c)
    c = re.sub(r'(\.brand-tag\s*\{[^}]*?color\s*:\s*)rgba\(255,255,255,\.45\)', r'\1#8A9AAA', c)
    c = re.sub(r'(\.brand-tag\s*\{)([^}]*)\}', lambda m: m.group(1) + m.group(2) + ' white-space: nowrap;}', c)
    c = c.replace('.unlock-card { background:#1E293B;', '.unlock-card { background:#1A2840;')
    return c


# ---------------------------------------------------------------- build HTML
def _bar(r):
    return "#059669" if r >= 0.75 else ("#D97706" if r >= 0.45 else "#DC2626")


def _kv(k, v):
    return f'<div class="kv"><span class="kv-key">{k}</span><span class="kv-dot"></span><span class="kv-val">{v}</span></div>'


def _kvm(k, v):
    return f'<div class="kv"><span class="kv-key">{k}</span><span class="kv-dot"></span><span class="kv-val mono">{v}</span></div>'


def _uc(cat, name, desc, cur, den, cta, sub, pill, partial=False):
    pc = " partial" if partial else ""
    w = round(cur / den * 100) if den else 0
    lbl = "Current parameter" if partial else "Progress"
    return (f'<div class="unlock-card{pc}"><div class="unlock-pts-pill{pc}">{pill}</div>'
            f'<div><div class="unlock-cat">{cat}</div><div class="unlock-name">{name}</div></div>'
            f'<div class="unlock-desc">{desc}</div>'
            f'<div class="unlock-progress"><div class="unlock-prog-row"><span class="unlock-prog-label">{lbl}</span>'
            f'<span class="unlock-prog-val">{cur} / {den} pts</span></div>'
            f'<div class="unlock-prog-track"><div class="unlock-prog-fill" style="width:{w}%;background:#D97706"></div></div></div>'
            f'<div class="unlock-cta"><div class="unlock-cta-arrow">&rarr;</div><div><div class="unlock-cta-text">{cta}</div>'
            f'<div class="unlock-cta-sub">{sub}</div></div></div></div>')


def build(d):
    score = d['score']
    scope = 100 - score
    com, poa, poi, aml = d['com'], d['poa'], d['poi'], d['aml']
    _PIN_SVG = ('<svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
                'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" '
                'style="vertical-align:-1px;margin-right:4px"><path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 '
                '16 0Z"/><circle cx="12" cy="10" r="3"/></svg>')
    _CHIP_STYLE = {
        "teal": "background:#0D4A40;border:1.5px solid #0D4A40;color:#5EEAD4",
        "blue": "background:#1A3A70;border:1.5px solid #1A3A70;color:#93C5FD",
        "white": "background:#1E2D42;border:1.5px solid #1E2D42;color:#B8C8D8",
        "amber": "background:#3A2E05;border:1.5px solid #6B5200;color:#FBBF24",
    }
    # Chips are supplied by build_context from resolved values -- never hardcoded.
    # `chip-amber` has no class in the shared template head, so amber chips carry
    # an inline style (kept identical in shape to fix_css's chip rules).
    chips = ""
    for kind, label in d.get("chips") or []:
        if label.startswith("@LOC@"):
            label = _PIN_SVG + label[len("@LOC@"):]
        cls = f"chip chip-{kind}" if kind != "amber" else "chip"
        extra = (f' style="{_CHIP_STYLE["amber"]};padding:2px 8px;border-radius:999px;font-size:11px;'
                 f'font-weight:600;display:inline-block;white-space:nowrap"') if kind == "amber" else ""
        chips += f'<span class="{cls}"{extra}>{label}</span>'
    hdr = (f'<div class="hdr"><div class="hdr-inner"><div class="hdr-top"><div class="brand">{LOGO}'
           '<div><div class="brand-name">finoscale</div><div class="brand-tag">Seller Intelligence Platform</div></div></div>'
           f'<div class="hdr-date">Report Date: {d["date"]}</div></div>'
           f'<div class="hdr-firm"><div class="hdr-label">Verified Seller Profile</div><div class="hdr-firmname">{d["firm"]}</div>'
           f'<div class="hdr-proprietor">Legal Name: {d["legal"]}</div></div><div class="hdr-chips">{chips}</div></div></div>')

    def card(lbl, v, den):
        r = v / den
        return (f'<div class="sc-card"><div class="sc-card-lbl">{lbl}</div><div class="sc-card-val">{v}'
                f'<span style="font-size:11px;opacity:.5"> /{den}</span></div><div class="sc-bar">'
                f'<div class="sc-fill" style="width:{round(r * 100)}%;background:{_bar(r)}"></div></div></div>')

    pend = lambda l, den: (f'<div class="sc-card pending-card"><div class="sc-card-lbl">{l}</div>'
        f'<div class="sc-card-val" style="color:rgba(255,255,255,.35);white-space:nowrap">0<span style="font-size:11px;opacity:.5"> /{den}</span></div>'
        '<div class="pending-tag"><svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="#FBBF24" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-1px;margin-right:3px"><path d="M5 22h14M5 2h14M17 22v-4.17a2 2 0 0 0-.59-1.41L12 12l-4.41 4.41A2 2 0 0 0 7 17.83V22M7 2v4.17a2 2 0 0 0 .59 1.41L12 12l4.41-4.41A2 2 0 0 0 17 6.17V2"/></svg>Pending</div></div>')

    scoreband = (f'<div class="score-band"><div class="score-main"><div class="score-main-lbl">Finoscale Basic Score</div>'
        f'<div><span class="score-main-num">{score}</span><span class="score-main-denom"> / 100</span></div>'
        f'<div class="score-sub">Verified data only &middot; {d["date"]}</div></div>'
        '<div style="display:flex;flex-direction:column;flex:1"><div class="score-cards">'
        f'{card("Compliance", com, 25)}{card("Proof of Address", poa, 10)}{card("Proof of Identity", poi, 10)}{card("Legal / AML", aml, 5)}</div>'
        f'<div class="score-cards-row2">{pend("On-Site Verification", 20)}{pend("3B Analysis", "12.5")}{pend("2B Analysis", "12.5")}{pend("ITR Analysis", 5)}</div></div></div>')

    prog = (f'<div class="prog-wrap"><div class="prog-legend"><div class="prog-legend-left">'
        '<div style="display:flex;align-items:center;gap:6px"><div class="prog-legend-dot" style="background:linear-gradient(90deg,#059669,#34D399)"></div>'
        f'<span class="prog-legend-label">Current Score &nbsp;<strong>{score}</strong></span></div>'
        '<div style="display:flex;align-items:center;gap:6px"><div class="prog-legend-dot" style="background:repeating-linear-gradient(90deg,rgba(251,191,36,.5) 0px,rgba(251,191,36,.5) 4px,rgba(251,191,36,.12) 4px,rgba(251,191,36,.12) 8px)"></div>'
        f'<span class="prog-legend-label">Scope of Improvement &nbsp;<strong style="color:#FBBF24">+{scope}</strong></span></div></div>'
        '<span style="font-family:\'DM Mono\',monospace;font-size:10px;color:rgba(255,255,255,.3)">Max possible: 100 / 100</span></div>'
        f'<div class="prog-bar-track"><div class="prog-bar-current" style="width:{score}%"></div><div class="prog-bar-scope" style="left:{score}%;width:{scope}%"></div></div>'
        '<div class="prog-ticks"><span class="prog-tick">0</span><span class="prog-tick">25</span><span class="prog-tick">50</span><span class="prog-tick">75</span><span class="prog-tick">100</span></div></div>')

    ucards = [
        _uc("On-Site Verification &middot; Pending", "Physical On-Site Verification",
            "Premises, stock, and operations have not been physically verified. Completing OSV is the highest single scoring action available.",
            0, 20, "Schedule OSV Visit &rarr; unlock +20 pts", "Coordinate with your Finoscale account manager", "0 / 20 pts"),
        _uc("3B &amp; 2B Analysis &middot; Pending", "GST Return Analysis (3B &amp; 2B)",
            "Detailed GSTR-3B turnover and GSTR-2B ITC analysis is pending. Requires one-time GST portal consent to pull filing data.",
            0, 25, "Grant GST Data Consent &rarr; unlock +25 pts", "One-time authorisation on the GST portal", "0 / 25 pts"),
        _uc("ITR Analysis &middot; Pending", "ITR &amp; Tax Compliance History",
            "Income Tax Return filing history is not on file. Consistent ITR with matching declared turnover significantly strengthens the business profile.",
            0, 5, "Submit ITR Documents &rarr; unlock +5 pts", "Upload last 2 years of ITR acknowledgements", "0 / 5 pts"),
    ]
    for tag in d['extra_unlock']:
        if tag == "__COMPLIANCE_PARTIAL__":
            ucards.append(_uc("Compliance &middot; Partial", "Improve GST Filing Timeliness",
                "GST 3B &amp; R1 filings show delays over the last 12 months. Consistent on-time filing restores the full compliance score.",
                com, 25, "File GST returns on time &rarr; unlock compliance points",
                "Maintain timely monthly GSTR-3B &amp; R1 filing", f"{com} / 25 pts", True))
        elif tag == "__POA_PARTIAL__":
            ucards.append(_uc("Proof of Address &middot; Partial", "Complete Premises Documentation",
                "Premises ownership / rental status or a matching utility bill is incomplete. A matching utility bill plus ownership or rental proof restores the full address-proof score.",
                poa, 10, "Submit premises &amp; address proof &rarr; unlock PoA points",
                "Ownership / rental agreement and matching utility bill", f"{poa} / 10 pts", True))
        elif tag == "__IDENTITY_PARTIAL__":
            ucards.append(_uc("Proof of Identity &middot; Partial", "Resolve PAN&ndash;GST Name Mismatch",
                "The PAN name does not match the GST legal name. Aligning the PAN and GST legal name restores the full identity score.",
                poi, 10, "Reconcile PAN &amp; GST legal name &rarr; unlock points",
                "Submit matching PAN and GST registration records", f"{poi} / 10 pts", True))

    unlock = ('<div class="unlock-sec"><div class="unlock-header"><div><div class="unlock-title">Unlock Your Full Score</div>'
        f'<div class="unlock-subtitle">3 parameters are pending &mdash; submit documents to increase your score by up to <span style="color:#FBBF24;font-weight:600">+{scope} points</span></div></div>'
        f'<div class="unlock-badge"><div class="unlock-badge-label">Points Unlockable</div><div class="unlock-badge-num">+{scope}</div><div class="unlock-badge-sub">across {len(ucards)} areas</div></div></div>'
        f'<div class="unlock-grid">{"".join(ucards)}</div></div>')

    left = '<div class="body-col"><div class="sec-title"><span class="sec-title-bar" style="background:var(--accent)"></span>Entity Details</div><div class="kv-list">'
    for k, v in d['entity']:
        left += _kv(k, v)
    left += '</div></div>'
    right = '<div class="body-col"><div class="sec-title"><span class="sec-title-bar" style="background:var(--accent)"></span>Registration &amp; Compliance IDs</div><div class="kv-list">'
    for k, v, m in d['reg']:
        right += (_kvm(k, v) if m == 'm' else _kv(k, v))
    right += '</div></div>'
    bodygrid = f'<div class="body-grid">{left}{right}</div>'

    profile = f'<div class="profile-sec"><div class="sec-title"><span class="sec-title-bar" style="background:var(--accent)"></span>Business Profile</div><p class="profile-text">{d["profile"]}</p></div>'

    hsn = '<div class="hsn-sec"><div class="sec-title"><span class="sec-title-bar" style="background:var(--accent)"></span>Declared Goods &amp; Services (HSN Codes)</div><table class="hsn-table"><thead><tr><th style="width:120px">HSN / SAC</th><th>Description</th></tr></thead><tbody>'
    for code, desc in d['hsn']:
        hsn += f'<tr><td><span class="hsn-code">{code}</span></td><td>{desc}</td></tr>'
    hsn += '</tbody></table></div>'

    findings = '<div class="findings-sec"><div class="sec-title"><span class="sec-title-bar" style="background:var(--accent)"></span>Findings &amp; Observations</div><div class="findings-grid">'
    for t, txt in d['findings']:
        cls, ic, sym = ("finding-confirmed", "icon-confirmed", "&#10003;") if t == 'c' else ("finding-note", "icon-note", "!")
        findings += f'<div class="finding-item {cls}"><span class="finding-icon {ic}">{sym}</span>{txt}</div>'
    findings += '</div></div>'

    annex = '<div class="annex-sec"><div class="sec-title"><span class="sec-title-bar" style="background:var(--accent)"></span>Compliance &amp; KYC Scoring Detail</div><table class="annex-table"><thead><tr><th style="width:80px">Code</th><th>Parameter</th><th>Result</th></tr></thead><tbody>'

    def sect(title, val, pending=False):
        col = '#FBBF24' if pending else '#fff'
        return f'<tr class="section-header-row"><td colspan="2"><strong>{title}</strong> &middot; {"Pending" if pending else "No Consent"}</td><td style="text-align:right;color:{col} !important;font-family:var(--mono);white-space:nowrap">{val}</td></tr>'

    row = lambda c, p, r: f'<tr><td><span class="code-tag">{c}</span></td><td>{p}</td><td>{r}</td></tr>'
    annex += sect("COMPLIANCE", com)
    for c, p, r in d['com_rows']:
        annex += row(c, p, r)
    annex += sect("PROOF OF ADDRESS", poa)
    for c, p, r in d['poa_rows']:
        annex += row(c, p, r)
    annex += sect("PROOF OF IDENTITY", poi)
    for c, p, r in d['poi_rows']:
        annex += row(c, p, r)
    annex += sect("LEGAL / AML CHECK", aml)
    for c, p, r in d['aml_rows']:
        annex += row(c, p, r)
    annex += sect("ON-SITE VERIFICATION", 0, True) + row("OSV-01", "Physical On-Site Verification", "Not yet completed &mdash; pending scheduling")
    annex += sect("3B &amp; 2B ANALYSIS", 0, True) + row("3B-01", "GSTR-3B Turnover Analysis", "Pending &mdash; GST consent not yet granted") + row("2B-01", "GSTR-2B ITC Analysis", "Pending &mdash; GST consent not yet granted")
    annex += sect("ITR ANALYSIS", 0, True) + row("ITR-01", "ITR Filing History &amp; Tax Compliance", "Pending &mdash; ITR documents not submitted")
    annex += '</tbody></table></div>'

    ftr = '<div class="ftr"><div class="ftr-brand"><strong>finoscale.ai</strong> &nbsp;&middot;&nbsp; Seller Intelligence &amp; KYC Platform</div><div class="ftr-disclaimer">This report is generated for platform use only. Scores are based on available data at the time of assessment. Finoscale does not guarantee accuracy of third-party data. For queries: www.finoscale.ai</div></div>'

    head = HEAD.replace("KASHI ENTERPRISES &mdash; Seller Intelligence Report", d['firm'] + " &mdash; Seller Intelligence Report")
    return head + hdr + scoreband + prog + unlock + bodygrid + profile + hsn + findings + annex + ftr + '\n</div></body></html>'


# ---------------------------------------------------------------- render tight-height PDF
def _pdf_page_count(pdf_bytes):
    """Count pages in a rendered PDF. Uses PyMuPDF instead of the original
    script's pdf2image+poppler dependency -- no extra system binary needed."""
    import pymupdf as fitz
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        return doc.page_count


def render_pdf(html, out_pdf):
    import weasyprint
    css_html = fix_css(html)
    hp = os.path.dirname(os.path.abspath(out_pdf)) + "/"

    def render(h):
        css = f'@page {{ size: 960px {h}px; margin: 0; }} html,body{{width:960px;background:#F1F3F7;}}'
        return weasyprint.HTML(string=css_html + f'<style>{css}</style>', base_url='file://' + hp).write_pdf(presentational_hints=True)

    best = None
    for h in range(4200, 2999, -100):  # coarse
        pdf = render(h)
        if _pdf_page_count(pdf) == 1:
            best = (h, pdf)
        elif best:
            break
    if not best:
        best = (4200, render(4200))
    h = best[0] - 10  # fine
    while h > 2999:
        pdf = render(h)
        if _pdf_page_count(pdf) == 1:
            best = (h, pdf)
            h -= 10
        else:
            break
    open(out_pdf, 'wb').write(best[1])
    return best[0]


def generate_report(context, out_dir, html_only=False):
    """context: the `d` dict shape documented at the top of this file.
    Returns (html_path, pdf_path | None)."""
    os.makedirs(out_dir, exist_ok=True)
    fname = re.sub(r'[^A-Za-z0-9]+', '_', context['firm']).strip('_').upper() + "_VDD_Report"
    html = build(context)
    html_path = os.path.join(out_dir, fname + ".html")
    open(html_path, "w", encoding="utf-8").write(html)
    if html_only:
        return html_path, None
    pdf_path = os.path.join(out_dir, fname + ".pdf")
    render_pdf(html, pdf_path)
    return html_path, pdf_path

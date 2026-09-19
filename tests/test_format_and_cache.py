"""
Robustness items 3 and 4 from the 18-Sep review of "what else can go wrong
with real vendor folders":

* format handling -- an encrypted PDF, an iPhone HEIC without the decoder, a
  .docx dropped in the folder, a 40-page scan, or a photo OCR finds nothing
  in must each come back as `unavailable` WITH A REASON the analyst can act
  on, never as a silent skip or a crash;
* API cache expiry -- a cached registry answer older than the client's
  cache_max_age_days is refetched; if the refetch fails the stale answer is
  served but counted so the pipeline can warn.
"""
import json
import sys
import time

import pytest
import requests

from vdd.extract import ocr
from vdd.extract.consistency import DocumentRead, check_consistency
from vdd.finoscale_api.client import FinoscaleClient


# ----------------------------------------------------------------- formats
def test_webp_and_heic_count_as_images():
    assert ocr._is_image("bill.webp")
    assert ocr._is_image("IMG_0042.HEIC")
    assert ocr._is_image("IMG_0042.heif")
    assert not ocr._is_image("bill.docx")


def test_unsupported_extension_reports_reason():
    r = ocr.extract_text("vendor_kyc.docx")
    assert r.method == "unavailable" and not r.confident
    assert "unsupported file type '.docx'" in r.reason


def test_encrypted_pdf_reports_reason(tmp_path):
    import pymupdf as fitz
    p = tmp_path / "aadhaar.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "GSTIN 27ABCDE1234F1Z5")
    doc.save(str(p), encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="1234", owner_pw="1234")
    doc.close()
    r = ocr.extract_text(str(p))
    assert r.method == "unavailable"
    assert "password-protected" in r.reason


def test_heic_without_decoder_reports_reason(tmp_path, monkeypatch):
    p = tmp_path / "IMG_0042.heic"
    p.write_bytes(b"\x00" * 16)
    monkeypatch.setitem(sys.modules, "pillow_heif", None)  # forces ImportError
    r = ocr.extract_text(str(p))
    assert r.method == "unavailable"
    assert "pillow-heif" in r.reason


def test_page_cap_is_stated_in_reason(monkeypatch):
    monkeypatch.setattr(ocr, "_pdf_text_layer", lambda path: None)
    monkeypatch.setattr(ocr, "_pdf_to_images", lambda path: [object()] * 9)
    seen = []

    def fake_ocr(im):
        seen.append(im)
        return ("Some readable text from this page of the scan", 5.0, 0)
    monkeypatch.setattr(ocr, "_easyocr_best_rotation", fake_ocr)
    r = ocr.extract_text("long_agreement.pdf")
    assert r.confident and r.method == "easyocr"
    assert len(seen) == ocr.OCR_MAX_PAGES
    assert f"first {ocr.OCR_MAX_PAGES} of 9 pages" in r.reason


def test_blank_ocr_reports_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(ocr, "_open_image", lambda path: object())
    monkeypatch.setattr(ocr, "_easyocr_best_rotation", lambda im: ("", 0.0, 0))
    r = ocr.extract_text(str(tmp_path / "dark_photo.jpg"))
    assert r.method == "unavailable"
    assert "too blurry or too dark" in r.reason and "too small in the frame" in r.reason


def test_rasterise_failure_reports_reason(monkeypatch):
    monkeypatch.setattr(ocr, "_pdf_text_layer", lambda path: None)

    def boom(path):
        raise RuntimeError("bad xref")
    monkeypatch.setattr(ocr, "_pdf_to_images", boom)
    r = ocr.extract_text("corrupt.pdf")
    assert r.method == "unavailable"
    assert "could not be rasterised" in r.reason and "RuntimeError" in r.reason


def test_reason_reaches_the_consistency_warning():
    gst = DocumentRead("gst.pdf", "gst_certificate", "pymupdf_text", True,
                       {"gstin": "27ABCDE1234F1Z5", "legal_name": "EXAMPLE STEELS", "constitution": "Proprietorship",
                        "address": "x", "date_of_registration": "01/01/2020"})
    locked = DocumentRead("aadhaar.pdf", "kyc_form", "unavailable", False, {},
                          reason="PDF is password-protected -- ask the vendor for an unlocked copy")
    capped = DocumentRead("agreement.pdf", "rental_agreement", "easyocr", True, {},
                          reason="only the first 6 of 9 pages were OCR'd")
    w = check_consistency([gst, locked, capped]).warnings
    assert any("aadhaar.pdf (kyc_form): could not be read -- PDF is password-protected" in x for x in w)
    assert any("agreement.pdf (rental_agreement): only the first 6 of 9 pages were OCR'd" in x for x in w)


# ------------------------------------------------------------- cache expiry
class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code, self.text = payload, status, json.dumps(payload)

    def json(self):
        return self._p


class _Session:
    def __init__(self, payload=None, exc=None):
        self.payload, self.exc, self.calls = payload, exc, 0

    def request(self, *a, **k):
        self.calls += 1
        if self.exc:
            raise self.exc
        return _Resp(self.payload)


def _client(tmp_path, session, **kw):
    return FinoscaleClient(api_key="k", base_url="https://x", cache_dir=str(tmp_path), session=session, **kw)


def _seed(client, age_days, data):
    path = client._cache_path("/api/t?None&None")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"_fetched_at": time.time() - age_days * 86400, "_path": "/api/t", "data": data}, f)
    return path


def test_fresh_cache_entry_is_served_without_a_call(tmp_path):
    s = _Session({"data": {"status": "live"}})
    c = _client(tmp_path, s)
    _seed(c, 1, {"data": {"status": "cached"}})
    assert c._cached("/api/t") == {"status": "cached"}
    assert s.calls == 0 and c.cache_stats["hits"] == 1


def test_stale_cache_entry_is_refetched(tmp_path):
    s = _Session({"data": {"status": "live"}})
    c = _client(tmp_path, s)
    path = _seed(c, 10, {"data": {"status": "cached"}})
    assert c._cached("/api/t") == {"status": "live"}
    assert s.calls == 1 and c.cache_stats["stale_refetched"] == 1
    with open(path, encoding="utf-8") as f:  # the file was rewritten with a fresh timestamp
        assert time.time() - json.load(f)["_fetched_at"] < 60


def test_zero_max_age_never_expires(tmp_path):
    s = _Session({"data": {"status": "live"}})
    c = _client(tmp_path, s, cache_max_age_days=0)
    _seed(c, 400, {"data": {"status": "cached"}})
    assert c._cached("/api/t") == {"status": "cached"}
    assert s.calls == 0


def test_stale_entry_served_when_network_fails(tmp_path):
    s = _Session(exc=requests.ConnectionError("no route"))
    c = _client(tmp_path, s)
    _seed(c, 10, {"data": {"status": "cached"}})
    assert c._cached("/api/t") == {"status": "cached"}
    assert c.cache_stats["stale_served_offline"] == 1


def test_no_cache_and_network_failure_raises(tmp_path):
    s = _Session(exc=requests.ConnectionError("no route"))
    c = _client(tmp_path, s)
    with pytest.raises(requests.ConnectionError):
        c._cached("/api/t")


# ------------------------------------------------- scans with a token text layer
def _pdf_with(tmp_path, name, text, image_cover):
    """A one-page PDF carrying `text` and, optionally, one image covering
    `image_cover` of the page (a stand-in for a scanned form)."""
    import pymupdf as fitz
    from PIL import Image
    import io
    p = tmp_path / name
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    if image_cover:
        buf = io.BytesIO()
        Image.new("RGB", (60, 80), "white").save(buf, format="PNG")
        w, h = 600 * image_cover ** 0.5, 800 * image_cover ** 0.5
        page.insert_image(fitz.Rect(0, 0, w, h), stream=buf.getvalue())
    if text:
        # one line per ~60 characters: get_text() only returns what lies on the page
        words, lines, cur = text.split(), [], ""
        for w in words:
            if len(cur) + len(w) > 60:
                lines.append(cur)
                cur = ""
            cur = (cur + " " + w).strip()
        lines.append(cur)
        for i, line in enumerate(lines[:60]):
            page.insert_text((40, 40 + 12 * i), line, fontsize=9)
    doc.save(str(p))
    doc.close()
    return str(p)


def test_signature_stamp_on_a_scan_is_not_a_text_layer(tmp_path):
    """Skandan's Factory License: 62 characters of e-signature stamp over a
    full-page image read as 'digital' and the licence was never OCR'd."""
    p = _pdf_with(tmp_path, "licence.pdf", "V S SARAVANAN DIGITALLY SIGNED 2026.04.28 16:12:30+05'30' V S SARAVANAN", 0.76)
    assert ocr._pdf_text_layer(p) is None
    p = _pdf_with(tmp_path, "digital.pdf", "GSTIN 27ABCDE1234F1Z5 " * 20, 0.0)
    assert ocr._pdf_text_layer(p) is not None
    # a letterhead image on a genuinely digital page is still a digital page
    p = _pdf_with(tmp_path, "letterhead.pdf", "Certificate of Registration " * 30, 0.6)
    assert ocr._pdf_text_layer(p) is not None


def test_image_dominated_pdf_is_detected_and_force_ocr_skips_the_text_layer(tmp_path, monkeypatch):
    p = _pdf_with(tmp_path, "kyc.pdf", "GSTIN * PAN Number MSME Registration Number " * 10, 1.0)
    assert ocr.pdf_image_dominated(p)
    assert not ocr.pdf_image_dominated(_pdf_with(tmp_path, "gst.pdf", "x " * 200, 0.01))
    monkeypatch.setattr(ocr, "_pdf_to_images", lambda path: [object()])
    monkeypatch.setattr(ocr, "_easyocr_best_rotation", lambda im: ("GSTIN 27ABCDE1234F1Z5 PAN ABCDE1234F", 5.0, 0))
    assert ocr.extract_text(p).method == "pymupdf_text"
    r = ocr.extract_text(p, force_ocr=True)
    assert r.method == "easyocr" and "27ABCDE1234F1Z5" in r.text


def test_pipeline_rereads_a_scanned_form_by_ocr_when_the_text_layer_is_only_labels(tmp_path, monkeypatch):
    from vdd import pipeline
    from vdd.extract.classify import ClassifiedDocs
    from vdd.extract.ocr import ExtractionResult
    p = _pdf_with(tmp_path, "KYC.pdf", "GSTIN * PAN Number", 1.0)
    calls = []

    def fake_extract(path, cache_dir=None, force_ocr=False):
        calls.append(force_ocr)
        if force_ocr:
            return ExtractionResult(text="GSTIN: 27ABCDE1234F1Z5 PAN: ABCDE1234F", method="easyocr", confident=True)
        return ExtractionResult(text="GSTIN * PAN Number", method="pymupdf_text", confident=True)
    monkeypatch.setattr(pipeline, "extract_text", fake_extract)
    reads = []
    entity = pipeline.extract_entity(ClassifiedDocs(by_type={"kyc_form": [p]}), [], reads_out=reads)
    assert calls == [False, True]
    (read,) = reads
    assert read.method == "pymupdf_text+easyocr" and read.quality == "read"
    assert "OCR'd from the page image" in read.reason
    assert entity.get("gstin") == "27ABCDE1234F1Z5"

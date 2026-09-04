"""
Text extraction for KYC documents.

Priority order (matches PROMPT.md's documented, working approach, minus the
Tesseract dependency which isn't installed on this machine -- see plan doc):
  1. Digital PDFs (selectable text layer) -> PyMuPDF, instant, no OCR needed.
  2. Images / scanned PDFs -> pytesseract, IF the Tesseract binary happens to
     be available (soft dependency, checked at call time).
  3. Otherwise -> Claude vision fallback (`extract_via_vision`), IF
     ANTHROPIC_API_KEY is set.
  4. Otherwise -> return method="unavailable" with empty text. Callers
     (resolvers) must treat this as "needs manual review", never guess.

Never silently fabricate text -- an empty/low-confidence result must be
visible in the pipeline's gap report, per the project's existing "flag,
don't pause" convention (Recykal VDD - Custom Instructions.md rule 4).
"""
import os
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class ExtractionResult:
    text: str
    method: str  # "pymupdf_text" | "tesseract" | "vision" | "manual_transcript" | "unavailable"
    confident: bool


def _is_pdf(path: str) -> bool:
    return path.lower().endswith(".pdf")


def _transcript_cache_path(path: str, cache_dir: Optional[str]) -> Optional[str]:
    if not cache_dir:
        return None
    safe = re.sub(r'[^A-Za-z0-9]+', '_', os.path.basename(path)).strip('_')
    return os.path.join(cache_dir, "vision_transcripts", safe + ".txt")


def _manual_transcript(path: str, cache_dir: Optional[str]) -> Optional[str]:
    """Pre-computed transcript for a document, written once (by a human or an
    agent with vision reading the image/PDF directly) and reused on every
    subsequent run -- avoids paying for a fresh vision API call every time,
    and lets a smoke test proceed even with no OCR/vision backend configured.
    Not a substitute for extract_via_vision in production -- just the same
    fallback tier, pre-computed and cached to disk instead of called live."""
    cache_path = _transcript_cache_path(path, cache_dir)
    if cache_path and os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            return f.read()
    return None


def _pdf_text_layer(path: str) -> Optional[str]:
    import pymupdf as fitz
    with fitz.open(path) as doc:
        parts = []
        digital_enough = True
        for page in doc:
            t = page.get_text()
            parts.append(t)
            if len(t.strip()) < 30:
                digital_enough = False
        text = "\n".join(parts)
        return text if digital_enough and text.strip() else None


def _pdf_to_images(path: str, dpi: int = 150):
    import pymupdf as fitz
    from PIL import Image
    import io
    with fitz.open(path) as doc:
        images = []
        for page in doc:
            pix = page.get_pixmap(dpi=dpi)
            images.append(Image.open(io.BytesIO(pix.tobytes("png"))))
        return images


def _tesseract_available() -> bool:
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def _ocr_image(img) -> str:
    import pytesseract
    from PIL import ImageOps
    gray = ImageOps.grayscale(img)
    bw = gray.point(lambda p: 255 if p > 150 else 0)
    return pytesseract.image_to_string(bw)


def _vision_on_bytes(image_bytes: bytes, media_type: str) -> Optional[str]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic
        import base64
        client = anthropic.Anthropic(api_key=api_key)
        data = base64.standard_b64encode(image_bytes).decode("utf-8")
        resp = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=2048,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}},
                    {"type": "text", "text": "Transcribe every piece of text visible in this document image "
                                              "verbatim, preserving layout/labels as best you can. Output only "
                                              "the transcription, no commentary."},
                ],
            }],
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    except Exception:
        return None


def _vision_on_pil_image(img) -> Optional[str]:
    import io
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return _vision_on_bytes(buf.getvalue(), "image/png")


def extract_via_vision(path: str) -> Optional[str]:
    """LLM-vision fallback for an image file OCR can't handle cleanly.
    Requires ANTHROPIC_API_KEY. Returns None if unavailable or the call fails
    -- callers must not treat None as 'document has no data'."""
    media_type = "image/jpeg" if path.lower().endswith((".jpg", ".jpeg")) else "image/png"
    with open(path, "rb") as f:
        return _vision_on_bytes(f.read(), media_type)


def extract_text(path: str, cache_dir: Optional[str] = None) -> ExtractionResult:
    manual = _manual_transcript(path, cache_dir)
    if manual:
        return ExtractionResult(text=manual, method="manual_transcript", confident=True)

    if _is_pdf(path):
        text = _pdf_text_layer(path)
        if text:
            return ExtractionResult(text=text, method="pymupdf_text", confident=True)
        # Scanned PDF -- fall through to image-based extraction on rendered pages.
        try:
            images = _pdf_to_images(path)
        except Exception:
            images = []
        if len(images) > 2:
            # PROMPT.md's documented rule: skip OCR beyond 2 pages, too slow --
            # fall straight to vision on page 1 only, if available.
            vtext = _vision_on_pil_image(images[0]) if images else None
            if vtext:
                return ExtractionResult(text=vtext, method="vision", confident=True)
            return ExtractionResult(text="", method="unavailable", confident=False)
        if images and _tesseract_available():
            text = "\n".join(_ocr_image(im) for im in images)
            if len(text.strip()) > 20:
                return ExtractionResult(text=text, method="tesseract", confident=True)
        vtext = _vision_on_pil_image(images[0]) if images else None
        if vtext:
            return ExtractionResult(text=vtext, method="vision", confident=True)
        return ExtractionResult(text="", method="unavailable", confident=False)

    # Image file (jpg/jpeg/png)
    if _tesseract_available():
        from PIL import Image
        text = _ocr_image(Image.open(path))
        if len(text.strip()) > 20:
            return ExtractionResult(text=text, method="tesseract", confident=True)
    vtext = extract_via_vision(path)
    if vtext:
        return ExtractionResult(text=vtext, method="vision", confident=True)
    return ExtractionResult(text="", method="unavailable", confident=False)

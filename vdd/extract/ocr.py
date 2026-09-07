"""
Text extraction for KYC documents.

Priority order:
  1. Digital PDFs (selectable text layer) -> PyMuPDF, instant, no OCR needed.
  2. Images / scanned PDFs -> pytesseract, IF the Tesseract binary happens to
     be available (soft dependency, checked at call time).
  3. Otherwise -> return method="unavailable" with empty text. Callers
     (resolvers) must treat this as "needs manual review", never guess.

No document image/bytes are ever sent to any LLM, full stop -- this
pipeline handles consented but highly sensitive personal financial/KYC
data. (An earlier version of this module had a Claude-vision OCR fallback
tier here; it was removed for this reason, not because it didn't work.)

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
    method: str  # "pymupdf_text" | "tesseract" | "manual_transcript" | "unavailable"
    confident: bool


def _is_pdf(path: str) -> bool:
    return path.lower().endswith(".pdf")


def _transcript_cache_path(path: str, cache_dir: Optional[str]) -> Optional[str]:
    if not cache_dir:
        return None
    safe = re.sub(r'[^A-Za-z0-9]+', '_', os.path.basename(path)).strip('_')
    return os.path.join(cache_dir, "vision_transcripts", safe + ".txt")


def _manual_transcript(path: str, cache_dir: Optional[str]) -> Optional[str]:
    """Pre-computed transcript for a document, written once by a human
    reading the image/PDF directly and reused on every subsequent run --
    lets a smoke test proceed even with no OCR backend configured. This is
    the only path by which a document's content can reach this pipeline
    without a working OCR engine -- it is never populated by an LLM call."""
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
        # PROMPT.md's documented rule: skip OCR beyond 2 pages, too slow.
        if len(images) <= 2 and images and _tesseract_available():
            text = "\n".join(_ocr_image(im) for im in images)
            if len(text.strip()) > 20:
                return ExtractionResult(text=text, method="tesseract", confident=True)
        return ExtractionResult(text="", method="unavailable", confident=False)

    # Image file (jpg/jpeg/png)
    if _tesseract_available():
        from PIL import Image
        text = _ocr_image(Image.open(path))
        if len(text.strip()) > 20:
            return ExtractionResult(text=text, method="tesseract", confident=True)
    return ExtractionResult(text="", method="unavailable", confident=False)

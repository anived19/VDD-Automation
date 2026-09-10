"""
Text extraction for KYC documents.

Priority order:
  1. Digital PDFs (selectable text layer) -> PyMuPDF, instant, no OCR needed.
  2. Images / scanned PDFs -> EasyOCR, a local neural OCR model that runs
     entirely on this machine's CPU. Tries all 4 rotations and keeps
     whichever one the model actually reads confidently, since real-world
     phone photos are frequently shot sideways with no EXIF orientation tag
     to correct for (confirmed on MVIKAS's cancelled cheque, 2026-09-07 --
     size 899x1599 but the cheque itself is rendered sideways within that
     frame, with `img.getexif()` carrying no orientation tag at all).
  3. A pre-written manual transcript cache, if one exists for this exact
     file -- last resort now, tried only after the local OCR engine has
     had a chance, not the primary path.
  4. Otherwise -> method="unavailable" with empty text. Callers
     (resolvers) must treat this as "needs manual review", never guess.

No document image/bytes are ever sent to any LLM or any third-party API,
full stop -- this pipeline handles consented but highly sensitive personal
financial/KYC data, and every OCR engine here runs locally, offline, at
inference time. (An earlier version of this module had a Claude-vision OCR
fallback tier; it was removed for exactly this reason. EasyOCR is not a
walk-back of that decision -- its model weights are a one-time, generic,
public download cached under ~/.EasyOCR, and no document content is ever
transmitted anywhere; per-document inference happens entirely on-device.)

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
    method: str  # "pymupdf_text" | "easyocr" | "tesseract" | "manual_transcript" | "unavailable"
    confident: bool
    # Degrees of clockwise rotation EasyOCR's brute-force pass found the document
    # actually needs to read upright (0 if unrotated, unattempted, or not from the
    # easyocr tier). Callers doing their own image analysis on this same file (e.g.
    # detect_diagonal_strike) must apply this first -- an angle computed against the
    # raw, un-rotated pixels is meaningless once the real content is sideways.
    rotation: int = 0


def _is_pdf(path: str) -> bool:
    return path.lower().endswith(".pdf")


def _is_image(path: str) -> bool:
    return path.lower().endswith((".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif"))


def _transcript_cache_path(path: str, cache_dir: Optional[str]) -> Optional[str]:
    if not cache_dir:
        return None
    safe = re.sub(r'[^A-Za-z0-9]+', '_', os.path.basename(path)).strip('_')
    return os.path.join(cache_dir, "vision_transcripts", safe + ".txt")


def _manual_transcript(path: str, cache_dir: Optional[str]) -> Optional[str]:
    """Pre-computed transcript for a document, written once by a human
    reading the image/PDF directly -- last-resort path now, tried only
    after both local OCR engines below have had a chance. Never populated
    by an LLM call; a human transcribing what they personally see is not
    the same thing as sending the document to one."""
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
            # len(t.strip()) only trims the ends -- a page that's a mostly-blank
            # template with the real content rendered as a scanned/stamped image
            # (confirmed on Skandan's Factory License + PCB Air/Water certs,
            # 2026-09-07: each "extracted" under ~100 chars, almost all of it
            # interior whitespace runs from the template's layout) can still
            # clear a raw-length threshold on whitespace alone. Count actual
            # non-whitespace characters instead, so this doesn't silently accept
            # a page whose substantive content was never really extracted.
            if len(re.sub(r'\s+', '', t)) < 30:
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


# ---------------------------------------------------------------- EasyOCR (local, primary)
_easyocr_reader = None
_easyocr_init_failed = False


def _get_easyocr_reader():
    """Lazily initialize one shared EasyOCR reader per process -- model load
    takes a few seconds, not worth repeating per document. gpu=False since
    this is a CPU dev machine; correctness matters far more than speed here."""
    global _easyocr_reader, _easyocr_init_failed
    if _easyocr_init_failed:
        return None
    if _easyocr_reader is None:
        try:
            import easyocr
            _easyocr_reader = easyocr.Reader(["en", "hi", "mr"], gpu=False, verbose=False)
        except Exception:
            _easyocr_init_failed = True
            return None
    return _easyocr_reader


def _easyocr_best_rotation(img) -> tuple[str, float, int]:
    """Try all 4 orientations and keep whichever one the model reads
    confidently, rather than trusting EXIF (frequently absent -- see module
    docstring) or filename hints (frequently useless). Scored by total
    characters recognized at >=0.4 confidence: in practice a wrong
    orientation reliably scores near zero, not a partial/ambiguous amount,
    so this cleanly picks out the real orientation without needing a
    separate rotation-detection model. Returns (text, score, angle) -- the
    angle is needed by any other image-level check on this same file (see
    detect_diagonal_strike), not just for text extraction."""
    reader = _get_easyocr_reader()
    if reader is None:
        return "", 0.0, 0
    import numpy as np
    from PIL import ImageOps
    img = ImageOps.exif_transpose(img).convert("RGB")
    best_text, best_score, best_angle = "", 0.0, 0
    for angle in (0, 90, 180, 270):
        rotated = img.rotate(angle, expand=True) if angle else img
        results = reader.readtext(np.array(rotated), detail=1)
        parts = [t for (_bbox, t, conf) in results if conf >= 0.4]
        score = sum(len(t) for t in parts)
        if score > best_score:
            best_score, best_text, best_angle = score, "\n".join(parts), angle
    return best_text, best_score, best_angle


# ---------------------------------------------------------------- Visual cancellation-mark check
def detect_diagonal_strike(path: str, rotation: int = 0) -> Optional[bool]:
    """Independent, non-OCR corroborating signal for a cancelled cheque's
    diagonal ink strike-through. Reading the handwritten word itself
    (typically cursive) is a handwriting-recognition problem no OCR engine
    here promises to solve reliably -- but "is there a long diagonal line
    drawn across this image" is a well-posed classical computer-vision
    question (Canny edge detection + probabilistic Hough transform),
    independent of what any handwriting says or whether OCR found any text
    at all. Returns None (not a verdict, not a guess) if OpenCV isn't
    installed or the file isn't a plain image -- callers must not treat
    None as "no mark found".

    `rotation` MUST be the same angle ExtractionResult.rotation reported for
    this exact file (0 if unknown) -- "diagonal" is only meaningful relative
    to the document's own upright orientation. Confirmed the hard way on
    MVIKAS's cancelled cheque (2026-09-07): the raw photo is ~90 degrees off
    upright, so a real diagonal strike on the cheque itself showed up as
    near-horizontal/near-vertical in raw pixel coordinates and was missed
    entirely before this parameter existed."""
    if not _is_image(path):
        return None
    try:
        import cv2
        import numpy as np
        from PIL import Image
    except Exception:
        return None
    img = Image.open(path).convert("L")
    if rotation:
        img = img.rotate(rotation, expand=True)
    arr = np.array(img)
    h, w = arr.shape[:2]
    edges = cv2.Canny(arr, 50, 150)
    # A real hand-drawn strike is rarely one unbroken line once Canny/Hough see it in
    # a noisy phone photo -- score by total diagonal-line length found, not just
    # "does any single segment clear a high bar", and keep the length/vote thresholds
    # loose enough for a somewhat broken line to still add up.
    min_len = 0.15 * (h ** 2 + w ** 2) ** 0.5
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=40,
                             minLineLength=min_len, maxLineGap=30)
    if lines is None:
        return False
    diagonal_total = 0.0
    for line in lines:
        # cv2.HoughLinesP's result shape has varied across OpenCV versions
        # ((N,1,4) historically; this installed build (5.0.0.93) returns
        # something that doesn't unpack the same way) -- flatten defensively
        # instead of assuming a specific shape.
        x1, y1, x2, y2 = np.asarray(line).reshape(-1)[:4]
        angle = abs(np.degrees(np.arctan2(y2 - y1, x2 - x1)))
        # A deliberate strike-through is neither near-horizontal nor near-vertical
        # (those are just the cheque's own printed rule lines/borders).
        if 20 <= angle <= 70 or 110 <= angle <= 160:
            diagonal_total += ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    return diagonal_total >= 0.25 * (h ** 2 + w ** 2) ** 0.5


def extract_text(path: str, cache_dir: Optional[str] = None) -> ExtractionResult:
    if _is_pdf(path):
        text = _pdf_text_layer(path)
        if text:
            return ExtractionResult(text=text, method="pymupdf_text", confident=True)
        try:
            images = _pdf_to_images(path)
        except Exception:
            images = []
        # PROMPT.md's documented rule: OCR at most 2 pages, too slow beyond that --
        # but confirmed on Skandan's 6-7 page PCB Air/Water consent certs
        # (2026-09-07) that the substantive content (applicant, premises address)
        # is on page 1, with the rest being boilerplate terms/annexures. Cap to
        # the first 2 pages rather than skipping the whole document just because
        # it has more pages than that.
        images = images[:2]
    elif _is_image(path):
        from PIL import Image
        images = [Image.open(path)]
    else:
        images = []

    if images:
        # One page in the overwhelming common case (a single photographed image);
        # for the rare multi-page scanned PDF, each page keeps its own detected
        # rotation, but only the single-image path's angle is ever consumed
        # downstream (detect_diagonal_strike operates on one image file, not a PDF).
        per_page = [_easyocr_best_rotation(im) for im in images]
        easy_text = "\n".join(t for (t, _s, _a) in per_page if t)
        easy_score = sum(s for (_t, s, _a) in per_page)
        best_angle = max(per_page, key=lambda r: r[1])[2] if per_page else 0
        if easy_score > 0 and len(easy_text.strip()) > 20:
            return ExtractionResult(text=easy_text, method="easyocr", confident=True, rotation=best_angle)

    manual = _manual_transcript(path, cache_dir)
    if manual:
        return ExtractionResult(text=manual, method="manual_transcript", confident=True)

    return ExtractionResult(text="", method="unavailable", confident=False)

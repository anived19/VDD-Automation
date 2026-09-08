"""
Minimal local demo server for the VDD pipeline.

Serves one static page (index.html) with a folder picker, and one JSON API
(`POST /api/run`) that saves the uploaded vendor folder to webapp/uploads/,
runs the exact same `vdd.pipeline.run_vendor()` the CLI uses (same
extraction, same live Finoscale API calls, same LangGraph review loop), and
returns a summary the frontend renders -- plus a link to the real generated
HTML report.

Not a production server -- Flask's dev server, for a client demo only.
"""
import os
import sys
import traceback
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE)
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from flask import Flask, request, jsonify, send_from_directory, Response

from vdd.finoscale_api.client import FinoscaleClient
from vdd.pipeline import run_vendor

UPLOADS = os.path.join(BASE, "uploads")
OUT_DIR = os.path.join(PROJECT_ROOT, "out")
CACHE_DIR = os.path.join(PROJECT_ROOT, "cache")
os.makedirs(UPLOADS, exist_ok=True)

app = Flask(__name__, static_folder=None)


@app.route("/")
def index():
    with open(os.path.join(BASE, "index.html"), encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/html")


def _build_client():
    api_key = os.environ.get("FINOSCALE_API_KEY")
    if not api_key:
        return None
    base_url = os.environ.get("FINOSCALE_API_BASE", "https://api-ppe.finoscale.ai")
    return FinoscaleClient(api_key=api_key, base_url=base_url, cache_dir=CACHE_DIR)


def _verification_rows(docs, extraction_warnings):
    """Per-uploaded-file classification status, for the 'was this file read
    correctly' panel. MUST use the real run's final ClassifiedDocs (result.docs)
    -- a fresh classify_folder() call only does filename matching and would
    wrongly show a file as unrecognized if it was actually only caught by the
    content-based fallback in extract_entity() (confirmed bug, 2026-09-08)."""
    warned = {}
    for w in extraction_warnings:
        # warnings are free-text like "<filename>: ..." or "<filename> (<doctype>): ..."
        head = w.split(":", 1)[0]
        base = head.split(" (")[0].strip()
        warned[base] = w

    rows = []
    for doc_type, paths in docs.by_type.items():
        for p in paths:
            base = os.path.basename(p)
            if base in warned:
                rows.append({"file": base, "doc_type": doc_type, "status": "warning", "detail": warned[base]})
            else:
                rows.append({"file": base, "doc_type": doc_type, "status": "ok", "detail": None})
    for p in docs.unmatched:
        base = os.path.basename(p)
        rows.append({"file": base, "doc_type": None, "status": "unrecognized",
                     "detail": warned.get(base, "Filename and content didn't match any known document type.")})
    rows.sort(key=lambda r: r["file"].lower())
    return rows


@app.route("/api/run", methods=["POST"])
def api_run():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files were uploaded."}), 400

    first_rel = files[0].filename.replace("\\", "/")
    vendor_name = first_rel.split("/")[0] if "/" in first_rel else os.path.splitext(first_rel)[0]
    vendor_name = "".join(c if c.isalnum() or c in "_- " else "_" for c in vendor_name).strip() or "VENDOR"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    vendor_dir = os.path.join(UPLOADS, f"{ts}_{vendor_name}")
    os.makedirs(vendor_dir, exist_ok=True)

    saved = 0
    for f in files:
        rel = (f.filename or "").replace("\\", "/")
        base = rel.split("/")[-1]
        if not base:
            continue
        f.save(os.path.join(vendor_dir, base))
        saved += 1
    if saved == 0:
        return jsonify({"error": "No usable files in the upload."}), 400

    client = _build_client()
    try:
        result = run_vendor(vendor_dir, OUT_DIR, client=client,
                             scoring_model_path=os.path.join(PROJECT_ROOT, "config", "scoring_model.json"))
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

    return jsonify({
        "vendor_name": result.vendor_name,
        "score": result.score,
        "html_report_url": (f"/report/{os.path.basename(result.html_path)}" if result.html_path else None),
        "pdf_report_url": (f"/report/{os.path.basename(result.pdf_path)}" if result.pdf_path else None),
        "missing_documents": result.missing_documents,
        "unresolved_fields": result.unresolved_fields,
        "cross_check_items": result.cross_check_items,
        "api_errors": result.api_errors,
        "extraction_warnings": result.extraction_warnings,
        "verification": _verification_rows(result.docs, result.extraction_warnings),
        "review": {
            "reviewed": result.reviewed,
            "approved": result.approved,
            "iterations": result.review_iterations,
            "corrections_applied": result.corrections_applied,
            "escalations_for_human": result.escalations_for_human,
            "review_error": result.review_error,
        },
        "finoscale_api_connected": client is not None,
    })


@app.route("/report/<path:filename>")
def report(filename):
    return send_from_directory(OUT_DIR, filename)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5055, debug=False)

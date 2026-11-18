"""Turn an invoice PDF into structured fields.

Three layers: (1) read the PDF text layer, or OCR it when it is a scan; (2) parse with deterministic
rules; (3) optionally ask Gemini to read the PDF directly. Every field carries a confidence so later
stages and the UI can tell a solid read from a shaky one.
"""
import base64
import hashlib
import json
import re
import time

import httpx
import pdfplumber
import pypdfium2 as pdfium
import pytesseract

from . import config
from .util import money, norm_po, parse_date

FIELDS = ["vendor_name", "vendor_gstin", "buyer_gstin", "invoice_number", "invoice_date", "po_reference",
          "currency", "subtotal", "tax_amount", "tax_type", "total", "bank_name", "bank_account", "bank_ifsc"]

GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]Z[A-Z\d]\b")
IFSC_RE = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")
LINE_RE = re.compile(r"^(?P<desc>.+?)\s+(?P<hsn>\d{4,8})\s+(?P<qty>\d+(?:\.\d+)?)\s+"
                     r"(?P<rate>[\d,]+\.\d{2})\s+(?P<amt>[\d,]+\.\d{2})\s*$")


class ExtractionError(Exception):
    pass


def inspect_pdf(path):
    """Cheap facts about the file, used by the intake stage."""
    data = open(path, "rb").read()
    with pdfplumber.open(path) as pdf:
        pages = len(pdf.pages)
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "pages": pages,
            "text_chars": len(text.strip()), "scanned": len(text.strip()) < 80, "text": text}


def ocr_pdf(path):
    """OCR every page. Returns (text, mean word confidence 0..1)."""
    pdf = pdfium.PdfDocument(path)
    chunks, confs = [], []
    for i in range(len(pdf)):
        img = pdf[i].render(scale=2.2).to_pil().convert("L")
        d = pytesseract.image_to_data(img, config="--psm 6", output_type=pytesseract.Output.DICT)
        words = {}
        for txt, conf, blk, par, ln in zip(d["text"], d["conf"], d["block_num"], d["par_num"], d["line_num"]):
            if txt.strip():
                words.setdefault((blk, par, ln), []).append(txt)
                if float(conf) >= 0:
                    confs.append(float(conf))
        chunks.append("\n".join(" ".join(w) for _, w in sorted(words.items())))
    return "\n".join(chunks), (sum(confs) / len(confs) / 100 if confs else 0.0)


def _field(value, conf):
    return {"value": value, "confidence": round(conf, 2) if value not in (None, "") else 0.0}


def parse_text(text, base_conf):
    """Deterministic parser for GST tax invoices. base_conf is 0.98 for a text layer, OCR confidence otherwise."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    f = {k: _field(None, 0) for k in FIELDS}
    c = base_conf

    if lines:
        f["vendor_name"] = _field(lines[0], c * 0.95)

    for l in lines:
        m = re.match(r"^(?:tax\s+)?invoice\s*(?:no\.?|number|#)\s*[:\-]?\s*(\S+)", l, re.I)
        if m and not f["invoice_number"]["value"]:
            f["invoice_number"] = _field(m.group(1).strip(".,;"), c)
        m = re.match(r"^(?:invoice\s*date|dated|date)\s*[:\-]?\s*(.+)$", l, re.I)
        if m and not f["invoice_date"]["value"]:
            f["invoice_date"] = _field(parse_date(m.group(1)), c)

    m = re.search(r"(?:PO\s*(?:number|no\.?|ref\.?)|purchase\s*order(?:\s*(?:no\.?|number|ref\.?))?)\s*[:\-]?\s*"
                  r"(PO[-\s]?\d{4}[-\s]?\d{3,5})", text, re.I) or re.search(r"\b(PO-\d{4}-\d{3,5})\b", text)
    if m:
        f["po_reference"] = _field(norm_po(m.group(1)), c)

    split = re.search(r"^\s*bill\s*to", text, re.I | re.M)
    head, tail = (text[:split.start()], text[split.start():]) if split else (text, "")
    g = GSTIN_RE.findall(head)
    if g:
        f["vendor_gstin"] = _field(g[0], c)
    g = GSTIN_RE.findall(tail)
    if g:
        f["buyer_gstin"] = _field(g[0], c)

    if re.search(r"\bINR\b|\bRs\.?|₹", text):
        f["currency"] = _field("INR", c)

    tax_parts, tax_kinds = [], set()
    for l in lines:
        m = re.match(r"^(sub\s*total|taxable\s*value|net\s*amount)\b[^\d]*([\d,]+\.\d{2})$", l, re.I)
        if m:
            f["subtotal"] = _field(money(m.group(2)), c)
            continue
        m = re.match(r"^(cgst|sgst|igst)\b.*?([\d,]+\.\d{2})$", l, re.I)
        if m:
            tax_parts.append(money(m.group(2)))
            tax_kinds.add(m.group(1).upper())
            continue
        m = re.match(r"^(grand\s*total|invoice\s*total|total\s*amount|total\s*payable|total)\b[^\d]*([\d,]+\.\d{2})$",
                     l, re.I)
        if m:
            f["total"] = _field(money(m.group(2)), c)
    if tax_parts:
        f["tax_amount"] = _field(round(sum(tax_parts), 2), c)
        f["tax_type"] = _field("IGST" if "IGST" in tax_kinds else "CGST+SGST", c)

    m = re.search(r"^bank\s*[:\-]\s*(.+)$", text, re.I | re.M)
    if m:
        f["bank_name"] = _field(m.group(1).strip(), c)
    m = re.search(r"^a\s*[/\\v]?\s*c\.?\s*(?:no\.?|number)?\s*[:\-]?\s*(\d{6,18})", text, re.I | re.M)
    if m:
        f["bank_account"] = _field(m.group(1), c)
    m = IFSC_RE.search(text)
    if m:
        f["bank_ifsc"] = _field(m.group(0), c)

    items = []
    for l in lines:
        m = LINE_RE.match(l)
        if m:
            items.append({"description": m["desc"].strip(), "hsn": m["hsn"], "qty": float(m["qty"]),
                          "rate": money(m["rate"]), "amount": money(m["amt"])})
    return f, items


def _gemini(path, model):
    """Ask one Gemini model to read the PDF. Raises ExtractionError on any problem so the caller can fall back."""
    if not config.GEMINI_API_KEY:
        raise ExtractionError("GEMINI_API_KEY is not set")
    prompt = (
        "You are extracting data from an Indian GST tax invoice. Return JSON only, with exactly these keys: "
        + ", ".join(FIELDS) + ", line_items, confidence. Rules: use null for anything not printed on the invoice "
        "(never guess); dates as YYYY-MM-DD; money as plain numbers with no commas; tax_type is 'IGST' or "
        "'CGST+SGST'; tax_amount is the sum of all tax lines; po_reference as printed. line_items is a list of "
        "{description, hsn, qty, rate, amount}. confidence is an object giving a 0-1 score for each of the other "
        "keys, lower when the print is unclear.")
    body = {"contents": [{"parts": [{"text": prompt}, {"inline_data": {
        "mime_type": "application/pdf", "data": base64.b64encode(open(path, "rb").read()).decode()}}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
    if model.startswith("gemini-3"):  # thinking levels exist on the 3.x models only
        body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": config.GEMINI_THINKING}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    last = None
    for attempt in range(2):
        try:
            r = httpx.post(url, headers={"x-goog-api-key": config.GEMINI_API_KEY}, json=body, timeout=config.GEMINI_TIMEOUT)
            if r.status_code in (429, 500, 503):
                last = f"HTTP {r.status_code}"
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(raw)
        except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError) as e:
            last = f"{type(e).__name__}: {str(e)[:120]}"
            break
    raise ExtractionError(f"{model} failed ({last})")


def _from_gemini(js):
    conf = js.get("confidence") or {}
    f = {}
    for k in FIELDS:
        v = js.get(k)
        if k in ("subtotal", "tax_amount", "total") and v is not None:
            v = money(v)
        if k == "po_reference" and v:
            v = norm_po(v) or v
        f[k] = _field(v, min(float(conf.get(k, 0.8) or 0), 0.95))
    items = []
    for it in js.get("line_items") or []:
        items.append({"description": str(it.get("description") or ""), "hsn": str(it.get("hsn") or ""),
                      "qty": money(it.get("qty")), "rate": money(it.get("rate")), "amount": money(it.get("amount"))})
    return f, items


def extract(path, info):
    """Main entry. info comes from inspect_pdf. Returns dict with fields, line_items, method, notes."""
    notes, scanned = [], info["scanned"]
    text, base_conf = info["text"], 0.98
    if scanned:
        text, base_conf = ocr_pdf(path)
        notes.append(f"Scanned image: read by OCR (average confidence {base_conf:.0%})")
    mode = config.EXTRACTOR
    if mode in ("auto", "llm") and config.GEMINI_API_KEY:
        started = time.time()
        for model in config.GEMINI_MODELS:
            if time.time() - started > config.GEMINI_BUDGET:
                notes.append("Gemini time budget used up; fell back to rule-based extraction")
                break
            try:
                fields, items = _from_gemini(_gemini(path, model))
                return {"fields": fields, "line_items": items, "method": f"gemini ({model})",
                        "notes": notes, "scanned": scanned, "text": text}
            except ExtractionError as e:
                notes.append(f"{e}; trying the next option")
        else:
            notes.append("All Gemini models failed; fell back to rule-based extraction")
    elif mode == "llm":
        notes.append("EXTRACTOR=llm but no GEMINI_API_KEY; used rule-based extraction")
    fields, items = parse_text(text, base_conf)
    return {"fields": fields, "line_items": items, "method": "rules" + (" + OCR" if scanned else ""),
            "notes": notes, "scanned": scanned, "text": text}

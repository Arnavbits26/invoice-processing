"""Generate synthetic vendor master, purchase orders, ledger seed and test invoice PDFs.

Run from the project root:  python scripts/make_data.py
Everything is deterministic, so the demo is repeatable.
"""
import io
import json
import random
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageFilter
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent.parent
DATA, SAMPLES = ROOT / "data", ROOT / "samples"
DATA.mkdir(exist_ok=True)
SAMPLES.mkdir(exist_ok=True)

BUYER = {"name": "Meridian Retail Pvt Ltd", "addr": "14 Residency Road, Bengaluru 560025",
         "gstin": "29AABCM5678K1Z2"}

VENDORS = [
    {"id": "V001", "name": "Sharma Office Supplies Pvt Ltd", "addr": "22 Commercial Street, Bengaluru 560001",
     "gstin": "29AAACS4410F1ZP", "bank": "HDFC Bank", "account": "50200012345678", "ifsc": "HDFC0000123"},
    {"id": "V002", "name": "Bharat IT Services LLP", "addr": "Hinjewadi Phase 2, Pune 411057",
     "gstin": "27AAEFB7712Q1Z3", "bank": "ICICI Bank", "account": "000405012233", "ifsc": "ICIC0000004"},
    {"id": "V003", "name": "Greenfield Logistics Pvt Ltd", "addr": "Guindy Industrial Estate, Chennai 600032",
     "gstin": "33AACCG9081L1ZK", "bank": "State Bank of India", "account": "32145098761", "ifsc": "SBIN0001234"},
    {"id": "V004", "name": "Apex Facility Management Pvt Ltd", "addr": "HSR Layout, Bengaluru 560102",
     "gstin": "29AAFCA2210D1Z8", "bank": "Axis Bank", "account": "917020045678123", "ifsc": "UTIB0000987"},
    {"id": "V005", "name": "Nimbus Cloud Solutions Pvt Ltd", "addr": "HITEC City, Hyderabad 500081",
     "gstin": "36AAHCN6654M1Z1", "bank": "Kotak Mahindra Bank", "account": "8812004455", "ifsc": "KKBK0000458"},
    {"id": "V006", "name": "Kaveri Printing Works", "addr": "Devaraja Mohalla, Mysuru 570001",
     "gstin": "29AAAFK1189H1Z6", "bank": "Canara Bank", "account": "1100010233445", "ifsc": "CNRB0000210"},
]
VBY = {v["id"]: v for v in VENDORS}

POS = [
    {"po_number": "PO-2026-1001", "vendor_id": "V001", "date": "2026-10-20", "gst_rate": 18, "lines": [
        {"desc": "A4 copier paper 75 GSM (ream)", "qty": 500, "rate": 285},
        {"desc": "Ball pens blue (box of 50)", "qty": 200, "rate": 120},
        {"desc": "Heavy-duty stapler", "qty": 40, "rate": 410}]},
    {"po_number": "PO-2026-1002", "vendor_id": "V002", "date": "2026-10-01", "gst_rate": 18, "lines": [
        {"desc": "Managed IT support (monthly)", "qty": 3, "rate": 85000}]},
    {"po_number": "PO-2026-1003", "vendor_id": "V003", "date": "2026-10-05", "gst_rate": 18, "lines": [
        {"desc": "Freight trips (Q4 2026)", "qty": 100, "rate": 5000}]},
    {"po_number": "PO-2026-1004", "vendor_id": "V004", "date": "2026-10-28", "gst_rate": 18, "lines": [
        {"desc": "Facility management services (monthly)", "qty": 1, "rate": 180000}]},
    {"po_number": "PO-2026-1005", "vendor_id": "V005", "date": "2026-10-30", "gst_rate": 18, "lines": [
        {"desc": "Cloud hosting - monthly plan", "qty": 12, "rate": 45000}]},
    {"po_number": "PO-2026-1006", "vendor_id": "V006", "date": "2026-10-25", "gst_rate": 12, "lines": [
        {"desc": "Brochure A4 tri-fold 130gsm", "qty": 5000, "rate": 38}]},
    {"po_number": "PO-2026-1007", "vendor_id": "V001", "date": "2026-10-15", "gst_rate": 18, "lines": [
        {"desc": "HP 26A toner cartridge", "qty": 60, "rate": 2150}]},
]
LEDGER_SEED = [
    {"invoice_no": "SOS/2026/0417", "vendor_id": "V001", "amount": 152220.0, "date": "2026-10-28",
     "po_number": "PO-2026-1007", "decision": "APPROVE"}]

ZENITH = {"id": None, "name": "Zenith Marketing Co", "addr": "Indiranagar, Bengaluru 560038",
          "gstin": "29AAAFZ9988P1Z9", "bank": "HDFC Bank", "account": "50100777888999", "ifsc": "HDFC0000777"}

# file, vendor, invoice_no, date, po_ref, lines (desc, hsn, qty, rate), gst, layout, scanned, extras
INVOICES = [
    dict(file="01_clean_sharma_office.pdf", vendor=VBY["V001"], no="SOS/2026/0431", date="10-Nov-2026",
         po="PO-2026-1001", gst=18, layout="classic", scanned=False, lines=[
             ("A4 copier paper 75 GSM (ream)", "4802", 500, 285), ("Ball pens blue (box of 50)", "9608", 200, 120),
             ("Heavy-duty stapler", "8305", 40, 410)],
         expect=("APPROVE", "Clean invoice, exact PO match")),
    dict(file="02_it_services_within_tolerance.pdf", vendor=VBY["V002"], no="BIT-INV-2611-014", date="12-Nov-2026",
         po="PO-2026-1002", gst=18, layout="compact", scanned=False, lines=[
             ("Managed IT support (monthly)", "998313", 3, 85000), ("Additional on-site visit", "998313", 1, 3050)],
         expect=("APPROVE", "+1.2% over PO, inside the 2% tolerance")),
    dict(file="03_freight_part1.pdf", vendor=VBY["V003"], no="GLL/11/0455", date="11-Nov-2026",
         po="PO-2026-1003", gst=18, layout="classic", scanned=False, lines=[
             ("Freight trips - Nov 2026 (part 1)", "996511", 50, 5000)],
         expect=("APPROVE", "First half of a split PO; run before file 04")),
    dict(file="04_freight_part2_over_balance.pdf", vendor=VBY["V003"], no="GLL/11/0472", date="16-Nov-2026",
         po="PO-2026-1003", gst=18, layout="classic", scanned=False, lines=[
             ("Freight trips - Dec 2026 (part 2)", "996511", 52, 5000)],
         expect=("HOLD", "Second split invoice exceeds the remaining PO balance")),
    dict(file="05_duplicate_scanned.pdf", vendor=VBY["V001"], no="SOS-2026-0417", date="12-Nov-2026",
         po="PO-2026-1007", gst=18, layout="classic", scanned=True, lines=[
             ("HP 26A toner cartridge", "844332", 60, 2150)],
         expect=("REJECT", "Scanned re-send of an invoice already paid, with a re-formatted number")),
    dict(file="06_scanned_no_po_reference.pdf", vendor=VBY["V005"], no="NCS/IN/26-27/1182", date="09-Nov-2026",
         po=None, gst=18, layout="compact", scanned=True, lines=[
             ("Cloud hosting - annual plan", "998315", 12, 45000)],
         expect=("APPROVE_WITH_FLAG", "Scanned, no PO number: PO inferred from vendor and amount")),
    dict(file="07_missing_number_and_total.pdf", vendor=VBY["V004"], no=None, date="14-Nov-2026",
         po="PO-2026-1004", gst=18, layout="classic", scanned=False, omit_total=True, lines=[
             ("Facility management - Nov 2026", "998533", 1, 180000)],
         expect=("REQUEST_INFO", "No invoice number and no total: ask the vendor")),
    dict(file="08_bank_details_changed.pdf", vendor=VBY["V006"], no="KPW/2611/77", date="13-Nov-2026",
         po="PO-2026-1006", gst=12, layout="compact", scanned=False,
         bank=("HDFC Bank", "50100455667788", "HDFC0000456"), lines=[
             ("Brochure A4 tri-fold 130gsm", "4911", 5000, 38)],
         expect=("HOLD", "Bank account differs from the vendor master: payment-fraud signal")),
    dict(file="09_unapproved_vendor.pdf", vendor=ZENITH, no="ZM/221", date="14-Nov-2026",
         po="PO-2026-1099", gst=18, layout="classic", scanned=False, lines=[
             ("Social media campaign - Nov", "998361", 1, 50000)],
         expect=("REJECT", "Vendor is not on the approved list and the PO does not exist")),
]


def inr(x):
    """Indian digit grouping, two decimals: 1234567.5 -> 12,34,567.50"""
    s = f"{x:.2f}"
    whole, frac = s.split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return f"{whole}.{frac}"


def totals(spec):
    sub = round(sum(q * r for _, _, q, r in spec["lines"]), 2)
    tax = round(sub * spec["gst"] / 100, 2)
    intra = spec["vendor"]["gstin"][:2] == BUYER["gstin"][:2]
    return sub, tax, intra


def draw_invoice(spec, path):
    v = spec["vendor"]
    c = canvas.Canvas(str(path), pagesize=A4)
    W, H = A4
    sub, tax, intra = totals(spec)
    total = round(sub + tax, 2)
    bank = spec.get("bank") or (v["bank"], v["account"], v["ifsc"])
    classic = spec["layout"] == "classic"
    y = H - 60
    if classic:
        c.setFont("Helvetica-Bold", 16); c.drawString(50, y, v["name"]); y -= 16
        c.setFont("Helvetica", 9); c.drawString(50, y, v["addr"]); y -= 12
        c.drawString(50, y, f"GSTIN: {v['gstin']}"); y -= 26
        c.setFont("Helvetica-Bold", 13); c.drawString(50, y, "TAX INVOICE"); y -= 20
        c.setFont("Helvetica", 10)
        if spec["no"]:
            c.drawString(50, y, f"Invoice No: {spec['no']}"); y -= 14
        c.drawString(50, y, f"Invoice Date: {spec['date']}"); y -= 14
        if spec["po"]:
            c.drawString(50, y, f"PO Number: {spec['po']}"); y -= 14
    else:
        c.setFont("Helvetica-Bold", 15); c.drawCentredString(W / 2, y, v["name"]); y -= 14
        c.setFont("Helvetica", 9); c.drawCentredString(W / 2, y, v["addr"]); y -= 12
        c.drawCentredString(W / 2, y, f"GSTIN : {v['gstin']}"); y -= 26
        c.setFont("Helvetica-Bold", 12); c.drawString(50, y, "Tax Invoice"); y -= 18
        c.setFont("Helvetica", 10)
        if spec["no"]:
            c.drawString(50, y, f"Invoice No.: {spec['no']}"); y -= 14
        c.drawString(50, y, f"Dated: {spec['date']}"); y -= 14
        if spec["po"]:
            c.drawString(50, y, f"Ref. PO No.: {spec['po']}"); y -= 14
    y -= 8
    c.setFont("Helvetica-Bold", 10); c.drawString(50, y, "Bill To:"); y -= 14
    c.setFont("Helvetica", 10)
    c.drawString(50, y, BUYER["name"]); y -= 13
    c.drawString(50, y, BUYER["addr"]); y -= 13
    c.drawString(50, y, f"GSTIN: {BUYER['gstin']}"); y -= 30
    # table header
    c.setFont("Helvetica-Bold", 9)
    for x, t in ((50, "Description"), (270, "HSN/SAC"), (340, "Qty"), (400, "Rate"), (490, "Amount")):
        c.drawString(x, y, t)
    y -= 6; c.line(50, y, 545, y); y -= 14
    c.setFont("Helvetica", 9)
    for desc, hsn, qty, rate in spec["lines"]:
        c.drawString(50, y, desc); c.drawString(270, y, hsn); c.drawString(340, y, str(qty))
        c.drawString(400, y, inr(rate)); c.drawString(490, y, inr(qty * rate)); y -= 14
    y -= 4; c.line(50, y, 545, y); y -= 18
    c.setFont("Helvetica", 10)
    sub_label = "Sub Total" if classic else "Taxable Value"
    rows = [(sub_label, sub)]
    h = spec["gst"] / 2
    rows += [(f"CGST @ {h:g}%", round(tax / 2, 2)), (f"SGST @ {h:g}%", round(tax / 2, 2))] if intra \
        else [(f"IGST @ {spec['gst']}%", tax)]
    if not spec.get("omit_total"):
        rows.append(("Grand Total" if classic else "Invoice Total", total))
    for label, val in rows:
        c.drawString(330, y, label); c.drawRightString(545, y, inr(val)); y -= 14
    if not spec.get("omit_total"):
        y -= 4; c.setFont("Helvetica", 9); c.drawString(50, y, "Currency: INR")
    y -= 30
    c.setFont("Helvetica-Bold", 9); c.drawString(50, y, "Bank Details"); y -= 13
    c.setFont("Helvetica", 9)
    c.drawString(50, y, f"Bank: {bank[0]}"); y -= 12
    c.drawString(50, y, f"A/C No: {bank[1]}"); y -= 12
    c.drawString(50, y, f"IFSC: {bank[2]}"); y -= 12
    c.showPage(); c.save()


def scan(pdf_path, seed):
    """Turn a text PDF into an image-only PDF that looks like a phone/scanner capture."""
    rnd = random.Random(seed)
    page = pdfium.PdfDocument(str(pdf_path))[0]
    img = page.render(scale=2.6).to_pil().convert("L")
    img = img.rotate(rnd.uniform(-0.9, 0.9), expand=True, fillcolor=255, resample=Image.BICUBIC)
    img = img.filter(ImageFilter.GaussianBlur(0.7))
    px = img.load()
    for _ in range(img.width * img.height // 90):
        x, y = rnd.randrange(img.width), rnd.randrange(img.height)
        px[x, y] = max(0, min(255, px[x, y] + rnd.randint(-55, 25)))
    buf = io.BytesIO(); img.save(buf, "JPEG", quality=62); buf.seek(0)
    Image.open(buf).convert("RGB").save(str(pdf_path), "PDF", resolution=170)


def main():
    (DATA / "vendors.json").write_text(json.dumps(VENDORS, indent=2))
    (DATA / "purchase_orders.json").write_text(json.dumps(POS, indent=2))
    (DATA / "ledger_seed.json").write_text(json.dumps(LEDGER_SEED, indent=2))
    manifest = []
    for i, spec in enumerate(INVOICES):
        path = SAMPLES / spec["file"]
        draw_invoice(spec, path)
        if spec["scanned"]:
            scan(path, seed=i + 7)
        sub, tax, _ = totals(spec)
        manifest.append({"file": spec["file"], "expected_decision": spec["expect"][0], "note": spec["expect"][1],
                         "scanned": spec["scanned"], "total": None if spec.get("omit_total") else round(sub + tax, 2)})
        print("wrote", spec["file"])
    (SAMPLES / "manifest.json").write_text(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()

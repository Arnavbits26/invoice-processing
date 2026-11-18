# Invoice → Decision (PS-1)

Takes a vendor invoice PDF, extracts it, matches it to a purchase order, applies rules and returns a
reasoned decision: **Approve, Approve with flag, Hold, Request info or Reject**.

## Run (macOS)

    cd ~/Desktop/zamp/invoice-processing
    rm -rf .venv && python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    brew install tesseract            # needed to read scanned invoices
    python -m pytest -q               # 19 tests
    python -m app.cli --all --reset   # run all 9 sample invoices in demo order

## Pipeline

intake → extract → completeness → vendor check → PO match → validation rules → duplicate check → decision

## Sample invoices (`samples/`, regenerate with `python scripts/make_data.py`)

| File | Expected | What it shows |
|---|---|---|
| 01 | Approve | Clean invoice, exact PO match |
| 02 | Approve | 1.2% over PO, inside the 2% tolerance |
| 03 | Approve | First half of a split PO (run before 04) |
| 04 | Hold | Second half exceeds the remaining PO balance |
| 05 | Reject | Scanned re-send of an already-approved invoice, number re-formatted |
| 06 | Approve with flag | Scanned, no PO number: PO inferred from vendor + amount + description |
| 07 | Request info | No invoice number and no total; drafts the vendor email |
| 08 | Hold | Bank account differs from vendor master (payment-fraud signal) |
| 09 | Reject | Vendor not on the approved list |

## Assumptions

- Indian GST invoices in INR; buyer is Meridian Retail Pvt Ltd (Karnataka).
- Tolerance: an invoice may exceed the remaining PO balance by up to 2%.
- Only approved invoices consume PO balance; held, rejected and info-requested ones do not.
- Any stage that errors holds the invoice. It never approves by default.

## Deploy (Render, Docker)

Tesseract can't be installed on Render's native Python runtime, so the app ships as a Docker service.

1. Push this folder to a GitHub repo (the `Dockerfile` and `render.yaml` are at the root).
2. On render.com: **New → Blueprint**, pick the repo. Render reads `render.yaml` and builds the image.
3. Optional: set `GEMINI_API_KEY` in the service's environment to turn on LLM extraction. Without it the rules + OCR extractor is used.
4. Open the `onrender.com` URL and click **Run all in order**.

Notes: the free plan sleeps after ~15 minutes idle (first load takes ~50 s) and its disk is ephemeral, so history resets on each deploy or restart. Use **Reset demo** to start clean.

Local Docker check: `docker build -t invoice . && docker run -p 10000:10000 invoice`, then open http://127.0.0.1:10000.

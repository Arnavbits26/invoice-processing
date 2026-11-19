# Invoice → Decision

Automated invoice processing for accounts payable. Upload a vendor invoice PDF (digital or scanned) and the app reads it, checks it against the vendor list and purchase orders, and returns one clear decision with the reasons: **Approve, Approve with flag, Hold, Request info or Reject**.

**Live demo:** https://invoice-to-decision-x1pd.onrender.com/
(Hosted on a free plan, so the first load after a long idle period can take up to a minute.)

---

## What it does

A person normally reads each invoice, checks the vendor, matches it to a purchase order (PO), checks the maths and tax, makes sure it was not paid already, and then decides. This app does that work in eight visible steps and shows its reasoning at every step.

| Decision | Meaning | Next step |
|---|---|---|
| **Approve** | Passes every check | Release for payment against the PO |
| **Approve with flag** | Fine to pay, but one point is uncertain | Pay after a person confirms the flagged point |
| **Hold** | Something looks wrong but may be fixable | Route to an AP reviewer; do not pay yet |
| **Request info** | Key details are missing | A reply email to the vendor is drafted |
| **Reject** | Must not be paid | Do not pay; notify AP |

Design principle: **rules decide, AI only reads.** Every decision comes from deterministic, explainable rules. An optional LLM can help read unusual layouts, but it never makes a decision. When the app is unsure, or a step fails, the invoice is held. It never approves by default.

## How it works

```
intake → extract → completeness → vendor check → PO match → validation rules → duplicate check → decision
```

| # | Step | What it checks |
|---|---|---|
| 1 | Intake | Valid PDF, has a text layer or is a scan |
| 2 | Extract fields | Vendor, GSTIN, invoice number, date, PO reference, tax, total, bank account, line items, each with a confidence score |
| 3 | Completeness | Invoice number, date, vendor name and total are present and readable |
| 4 | Vendor check | Vendor is on the approved list (fuzzy name match plus GSTIN); bank account matches the vendor master |
| 5 | PO match | Uses the PO number on the invoice, or infers the PO from vendor, amount and description |
| 6 | Validation rules | Line items and totals add up; tax rate and GST type (IGST vs CGST+SGST) are correct; prices and quantities match the PO; invoice fits the remaining PO balance (2% tolerance) |
| 7 | Duplicate check | Same file, same invoice number (ignoring dashes, slashes and spaces), or same vendor, amount and nearby date |
| 8 | Decision | Collects all flags, picks the most serious one, and writes the reason, next step and any vendor email |

Flags carry a severity, and the most serious one wins: `reject` > `hold` > `info needed` > `flag` > none.

Only **approved** invoices use up a PO's balance (kept in a ledger). That is how a second invoice against the same PO can be held for exceeding what is left.

## Sample invoices

Nine realistic invoices live in `samples/` (regenerate them with `python scripts/make_data.py`). Run them in order, because **03 must run before 04**.

| # | File | Expected decision | What it shows |
|---|---|---|---|
| 01 | `01_clean_sharma_office.pdf` | Approve | Clean invoice, exact PO match |
| 02 | `02_it_services_within_tolerance.pdf` | Approve | 1.2% over the PO, inside the 2% tolerance |
| 03 | `03_freight_part1.pdf` | Approve | First half of a split PO |
| 04 | `04_freight_part2_over_balance.pdf` | Hold | Second half goes over the remaining PO balance |
| 05 | `05_duplicate_scanned.pdf` | Reject | Scanned re-send of an already approved invoice, with a re-formatted number |
| 06 | `06_scanned_no_po_reference.pdf` | Approve with flag | Scanned, no PO number: PO inferred from vendor, amount and description |
| 07 | `07_missing_number_and_total.pdf` | Request info | No invoice number and no total; vendor email drafted |
| 08 | `08_bank_details_changed.pdf` | Hold | Bank account differs from the vendor master (payment-fraud signal) |
| 09 | `09_unapproved_vendor.pdf` | Reject | Vendor is not on the approved list |

### Edge cases covered

- **Split PO:** one purchase order shared across two invoices (03, 04).
- **Scanned duplicate:** read by OCR, matched to an earlier invoice despite a differently formatted number (05).
- **Changed bank details:** otherwise valid invoice with a payee account that differs from the vendor record (08).
- **Missing information:** no invoice number or total, so the app asks the vendor instead of guessing (07).

## Quick start

**Requirements:** Python 3.10+ and [Tesseract](https://github.com/tesseract-ocr/tesseract) (used to read scanned invoices).

```bash
git clone git@github.com:Arnavbits26/invoice-processing.git
cd invoice-processing

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

brew install tesseract        # macOS. On Debian/Ubuntu: sudo apt-get install tesseract-ocr

uvicorn app.server:app --reload
```

Open http://127.0.0.1:8000.

### Using the app

- **Process tab:** drop in a PDF or click **Run** on a sample. Watch the eight steps complete, then read the decision card, the fields that were read (with confidence), the line items, the PO balance bar and the invoice itself. When more information is needed, the drafted vendor email appears with a copy button.
- **Run all in order:** clears the history, then runs all nine samples in the right order.
- **Slow motion:** adds a short pause between steps so they are easy to follow.
- **Dashboard tab:** totals, outcome mix, run history (click a row to replay it) and PO balances.
- **Reset demo:** clears the history and restores the starting ledger.

### Command line

```bash
python -m app.cli --all --reset     # run all nine samples in demo order
```

## Configuration

Settings are read from environment variables. Copy `.env.example` to `.env` to set them locally (`.env` is git-ignored).

| Variable | Default | Purpose |
|---|---|---|
| `EXTRACTOR` | `auto` | `auto` uses Gemini when a key is set, otherwise rules; `llm` prefers Gemini; `rules` never calls it |
| `GEMINI_API_KEY` | empty | Optional. Turns on LLM reading. Without it the rules and OCR reader is used |
| `GEMINI_MODELS` | `gemini-3.1-flash-lite,gemini-flash-latest,gemini-3.6-flash` | Models tried in order; the first that answers wins |
| `GEMINI_THINKING` | `low` | Thinking level for Gemini 3.x models (`low`, `medium`, `high`) |
| `GEMINI_TIMEOUT` | `30` | Seconds to wait on one Gemini call |
| `GEMINI_BUDGET` | `60` | Total seconds across all models before falling back to rules |
| `TOLERANCE_PCT` | `2.0` | How far over the remaining PO balance an invoice may go and still pass |
| `ARITHMETIC_TOLERANCE` | `1.0` | Rupee rounding allowed when adding up lines, tax and total |
| `BUYER_GSTIN` | `29AABCM5678K1Z2` | Buyer's GSTIN, used to pick IGST vs CGST+SGST |
| `DB_PATH` | `data/app.db` | SQLite database file |
| `STAGE_DELAY` | `0.45` | Pause (seconds) between steps in the live view; set to `0` for full speed |

Other thresholds (vendor name match 88/100, PO inference confidence 0.80 with a 0.15 lead, 45-day duplicate window) are constants in `app/config.py`.

### Optional: Gemini reading

Add a key to `.env`:

```
GEMINI_API_KEY=your-key
```

The app then asks Gemini to read each PDF. If a model is busy or times out it moves to the next model in `GEMINI_MODELS`, and finally to the built-in rules reader, so a run always completes. The "What was read" panel shows which reader was used. The rules and OCR reader alone produces the expected decision for all nine samples.

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Web UI |
| `GET` | `/healthz` | Health check (also answers `HEAD`), used by Render and uptime monitors |
| `GET` | `/api/samples` | List the sample invoices |
| `POST` | `/api/samples/{name}/run` | Run a sample (`?slow=` for slow motion) |
| `POST` | `/api/runs` | Upload a PDF (PDF header check, 10 MB limit) |
| `GET` | `/api/runs/{id}/events` | Live stage events (Server-Sent Events) |
| `GET` | `/api/runs` | Run history |
| `GET` | `/api/runs/{id}` | One run in full |
| `GET` | `/api/runs/{id}/file` | The original PDF |
| `GET` | `/api/stats` | Dashboard totals |
| `GET` | `/api/pos` | PO balances |
| `POST` | `/api/reset` | Clear history and restore the seed ledger |

## Project structure

```
app/
  server.py      FastAPI app, SSE streaming, upload and dashboard endpoints
  pipeline.py    Runs the eight stages, emits an event per stage, fails safe to HOLD
  stages.py      Completeness, vendor, PO match, rules, duplicates and the decision
  extract.py     Text layer, OCR, rules parser and the optional Gemini reader
  store.py       SQLite: ledger of approved invoices and the run history
  masters.py     Vendor and PO master data loaders
  config.py      Settings and thresholds
  util.py        Helpers (invoice-number cleaning, dates, Indian rupee formatting)
  cli.py         Command-line runner
data/            vendors.json, purchase_orders.json, ledger_seed.json
samples/         Nine sample invoices and manifest.json
scripts/         make_data.py (regenerates data and samples)
static/          index.html (single-page UI)
tests/           Pipeline and server tests
Dockerfile       Image with Tesseract installed
render.yaml      Render Blueprint
```

## Tests

```bash
python -m pytest -q
```

27 tests cover every sample's expected decision (in demo order), the fail-safe behaviour, non-PDF input, reset, the helper functions and the server endpoints including live streaming.

## Docker and deployment

```bash
docker build -t invoice .
docker run -p 10000:10000 invoice      # then open http://127.0.0.1:10000
```

Deploying to [Render](https://render.com):

1. Push the repo to GitHub.
2. In Render choose **New → Blueprint** and select the repo. Render reads `render.yaml` and builds the Docker image (Tesseract cannot be installed on Render's native Python runtime, which is why Docker is used).
3. Optional: add `GEMINI_API_KEY` under the service's Environment settings.

Notes for the free plan:

- The service spins down after about 15 minutes without traffic, and the next request takes around a minute. An uptime monitor that calls `/healthz` every few minutes keeps it awake.
- The disk is ephemeral, so history resets on every deploy or restart. Use **Reset demo** or **Run all in order** to start clean.

## Assumptions and limitations

- Indian GST invoices in INR. The buyer is a fictional company, Meridian Retail Pvt Ltd (Karnataka).
- **Vendors, purchase orders and invoices are synthetic**, generated by `scripts/make_data.py`. In production the master data would come from an ERP.
- The 2% tolerance is an assumption and is measured against the remaining PO balance, not the full PO value.
- Only approved invoices consume PO balance. Held, rejected and info-requested invoices do not.
- "Approve" means released for payment. There is no payment or ERP integration, and vendor emails are drafted but not sent.
- There is no sign-in or user roles, and no screen yet for a person to resolve a held invoice.
- One currency (INR) and one buyer.

## Tech stack

Python, FastAPI, Server-Sent Events, SQLite, pdfplumber, pypdfium2, Tesseract OCR (pytesseract), rapidfuzz, httpx, an optional Gemini API reader, plain HTML and JavaScript for the UI, pytest, Docker, Render.

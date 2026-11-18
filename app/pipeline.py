"""Orchestrates the 8 stages and emits an event for each, so the UI can show progress live."""
import time
import traceback
import uuid
from pathlib import Path

from . import extract as ex_mod
from . import stages, store
from .util import inr

STAGES = [("intake", "Intake"), ("extract", "Extract fields"), ("completeness", "Completeness check"),
          ("vendor", "Vendor check"), ("po", "PO match"), ("rules", "Validation rules"),
          ("duplicates", "Duplicate check"), ("decision", "Decision")]


def new_run_id():
    return uuid.uuid4().hex[:10]


def run(path, filename=None, emit=None, delay=0.0, run_id=None):
    """Process one invoice. emit(event) is called as stages start and finish. Returns the result dict."""
    path = str(path)
    filename = filename or Path(path).name
    run_id = run_id or new_run_id()
    events = []

    def push(stage, status, summary="", details=None, flags=None, data=None, ms=None):
        label = dict(STAGES)[stage]
        e = {"stage": stage, "label": label, "status": status, "summary": summary, "details": details or [],
             "flags": flags or [], "data": data or {}, "ms": ms, "ts": store.now()}
        events.append(e)
        store.save_events(run_id, events)
        if emit:
            emit(e)

    def stage(name, fn):
        """Run one stage, announce it, never let an exception become an approval."""
        push(name, "running")
        if delay:
            time.sleep(delay)
        t = time.perf_counter()
        try:
            out = fn()
        except Exception as exc:  # fail safe: an error holds the invoice
            traceback.print_exc()
            out = stages.outcome("fail", f"Stage crashed: {type(exc).__name__}", [str(exc)[:200]], [
                stages.flag("STAGE_ERROR", "hold", f"The '{dict(STAGES)[name]}' step failed ({type(exc).__name__}); "
                            "a person must review this invoice.")])
        ms = int((time.perf_counter() - t) * 1000)
        push(name, out["status"], out["summary"], out["details"], out["flags"], out["data"], ms)
        return out

    store.create_run(run_id, filename, "pending")
    done = {}

    # 1 intake
    info = {}

    def _intake():
        info.update(ex_mod.inspect_pdf(path))
        kind = "scanned image (no text layer)" if info["scanned"] else "digital PDF with a text layer"
        return stages.outcome("ok", f"{kind}, {info['pages']} page(s)", [
            f"File: {filename} ({info['bytes'] / 1024:.0f} KB)", f"SHA-256: {info['sha256'][:16]}…"],
            data={"scanned": info["scanned"], "pages": info["pages"]})
    done["intake"] = stage("intake", _intake)
    if not info:
        return _finish(run_id, filename, done, None, None, None, events, error="Could not open the file as a PDF")
    store_hash(run_id, info["sha256"])

    # 2 extract
    ex = {}

    def _extract():
        ex.update(ex_mod.extract(path, info))
        shown = {k: v["value"] for k, v in ex["fields"].items() if v["value"] is not None}
        d = [f"Method: {ex['method']}"] + ex["notes"] + [f"{k}: {v}" for k, v in shown.items()
                                                         if k not in ("bank_account",)]
        d.append(f"{len(ex['line_items'])} line item(s) read")
        return stages.outcome("ok", f"Read {len(shown)} fields and {len(ex['line_items'])} line(s) via {ex['method']}",
                              d, [], {"fields": ex["fields"], "line_items": ex["line_items"]})
    done["extract"] = stage("extract", _extract)
    if not ex:
        return _finish(run_id, filename, done, None, None, None, events, error="Extraction failed")

    done["completeness"] = stage("completeness", lambda: stages.completeness(ex))

    vendor = po = po_info = None
    skip = lambda why: stages.outcome("skip", why)
    if stages.val(ex, "vendor_name") or stages.val(ex, "vendor_gstin"):
        done["vendor"] = stage("vendor", lambda: stages.vendor_check(ex))
        vendor = done["vendor"]["data"].get("vendor")
    else:
        done["vendor"] = stage("vendor", lambda: skip("No vendor name or GSTIN to look up"))

    if vendor:
        done["po"] = stage("po", lambda: stages.po_match(ex, vendor))
        po, po_info = done["po"]["data"].get("po"), done["po"]["data"]
    else:
        done["po"] = stage("po", lambda: skip("Skipped: vendor not approved"))

    if po:
        done["rules"] = stage("rules", lambda: stages.rules(ex, vendor, po))
    else:
        done["rules"] = stage("rules", lambda: skip("Skipped: no PO to validate against"))

    if vendor:
        done["duplicates"] = stage("duplicates", lambda: stages.duplicates(ex, vendor, info["sha256"], run_id))
    else:
        done["duplicates"] = stage("duplicates", lambda: skip("Skipped: vendor not approved"))

    verdict = {}

    def _decide():
        verdict.update(stages.decide(ex, done, vendor, po, po_info))
        return stages.outcome("ok", verdict["decision"].replace("_", " ").title(), [verdict["headline"]]
                              + [f"Note: {n}" for n in verdict["notes"]] + ["Next: " + verdict["next_action"]],
                              [], {"decision": verdict["decision"]})
    done["decision"] = stage("decision", _decide)
    if not verdict:
        verdict.update({"decision": "HOLD", "headline": "On hold: the decision step failed.", "reasons": [],
                        "notes": [], "next_action": "Route to a reviewer.", "email_draft": None})
    return _finish(run_id, filename, done, ex, vendor, po, events, verdict=verdict, info=info)


def store_hash(run_id, h):
    store.set_hash(run_id, h)


def _finish(run_id, filename, done, ex, vendor, po, events, verdict=None, info=None, error=None):
    f = (lambda k: ex["fields"][k]["value"]) if ex else (lambda k: None)
    verdict = verdict or {"decision": "HOLD", "headline": f"On hold: {error}.", "reasons": [], "notes": [],
                          "next_action": "Ask the sender for a readable PDF.", "email_draft": None}
    result = {
        "run_id": run_id, "filename": filename, **verdict, "error": error,
        "vendor_name": vendor["name"] if vendor else f("vendor_name"), "invoice_number": f("invoice_number"),
        "invoice_date": f("invoice_date"), "total": f("total"), "po_number": po["po_number"] if po else None,
        "extracted": {k: v for k, v in (ex["fields"].items() if ex else [])}, "line_items": ex["line_items"] if ex else [],
        "extraction_method": ex["method"] if ex else None, "balance": done.get("rules", {}).get("data", {}),
        "po_match": {k: v for k, v in done.get("po", {}).get("data", {}).items() if k != "po"},
    }
    if verdict["decision"] in ("APPROVE", "APPROVE_WITH_FLAG") and vendor and po and f("invoice_number") and f("total"):
        store.add_ledger(f("invoice_number"), vendor["id"], f("total"), f("invoice_date"), po["po_number"],
                         verdict["decision"], run_id)
        result["ledger_updated"] = True
    store.finish_run(run_id, result)
    return result

import json
import shutil

import pytest

from app import config, pipeline, stages, store
from app.util import inr, norm_invoice_no, norm_po, parse_date

SAMPLES = config.SAMPLES_DIR
MANIFEST = json.loads((SAMPLES / "manifest.json").read_text())


def run(name):
    return pipeline.run(SAMPLES / name)


def codes(result):
    return {r["code"] for r in result["reasons"]}


def test_every_sample_gets_its_expected_decision_in_demo_order():
    for m in MANIFEST:
        r = run(m["file"])
        assert r["decision"] == m["expected_decision"], f"{m['file']}: {r['headline']}"


def test_clean_invoice_is_approved_and_ledger_updated():
    r = run("01_clean_sharma_office.pdf")
    assert r["decision"] == "APPROVE" and r["po_number"] == "PO-2026-1001" and r["total"] == 215822.0
    assert any(x["invoice_no"] == "SOS/2026/0431" for x in store.ledger_for_po("PO-2026-1001"))


def test_within_tolerance_is_approved_with_a_note():
    r = run("02_it_services_within_tolerance.pdf")
    assert r["decision"] == "APPROVE"
    assert any("inside the 2% tolerance" in n for n in r["notes"])
    assert any("on-site visit" in n for n in r["notes"])  # extra line not on the PO is noted


def test_split_po_second_invoice_is_held_with_the_arithmetic():
    assert run("03_freight_part1.pdf")["decision"] == "APPROVE"
    r = run("04_freight_part2_over_balance.pdf")
    assert r["decision"] == "HOLD" and "OVER_BALANCE" in codes(r)
    msg = next(x["message"] for x in r["reasons"] if x["code"] == "OVER_BALANCE")
    assert "₹11,800" in msg and "₹2,95,000" in msg


def test_split_po_order_matters_second_invoice_alone_fits_the_full_po():
    assert run("04_freight_part2_over_balance.pdf")["decision"] == "APPROVE"


def test_scanned_duplicate_with_reformatted_number_is_rejected():
    r = run("05_duplicate_scanned.pdf")
    assert r["decision"] == "REJECT" and "DUPLICATE_INVOICE" in codes(r)
    assert r["extraction_method"] == "rules + OCR"


def test_scanned_invoice_without_po_infers_it_and_flags():
    r = run("06_scanned_no_po_reference.pdf")
    assert r["decision"] == "APPROVE_WITH_FLAG" and r["po_number"] == "PO-2026-1005"
    assert r["po_match"]["method"] == "inferred" and r["po_match"]["confidence"] >= 0.8


def test_missing_critical_fields_requests_info_and_drafts_an_email():
    r = run("07_missing_number_and_total.pdf")
    assert r["decision"] == "REQUEST_INFO"
    assert {"MISSING_INVOICE_NUMBER", "MISSING_TOTAL"} <= codes(r)
    assert r["email_draft"] and "invoice number" in r["email_draft"]["body"] and "PO-2026-1004" in r["email_draft"]["body"]
    assert store.ledger_for_po("PO-2026-1004") == []  # nothing consumed


def test_changed_bank_details_hold_and_never_print_full_account_numbers():
    r = run("08_bank_details_changed.pdf")
    assert r["decision"] == "HOLD" and "BANK_CHANGED" in codes(r)
    msg = next(x["message"] for x in r["reasons"] if x["code"] == "BANK_CHANGED")
    assert "50100455667788" not in msg and "1100010233445" not in msg and "••••7788" in msg


def test_unapproved_vendor_is_rejected_and_later_stages_skipped():
    r = run("09_unapproved_vendor.pdf")
    assert r["decision"] == "REJECT" and "VENDOR_NOT_APPROVED" in codes(r)
    ev = store.get_run(r["run_id"])["events"]
    skipped = {e["stage"] for e in ev if e["status"] == "skip"}
    assert {"po", "rules", "duplicates"} <= skipped


def test_resubmitting_an_approved_invoice_is_rejected_as_duplicate():
    assert run("01_clean_sharma_office.pdf")["decision"] == "APPROVE"
    r = run("01_clean_sharma_office.pdf")
    assert r["decision"] == "REJECT" and {"DUPLICATE_FILE", "DUPLICATE_INVOICE"} & codes(r)


def test_stage_crash_fails_safe_to_hold_not_approve(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("simulated crash")
    monkeypatch.setattr(stages, "po_match", boom)
    r = run("01_clean_sharma_office.pdf")
    assert r["decision"] == "HOLD" and "STAGE_ERROR" in codes(r)
    assert store.ledger_for_po("PO-2026-1001") == []


def test_non_pdf_input_does_not_crash(tmp_path):
    bad = tmp_path / "not_a_pdf.pdf"
    bad.write_text("hello")
    r = pipeline.run(bad)
    assert r["decision"] == "HOLD" and r["error"]


def test_events_are_stored_for_the_dashboard():
    r = run("01_clean_sharma_office.pdf")
    saved = store.get_run(r["run_id"])
    assert [e["stage"] for e in saved["events"] if e["status"] != "running"] == [s for s, _ in pipeline.STAGES]
    assert saved["decision"] == "APPROVE"


def test_reset_restores_demo_state():
    run("03_freight_part1.pdf")
    store.reset()
    assert store.list_runs() == [] and store.ledger_for_po("PO-2026-1003") == []
    assert store.ledger_for_po("PO-2026-1007")  # seeded history is back


@pytest.mark.parametrize("raw,expected", [("SOS/2026/0417", "SOS20260417"), ("sos-2026-0417", "SOS20260417"),
                                          (None, "")])
def test_invoice_number_normalisation(raw, expected):
    assert norm_invoice_no(raw) == expected


def test_helpers():
    assert norm_po("po 2026 1001") == "PO-2026-1001" and norm_po("nothing") is None
    assert parse_date("10-Nov-2026") == "2026-11-10" and parse_date("garbage") is None
    assert inr(215822) == "₹2,15,822" and inr(1234567) == "₹12,34,567" and inr(None) == "unknown"

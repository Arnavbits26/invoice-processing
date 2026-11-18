"""The checks. Each stage returns an outcome dict:
   {status: ok|warn|fail|skip, summary, details[], flags[], data{}}
A flag is {code, severity, message}. Severity decides the final decision:
   reject > hold > info_needed > flag (approve with flag) > info (no effect).
"""
from datetime import timedelta

from rapidfuzz import fuzz

from . import config, masters, store
from .util import inr, last4, norm_invoice_no, norm_po, tokens, to_date

REQUIRED = [("invoice_number", "invoice number"), ("invoice_date", "invoice date"),
            ("vendor_name", "vendor name"), ("total", "total amount")]


def flag(code, severity, message):
    return {"code": code, "severity": severity, "message": message}


def outcome(status, summary, details=None, flags=None, data=None):
    return {"status": status, "summary": summary, "details": details or [], "flags": flags or [], "data": data or {}}


def val(ex, key):
    return ex["fields"][key]["value"]


def conf(ex, key):
    return ex["fields"][key]["confidence"]


# ---------------------------------------------------------------- stage 3
def completeness(ex):
    flags, details = [], []
    for key, label in REQUIRED:
        if val(ex, key) in (None, ""):
            flags.append(flag(f"MISSING_{key.upper()}", "info_needed", f"The invoice has no readable {label}."))
            details.append(f"Missing: {label}")
        elif conf(ex, key) < 0.6:
            flags.append(flag(f"LOW_CONFIDENCE_{key.upper()}", "flag",
                              f"The {label} was hard to read (confidence {conf(ex, key):.0%}); please verify it."))
            details.append(f"Low confidence: {label} ({conf(ex, key):.0%})")
        else:
            details.append(f"Present: {label}")
    if val(ex, "bank_account") in (None, ""):
        details.append("No bank details printed (cannot cross-check payee account)")
    missing = [f for f in flags if f["severity"] == "info_needed"]
    if missing:
        return outcome("warn", f"{len(missing)} critical field(s) missing", details, flags)
    return outcome("ok", "All critical fields present", details, flags)


# ---------------------------------------------------------------- stage 4
def vendor_check(ex):
    name, gstin = val(ex, "vendor_name"), val(ex, "vendor_gstin")
    best, how, score = None, None, 0
    for v in masters.vendors():
        if gstin and v["gstin"] == gstin:
            best, how, score = v, "GSTIN", 100
            break
        s = fuzz.token_set_ratio(tokens(name), tokens(v["name"]))
        if s > score:
            best, score = v, s
            how = "name"
    flags, details = [], []
    if how == "GSTIN":
        s = fuzz.token_set_ratio(tokens(name), tokens(best["name"]))
        details.append(f"GSTIN {gstin} found in the approved list: {best['name']}")
        if s < 70:
            flags.append(flag("VENDOR_NAME_MISMATCH", "hold",
                              f"The GSTIN belongs to '{best['name']}' but the invoice is titled '{name}'."))
    elif best and score >= config.VENDOR_NAME_MATCH:
        details.append(f"Matched by name ({score:.0f}% similar): {best['name']}")
        if gstin and gstin != best["gstin"]:
            flags.append(flag("GSTIN_MISMATCH", "hold",
                              f"Name matches '{best['name']}' but the GSTIN on the invoice ({gstin}) differs from the "
                              f"registered one ({best['gstin']})."))
        elif not gstin:
            flags.append(flag("GSTIN_MISSING", "flag", "Vendor matched by name only; no GSTIN on the invoice."))
    else:
        flags.append(flag("VENDOR_NOT_APPROVED", "reject",
                          f"'{name or 'Unknown vendor'}' is not on the approved vendor list. It needs vendor "
                          "onboarding before any invoice can be paid."))
        return outcome("fail", "Vendor is not on the approved list", [f"Closest match: {best['name']} ({score:.0f}%)"
                       if best else "No candidates"], flags)
    acct, ifsc = val(ex, "bank_account"), val(ex, "bank_ifsc")
    if acct:
        if acct == best["account"] and (not ifsc or ifsc == best["ifsc"]):
            details.append(f"Bank account {last4(acct)} matches the vendor master")
        else:
            flags.append(flag("BANK_CHANGED", "hold",
                              f"Bank account on the invoice ({last4(acct)}, {ifsc or 'no IFSC'}) does not match the "
                              f"vendor master ({last4(best['account'])}, {best['ifsc']}). Changed bank details are a "
                              "common payment-fraud pattern: verify by calling the vendor on their registered number "
                              "before paying."))
            details.append(f"Bank account {last4(acct)} differs from master {last4(best['account'])}")
    status = "ok" if not [f for f in flags if f["severity"] in ("hold", "reject")] else "warn"
    return outcome(status, f"Approved vendor: {best['name']}", details, flags, {"vendor": best, "matched_by": how})


# ---------------------------------------------------------------- stage 5
def remaining_balance(po):
    total = masters.po_amounts(po)["total"]
    used = sum(r["amount"] for r in store.ledger_for_po(po["po_number"]))
    return total, used, round(total - used, 2)


def po_match(ex, vendor):
    ref = norm_po(val(ex, "po_reference") or "")
    pos = masters.purchase_orders()
    if ref:
        po = pos.get(ref)
        if not po:
            return outcome("fail", f"{ref} does not exist", [], [flag(
                "PO_NOT_FOUND", "hold", f"The invoice cites {ref}, which is not in the purchase-order system.")])
        if po["vendor_id"] != vendor["id"]:
            owner = masters.vendor_by_id(po["vendor_id"])["name"]
            return outcome("fail", f"{ref} belongs to another vendor", [], [flag(
                "PO_VENDOR_MISMATCH", "hold", f"{ref} was issued to {owner}, not {vendor['name']}.")])
        return outcome("ok", f"Explicit PO reference {ref}", [f"{ref} was issued to {vendor['name']}"], [],
                       {"po": po, "method": "explicit", "confidence": 1.0})

    # No PO number printed: infer it from vendor + amount + description
    total = val(ex, "total")
    desc = " ".join(i["description"] for i in ex["line_items"])
    inv_date = to_date(val(ex, "invoice_date"))
    cands = []
    for po in pos.values():
        if po["vendor_id"] != vendor["id"]:
            continue
        _, _, rem = remaining_balance(po)
        if rem <= 0:
            continue
        amt = 0.5 if total is None else max(0.0, 1 - (abs(total - rem) / rem) / 0.10)
        d = fuzz.token_set_ratio(tokens(desc), tokens(" ".join(l["desc"] for l in po["lines"]))) / 100
        dt = 1.0 if (inv_date is None or to_date(po["date"]) <= inv_date) else 0.0
        cands.append({"po": po, "confidence": round(0.55 * amt + 0.35 * d + 0.10 * dt, 3),
                      "amount_score": round(amt, 2), "description_score": round(d, 2), "remaining": rem})
    cands.sort(key=lambda c: -c["confidence"])
    if not cands:
        return outcome("fail", "No PO number and no open PO for this vendor", [], [flag(
            "NO_PO_MATCH", "hold", "There is no PO number on the invoice and the vendor has no open purchase order.")])
    best = cands[0]
    margin = best["confidence"] - (cands[1]["confidence"] if len(cands) > 1 else 0)
    details = [f"{c['po']['po_number']}: confidence {c['confidence']:.2f} (amount {c['amount_score']:.2f}, "
               f"description {c['description_score']:.2f})" for c in cands[:3]]
    if best["confidence"] >= config.PO_INFER_MIN_CONFIDENCE and margin >= config.PO_INFER_MIN_MARGIN:
        n = best["po"]["po_number"]
        return outcome("warn", f"No PO number printed: inferred {n} ({best['confidence']:.0%} confidence)", details,
                       [flag("PO_INFERRED", "flag", f"The invoice does not quote a PO. It was matched to {n} on vendor, "
                             f"amount and description with {best['confidence']:.0%} confidence; a person should "
                             "confirm the match.")],
                       {"po": best["po"], "method": "inferred", "confidence": best["confidence"]})
    return outcome("fail", "Could not match to a PO with confidence", details, [flag(
        "NO_PO_MATCH", "hold", f"No PO number on the invoice and the best candidate ({best['po']['po_number']}) is only "
        f"{best['confidence']:.0%} confident.")])


# ---------------------------------------------------------------- stage 6
def rules(ex, vendor, po):
    flags, details = [], []
    sub, tax, total = val(ex, "subtotal"), val(ex, "tax_amount"), val(ex, "total")
    tol = config.TOLERANCE_PCT

    # arithmetic
    line_sum = round(sum(i["amount"] or 0 for i in ex["line_items"]), 2)
    if ex["line_items"] and sub is not None:
        if abs(line_sum - sub) > config.ARITHMETIC_TOLERANCE:
            flags.append(flag("LINE_SUM_MISMATCH", "info_needed",
                              f"Line items add up to {inr(line_sum)} but the invoice says sub-total {inr(sub)}."))
        else:
            details.append(f"Line items add up to the sub-total ({inr(sub)})")
    if None not in (sub, tax, total):
        if abs(sub + tax - total) > config.ARITHMETIC_TOLERANCE:
            flags.append(flag("ARITHMETIC_MISMATCH", "info_needed",
                              f"{inr(sub)} + {inr(tax)} tax = {inr(sub + tax)}, but the invoice total is {inr(total)}."))
        else:
            details.append(f"Sub-total + tax = total ({inr(total)})")
        expected = round(sub * po["gst_rate"] / 100, 2)
        if abs(tax - expected) > max(config.ARITHMETIC_TOLERANCE, 0.005 * sub):
            flags.append(flag("TAX_RATE_MISMATCH", "hold",
                              f"Tax charged is {inr(tax)} but {po['gst_rate']}% of {inr(sub)} is {inr(expected)}."))
        else:
            details.append(f"Tax matches the {po['gst_rate']}% GST rate on the PO")
    else:
        details.append("Total or tax not readable: arithmetic checks skipped")

    # GST type: same state as the buyer -> CGST+SGST, otherwise IGST
    intra = vendor["gstin"][:2] == config.BUYER_GSTIN[:2]
    want = "CGST+SGST" if intra else "IGST"
    got = val(ex, "tax_type")
    if got and got != want:
        flags.append(flag("WRONG_GST_TYPE", "hold",
                          f"Vendor state code {vendor['gstin'][:2]} vs buyer {config.BUYER_GSTIN[:2]}: the invoice "
                          f"should charge {want} but shows {got}."))
    elif got:
        details.append(f"GST type {got} is correct for the vendor's state")

    cur = val(ex, "currency")
    if cur and cur != "INR":
        flags.append(flag("CURRENCY_MISMATCH", "hold", f"The invoice is in {cur}; the PO is in INR."))

    # line items against the PO
    for it in ex["line_items"]:
        best, score = None, 0
        for pl in po["lines"]:
            s = fuzz.token_set_ratio(tokens(it["description"]), tokens(pl["desc"]))
            if s > score:
                best, score = pl, s
        if best and score >= 60:
            if it["rate"] and abs(it["rate"] - best["rate"]) / best["rate"] * 100 > tol:
                flags.append(flag("PRICE_MISMATCH", "hold",
                                  f"'{it['description']}' is billed at {inr(it['rate'])} per unit; the PO price is "
                                  f"{inr(best['rate'])}."))
            elif it["qty"] and it["qty"] > best["qty"]:
                flags.append(flag("QTY_EXCEEDS_PO", "hold",
                                  f"'{it['description']}': {it['qty']:g} units billed, only {best['qty']:g} on the PO."))
            else:
                details.append(f"Line '{it['description']}' matches the PO (qty {it['qty']:g} at {inr(it['rate'])})")
        else:
            flags.append(flag("LINE_NOT_ON_PO", "info",
                              f"'{it['description']}' ({inr(it['amount'])}) is not a line on the PO."))
            details.append(f"Line '{it['description']}' is not on the PO")

    # remaining PO balance
    po_total, used, rem = remaining_balance(po)
    data = {"po_total": po_total, "already_invoiced": used, "remaining": rem}
    if total is None:
        details.append("No total, so the PO balance check was skipped")
    elif rem <= 0:
        flags.append(flag("PO_EXHAUSTED", "hold", f"{po['po_number']} is already fully invoiced "
                          f"({inr(used)} of {inr(po_total)})."))
    elif total <= rem:
        details.append(f"{inr(total)} is within the remaining PO balance of {inr(rem)} "
                       f"(PO {inr(po_total)}, already invoiced {inr(used)})")
    else:
        over = round(total - rem, 2)
        pct = over / rem * 100
        if pct <= tol:
            flags.append(flag("WITHIN_TOLERANCE", "info",
                              f"Invoice is {inr(over)} ({pct:.1f}%) over the remaining PO balance of {inr(rem)}, inside "
                              f"the {tol:g}% tolerance."))
            details.append(f"{pct:.1f}% over the PO balance, inside the {tol:g}% tolerance")
        else:
            flags.append(flag("OVER_BALANCE", "hold",
                              f"Invoice of {inr(total)} exceeds the remaining balance on {po['po_number']} by "
                              f"{inr(over)} ({pct:.1f}%, tolerance {tol:g}%). PO {inr(po_total)} − already invoiced "
                              f"{inr(used)} = {inr(rem)} left."))
    bad = [f for f in flags if f["severity"] in ("hold", "info_needed")]
    return outcome("warn" if bad else "ok", f"{len(bad)} rule violation(s)" if bad else "All rules passed",
                   details, flags, data)


# ---------------------------------------------------------------- stage 7
def duplicates(ex, vendor, file_hash, run_id):
    flags, details = [], []
    prior = store.file_seen(file_hash, run_id)
    if prior:
        flags.append(flag("DUPLICATE_FILE", "reject", f"This exact file was already processed ({prior['filename']}, "
                          f"decision {prior['decision']})."))
    num, amt = val(ex, "invoice_number"), val(ex, "total")
    if not num:
        details.append("No invoice number: exact-number duplicate check skipped")
    ledger = store.ledger_for_vendor(vendor["id"])
    hit = next((r for r in ledger if num and r["invoice_no_norm"] == norm_invoice_no(num)), None)
    if hit:
        flags.append(flag("DUPLICATE_INVOICE", "reject",
                          f"Invoice {num} is the same as {hit['invoice_no']} already approved on {hit['date']} for "
                          f"{inr(hit['amount'])} against {hit['po_number']}. Paying again would be a double payment."))
    elif amt is not None:
        d = to_date(val(ex, "invoice_date"))
        for r in ledger:
            rd = to_date(r["date"])
            if abs(r["amount"] - amt) < 0.5 and d and rd and abs((d - rd).days) <= config.DUPLICATE_WINDOW_DAYS:
                flags.append(flag("POSSIBLE_DUPLICATE", "hold",
                                  f"Same vendor and amount ({inr(amt)}) as invoice {r['invoice_no']} from {r['date']}, "
                                  f"but a different invoice number. Possible re-issue."))
                break
    if not flags:
        details.append(f"No earlier invoice from {vendor['name']} with this number or amount")
    bad = [f for f in flags if f["severity"] in ("reject", "hold")]
    return outcome("fail" if bad else "ok", "Duplicate found" if bad else "No duplicates", details, flags)


# ---------------------------------------------------------------- stage 8
ORDER = ["reject", "hold", "info_needed", "flag"]
DECISION = {"reject": "REJECT", "hold": "HOLD", "info_needed": "REQUEST_INFO", "flag": "APPROVE_WITH_FLAG"}


def decide(ex, stages, vendor, po, po_info):
    flags = [f for s in stages.values() for f in s["flags"]]
    decision = "APPROVE"
    for sev in ORDER:
        if any(f["severity"] == sev for f in flags):
            decision = DECISION[sev]
            break
    reasons = [f for f in flags if f["severity"] != "info"]
    notes = [f["message"] for f in flags if f["severity"] == "info"]
    name = val(ex, "vendor_name") or "the vendor"
    num = val(ex, "invoice_number") or "(no number)"
    pon = po["po_number"] if po else None
    headline = {
        "APPROVE": f"Approved: {num} from {name} matches {pon} and passes every check.",
        "APPROVE_WITH_FLAG": f"Approved with a flag: {num} from {name} can be paid, but a person should confirm "
                             f"{len(reasons)} point(s).",
        "HOLD": f"On hold: {num} from {name} needs a person to review before payment.",
        "REQUEST_INFO": f"More information needed: {num} from {name} cannot be processed as sent.",
        "REJECT": f"Rejected: {num} from {name} must not be paid.",
    }[decision]
    next_action = {
        "APPROVE": f"Release for payment against {pon}.",
        "APPROVE_WITH_FLAG": "Release for payment once a person confirms the flagged point(s).",
        "HOLD": "Route to the AP reviewer. Do not pay until the issue is resolved.",
        "REQUEST_INFO": "Send the drafted email to the vendor and wait for a corrected invoice.",
        "REJECT": "Do not pay. Notify AP; no vendor email is sent automatically.",
    }[decision]
    email = None
    if decision == "REQUEST_INFO":
        asks = [f["message"] for f in reasons if f["severity"] == "info_needed"]
        email = {"to": vendor["name"] if vendor else name,
                 "subject": f"Invoice {num if val(ex, 'invoice_number') else ''} could not be processed: please resend".replace("  ", " "),
                 "body": f"Dear {name} team,\n\nThank you for your invoice. We could not process it because:\n\n"
                         + "\n".join(f"  - {a}" for a in asks)
                         + f"\n\nPlease send a corrected invoice{(' referencing ' + pon) if pon else ''} and we will "
                           "process it straight away.\n\nRegards,\nAccounts Payable\nMeridian Retail Pvt Ltd"}
    return {"decision": decision, "headline": headline, "reasons": reasons, "notes": notes, "next_action": next_action,
            "email_draft": email}

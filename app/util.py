import re
from datetime import date, datetime

_DATE_FORMATS = ("%d-%b-%Y", "%d %b %Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%d-%B-%Y", "%d %B %Y")


def norm_invoice_no(s):
    """SOS/2026/0417 and SOS-2026-0417 and 'sos 2026 0417' all become SOS20260417."""
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def norm_po(s):
    """Return PO-YYYY-NNNN, or None if the text does not look like a PO number."""
    m = re.search(r"PO\D{0,2}(\d{4})\D{0,2}(\d{3,5})", (s or "").upper())
    return f"PO-{m.group(1)}-{m.group(2)}" if m else None


def parse_date(s):
    s = (s or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def to_date(iso):
    try:
        return date.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


def money(s):
    """'1,42,500.00' -> 142500.0 ; None on failure."""
    try:
        return float(str(s).replace(",", "").replace("INR", "").replace("Rs.", "").strip())
    except (TypeError, ValueError):
        return None


def inr(x, decimals=0):
    """Indian digit grouping with a rupee sign: 215822 -> ₹2,15,822"""
    if x is None:
        return "unknown"
    neg = x < 0
    s = f"{abs(x):.{decimals}f}"
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return f"{'-' if neg else ''}₹{whole}{('.' + frac) if frac else ''}"


def tokens(s):
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).strip()


def last4(s):
    d = re.sub(r"\D", "", s or "")
    return f"••••{d[-4:]}" if d else "none"

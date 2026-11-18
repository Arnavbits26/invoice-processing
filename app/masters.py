"""Read-only master data: approved vendors and purchase orders."""
import json
from functools import lru_cache

from . import config


@lru_cache(maxsize=1)
def vendors():
    return json.loads((config.DATA_DIR / "vendors.json").read_text())


@lru_cache(maxsize=1)
def purchase_orders():
    return {p["po_number"]: p for p in json.loads((config.DATA_DIR / "purchase_orders.json").read_text())}


def vendor_by_id(vid):
    return next((v for v in vendors() if v["id"] == vid), None)


def po_amounts(po):
    net = round(sum(l["qty"] * l["rate"] for l in po["lines"]), 2)
    tax = round(net * po["gst_rate"] / 100, 2)
    return {"net": net, "tax": tax, "total": round(net + tax, 2)}

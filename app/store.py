"""SQLite persistence: the payment ledger (for balances and duplicates) and the run history."""
import json
import sqlite3
import threading
from datetime import datetime, timezone

from . import config
from .util import norm_invoice_no

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_no TEXT, invoice_no_norm TEXT, vendor_id TEXT,
  amount REAL, date TEXT, po_number TEXT, decision TEXT, run_id TEXT);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, filename TEXT, file_hash TEXT, status TEXT, decision TEXT, headline TEXT,
  vendor TEXT, invoice_no TEXT, total REAL, created_at TEXT, finished_at TEXT, result_json TEXT, events_json TEXT);
"""


def _conn():
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init():
    with _lock, _conn() as c:
        c.executescript(SCHEMA)
        if c.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0:
            _seed(c)


def _seed(c):
    for r in json.loads((config.DATA_DIR / "ledger_seed.json").read_text()):
        c.execute("INSERT INTO ledger (invoice_no, invoice_no_norm, vendor_id, amount, date, po_number, decision, run_id) "
                  "VALUES (?,?,?,?,?,?,?,?)", (r["invoice_no"], norm_invoice_no(r["invoice_no"]), r["vendor_id"],
                                              r["amount"], r["date"], r["po_number"], r["decision"], "seed"))


def reset():
    """Back to the demo starting point: history cleared, ledger re-seeded."""
    with _lock, _conn() as c:
        c.executescript(SCHEMA)
        c.execute("DELETE FROM ledger")
        c.execute("DELETE FROM runs")
        _seed(c)


def ledger_for_po(po_number):
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM ledger WHERE po_number=?", (po_number,))]


def ledger_for_vendor(vendor_id):
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM ledger WHERE vendor_id=?", (vendor_id,))]


def add_ledger(invoice_no, vendor_id, amount, date, po_number, decision, run_id):
    with _lock, _conn() as c:
        c.execute("INSERT INTO ledger (invoice_no, invoice_no_norm, vendor_id, amount, date, po_number, decision, run_id) "
                  "VALUES (?,?,?,?,?,?,?,?)", (invoice_no, norm_invoice_no(invoice_no), vendor_id, amount, date,
                                              po_number, decision, run_id))


def file_seen(file_hash, exclude_run=None):
    with _lock, _conn() as c:
        r = c.execute("SELECT id, filename, decision FROM runs WHERE file_hash=? AND id != ? AND status='done' "
                      "ORDER BY created_at LIMIT 1", (file_hash, exclude_run or "")).fetchone()
        return dict(r) if r else None


def create_run(run_id, filename, file_hash):
    with _lock, _conn() as c:
        c.execute("INSERT OR IGNORE INTO runs (id, filename, file_hash, status, created_at, events_json) VALUES (?,?,?,?,?,?)",
                  (run_id, filename, file_hash, "running", now(), "[]"))


def set_hash(run_id, file_hash):
    with _lock, _conn() as c:
        c.execute("UPDATE runs SET file_hash=? WHERE id=?", (file_hash, run_id))


def save_events(run_id, events):
    with _lock, _conn() as c:
        c.execute("UPDATE runs SET events_json=? WHERE id=?", (json.dumps(events), run_id))


def finish_run(run_id, result, status="done"):
    with _lock, _conn() as c:
        c.execute("UPDATE runs SET status=?, decision=?, headline=?, vendor=?, invoice_no=?, total=?, finished_at=?, "
                  "result_json=? WHERE id=?",
                  (status, result.get("decision"), result.get("headline"), result.get("vendor_name"),
                   result.get("invoice_number"), result.get("total"), now(), json.dumps(result), run_id))


def get_run(run_id):
    with _lock, _conn() as c:
        r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["result"] = json.loads(d.pop("result_json") or "null")
    d["events"] = json.loads(d.pop("events_json") or "[]")
    return d


def list_runs(limit=200):
    with _lock, _conn() as c:
        rows = c.execute("SELECT id, filename, status, decision, headline, vendor, invoice_no, total, created_at, "
                         "finished_at FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def stats():
    with _lock, _conn() as c:
        rows = c.execute("SELECT decision, COUNT(*) n, COALESCE(SUM(total),0) amt FROM runs WHERE status='done' "
                         "GROUP BY decision").fetchall()
    return {r["decision"]: {"count": r["n"], "amount": r["amt"]} for r in rows}

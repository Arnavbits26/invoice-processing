"""Run invoices from the terminal:  python -m app.cli samples/01_clean_sharma_office.pdf [--reset] [--all]"""
import argparse
import glob
import sys

from . import pipeline, store

ICON = {"running": "…", "ok": "✓", "warn": "!", "fail": "✗", "skip": "-"}


def show(e):
    if e["status"] == "running":
        return
    print(f"  {ICON.get(e['status'], '?')} {e['label']:<20} {e['summary']}  ({e['ms']} ms)")
    for f in e["flags"]:
        tag = "NOTE" if f["severity"] == "info" else f["severity"].upper()
        print(f"      [{tag}] {f['message']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--all", action="store_true", help="run every sample in order")
    ap.add_argument("--reset", action="store_true", help="reset ledger and history first")
    a = ap.parse_args()
    store.init()
    if a.reset:
        store.reset()
    files = sorted(glob.glob("samples/*.pdf")) if a.all else a.files
    if not files:
        sys.exit("give a PDF path or --all")
    for p in files:
        print(f"\n== {p}")
        r = pipeline.run(p, emit=show)
        print(f"  => {r['decision']}: {r['headline']}")


if __name__ == "__main__":
    main()

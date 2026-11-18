"""HTTP API + static UI. Runs execute in a background thread; the browser follows them over Server-Sent Events."""
import asyncio
import json
import re
import threading
from collections import Counter
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from . import config, masters, pipeline, stages, store

ROOT = config.ROOT
SAMPLE_TITLES = {
    "01": "Clean invoice", "02": "Slightly over PO, within tolerance", "03": "Split PO: first invoice",
    "04": "Split PO: second invoice", "05": "Scanned duplicate", "06": "Scanned, no PO number",
    "07": "Missing number and total", "08": "Bank details changed", "09": "Unapproved vendor"}


@asynccontextmanager
async def lifespan(_):
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    store.init()
    yield


app = FastAPI(title="Invoice → Decision", lifespan=lifespan)


def _samples():
    items = json.loads((config.SAMPLES_DIR / "manifest.json").read_text())
    return [{"file": m["file"], "title": SAMPLE_TITLES.get(m["file"][:2], m["file"]), "note": m["note"],
             "scanned": m["scanned"]} for m in items]


def _start(path, filename, slow):
    run_id = pipeline.new_run_id()
    store.create_run(run_id, filename, "pending")
    delay = config.STAGE_DELAY if slow else 0.0

    def work():
        try:
            pipeline.run(path, filename=filename, delay=delay, run_id=run_id)
        except Exception as e:  # never leave a browser waiting on a run that died
            store.finish_run(run_id, {"decision": "HOLD", "headline": f"On hold: processing failed ({type(e).__name__}).",
                                      "reasons": [], "notes": [], "next_action": "Route to a reviewer.",
                                      "email_draft": None, "error": str(e)[:200]}, status="error")
    threading.Thread(target=work, daemon=True).start()
    return run_id


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/samples")
def samples():
    return _samples()


@app.post("/api/samples/{name}/run")
def run_sample(name: str, slow: bool = Query(True)):
    if name not in {s["file"] for s in _samples()}:
        raise HTTPException(404, "Unknown sample")
    return {"run_id": _start(config.SAMPLES_DIR / name, name, slow)}


@app.post("/api/runs")
async def upload(file: UploadFile = File(...), slow: bool = Query(True)):
    data = await file.read()
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "That does not look like a PDF file.")
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "File is larger than 10 MB.")
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    run_id = pipeline.new_run_id()
    path = config.UPLOAD_DIR / f"{run_id}.pdf"
    path.write_bytes(data)
    store.create_run(run_id, file.filename or "upload.pdf", "pending")
    delay = config.STAGE_DELAY if slow else 0.0

    def work():
        try:
            pipeline.run(path, filename=file.filename or "upload.pdf", delay=delay, run_id=run_id)
        except Exception as e:
            store.finish_run(run_id, {"decision": "HOLD", "headline": f"On hold: processing failed ({type(e).__name__}).",
                                      "reasons": [], "notes": [], "next_action": "Route to a reviewer.",
                                      "email_draft": None, "error": str(e)[:200]}, status="error")
    threading.Thread(target=work, daemon=True).start()
    return {"run_id": run_id}


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.get("/api/runs/{run_id}/events")
async def events(run_id: str):
    async def gen():
        sent = 0
        while True:
            r = store.get_run(run_id)
            if not r:
                yield _sse("error", {"message": "unknown run"})
                return
            for e in r["events"][sent:]:
                yield _sse("stage", e)
            sent = len(r["events"])
            if r["status"] != "running":
                yield _sse("done", r["result"])
                return
            await asyncio.sleep(0.1)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs")
def runs():
    return store.list_runs()


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    r = store.get_run(run_id)
    if not r:
        raise HTTPException(404, "Unknown run")
    return r


@app.get("/api/runs/{run_id}/file")
def run_file(run_id: str):
    if not re.fullmatch(r"[a-f0-9]{10}", run_id):
        raise HTTPException(404)
    up = config.UPLOAD_DIR / f"{run_id}.pdf"
    if up.exists():
        return FileResponse(up, media_type="application/pdf")
    r = store.get_run(run_id)
    if r and r["filename"] in {s["file"] for s in _samples()}:
        return FileResponse(config.SAMPLES_DIR / r["filename"], media_type="application/pdf")
    raise HTTPException(404, "File not available")


@app.get("/api/stats")
def stats():
    done = [r for r in store.list_runs(1000) if r["status"] in ("done", "error")]
    by = Counter(r["decision"] for r in done)
    val = lambda *ds: sum((r["total"] or 0) for r in done if r["decision"] in ds)
    return {"processed": len(done), "by_decision": dict(by),
            "approved_value": val("APPROVE", "APPROVE_WITH_FLAG"), "held_value": val("HOLD", "REQUEST_INFO"),
            "rejected_value": val("REJECT")}


@app.get("/api/pos")
def pos():
    out = []
    for po in masters.purchase_orders().values():
        total, used, rem = stages.remaining_balance(po)
        out.append({"po_number": po["po_number"], "vendor": masters.vendor_by_id(po["vendor_id"])["name"],
                    "total": total, "invoiced": used, "remaining": rem})
    return out


@app.post("/api/reset")
def reset():
    store.reset()
    return {"ok": True}

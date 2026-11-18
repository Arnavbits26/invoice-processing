import json
import time

import pytest
from fastapi.testclient import TestClient

from app import config
from app.server import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    with TestClient(app) as c:
        yield c


def wait_done(client, run_id, timeout=15):
    t = time.time()
    while time.time() - t < timeout:
        r = client.get(f"/api/runs/{run_id}").json()
        if r["status"] != "running":
            return r
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_samples_listed(client):
    s = client.get("/api/samples").json()
    assert len(s) == 9 and s[0]["title"] == "Clean invoice" and s[4]["scanned"] is True


def test_run_sample_and_read_result(client):
    rid = client.post("/api/samples/01_clean_sharma_office.pdf/run?slow=false").json()["run_id"]
    r = wait_done(client, rid)
    assert r["status"] == "done" and r["result"]["decision"] == "APPROVE"
    assert len([e for e in r["events"] if e["status"] != "running"]) == 8


def test_sse_streams_every_stage_then_done(client):
    rid = client.post("/api/samples/04_freight_part2_over_balance.pdf/run?slow=false").json()["run_id"]
    with client.stream("GET", f"/api/runs/{rid}/events") as resp:
        body = "".join(resp.iter_text())
    kinds = [l.split(": ", 1)[1] for l in body.splitlines() if l.startswith("event: ")]
    assert kinds.count("stage") >= 8 and kinds[-1] == "done"
    done = json.loads([l for l in body.splitlines() if l.startswith("data: ")][-1][6:])
    assert done["decision"] in ("APPROVE", "HOLD")


def test_upload_pdf_and_reject_non_pdf(client):
    pdf = (config.SAMPLES_DIR / "09_unapproved_vendor.pdf").read_bytes()
    rid = client.post("/api/runs?slow=false", files={"file": ("my invoice.pdf", pdf, "application/pdf")}).json()["run_id"]
    r = wait_done(client, rid)
    assert r["result"]["decision"] == "REJECT" and r["filename"] == "my invoice.pdf"
    assert client.get(f"/api/runs/{rid}/file").status_code == 200
    bad = client.post("/api/runs", files={"file": ("x.pdf", b"hello", "application/pdf")})
    assert bad.status_code == 400


def test_dashboard_endpoints_and_reset(client):
    for n in ("03_freight_part1.pdf", "07_missing_number_and_total.pdf"):
        wait_done(client, client.post(f"/api/samples/{n}/run?slow=false").json()["run_id"])
    st = client.get("/api/stats").json()
    assert st["processed"] == 2 and st["by_decision"] == {"APPROVE": 1, "REQUEST_INFO": 1}
    assert st["approved_value"] == 295000
    po = {p["po_number"]: p for p in client.get("/api/pos").json()}["PO-2026-1003"]
    assert po["invoiced"] == 295000 and po["remaining"] == 295000
    assert len(client.get("/api/runs").json()) == 2
    client.post("/api/reset")
    assert client.get("/api/runs").json() == [] and client.get("/api/stats").json()["processed"] == 0


def test_unknown_things_404(client):
    assert client.get("/api/runs/nope").status_code == 404
    assert client.post("/api/samples/../../etc/passwd/run").status_code in (404, 405)
    assert client.get("/api/runs/../x/file").status_code == 404


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}

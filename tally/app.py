"""FastAPI routes and server-sent progress events.

Run with:  uv run uvicorn tally.app:app --reload
Nothing here sends anything anywhere: drafts are returned for the person to
copy, and the evidence pack is a download.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse

from tally.agents.dispute import draft
from tally.agents.gemini import AgentError, Gemini
from tally.config import load_setup, save_setup
from tally.engine.models import Setup
from tally.export import evidence_pdf
from tally.pipeline import RunState, Runner, recompute
from tally.report import build_report, claimed_findings
from tally.store import Store

STATIC = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        ask = Gemini()
    except AgentError:
        ask = None  # app still opens saved runs and replays without a key
    app.state.runner = Runner(Store(), ask, load_setup)
    yield


app = FastAPI(title="Tally", lifespan=lifespan)


def runner() -> Runner:
    return app.state.runner


def get_run(run_id: str) -> RunState:
    run = runner().get(run_id)
    if run is None:
        raise HTTPException(404, "no such run")
    return run


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "index.html").read_text()


# --- set-up ------------------------------------------------------------------


@app.get("/api/setup")
def get_setup() -> dict[str, Any]:
    return load_setup().model_dump(mode="json")


@app.put("/api/setup")
def put_setup(setup: Setup) -> dict[str, Any]:
    save_setup(setup)
    return setup.model_dump(mode="json")


# --- runs ----------------------------------------------------------------------


@app.post("/api/runs")
async def create_run(files: list[UploadFile]) -> dict[str, Any]:
    if runner().ask is None:
        raise HTTPException(503, "No Gemini key: set GEMINI_KEY in .env and restart")
    if not files:
        raise HTTPException(400, "no files")
    uploads = [(f.filename or "upload", await f.read()) for f in files]
    run = runner().start(uploads)
    return {"run_id": run.run_id, "docs": len(run.docs), "duplicates": run.duplicates}


@app.get("/api/runs")
def list_runs() -> list[dict[str, str]]:
    return [{"run_id": r, "created": c} for r, c in runner().store.list_runs()]


@app.get("/api/runs/{run_id}")
def get_report(run_id: str) -> dict[str, Any]:
    return build_report(get_run(run_id), load_setup())


@app.get("/api/runs/{run_id}/events")
async def events(run_id: str, replay: bool = False) -> StreamingResponse:
    """Live progress. With ``replay``, stream a saved run's events at the speed they happened."""
    get_run(run_id)
    r = runner()

    async def stream():
        if replay or not r.is_live(run_id):
            last = 0.0
            for t, event in r.store.events(run_id):
                if replay:
                    await asyncio.sleep(max(0.0, t - last))
                    last = t
                yield _sse(event)
            yield _sse({"type": "end"})
            return
        q = r.listen(run_id)
        try:
            # Listening started first, so anything after this read arrives on the queue.
            backlog = [e for _, e in r.store.events(run_id)]
            seen = {e["seq"] for e in backlog}
            for event in backlog:
                yield _sse(event)
            if any(e["type"] == "end" for e in backlog):
                return
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if event["seq"] in seen:
                    continue
                yield _sse(event)
                if event["type"] == "end":
                    return
        finally:
            r.unlisten(run_id, q)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


@app.get("/api/runs/{run_id}/docs/{doc_id}/file")
def doc_file(run_id: str, doc_id: str) -> FileResponse:
    doc = _doc(get_run(run_id), doc_id)
    return FileResponse(doc.path, media_type=doc.mime, filename=doc.name)


def _doc(run: RunState, doc_id: str):
    doc = next((d for d in run.docs if d.doc_id == doc_id), None)
    if doc is None:
        raise HTTPException(404, "no such document")
    return doc


# --- a person's decisions ---------------------------------------------------------


@app.put("/api/runs/{run_id}/docs/{doc_id}/reading")
async def correct_reading(run_id: str, doc_id: str, reading: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Review queue: a person confirms or corrects a reading; matching re-runs."""
    run = get_run(run_id)
    doc = _doc(run, doc_id)
    if doc.kind not in {"self_bill", "ticket", "waste_transfer_note"}:
        raise HTTPException(400, "only ticket and invoice readings can be corrected")
    failures = await runner().correct(run, doc, reading)
    return {"failures": failures, "report": build_report(run, load_setup())}


@app.post("/api/runs/{run_id}/matches/{item_id}")
def decide_match(run_id: str, item_id: str, line_id: str | None = Body(None, embed=True)) -> dict[str, Any]:
    """A person settles a match Argon couldn't (or overrides Argon)."""
    run = get_run(run_id)
    run.adjudications[item_id] = (line_id, "Decided by you.")
    runner().save(run)
    return build_report(run, load_setup())


@app.put("/api/runs/{run_id}/approvals")
def set_approvals(run_id: str, approvals: dict[str, bool] = Body(...)) -> dict[str, Any]:
    run = get_run(run_id)
    run.approvals.update(approvals)
    runner().save(run)
    return build_report(run, load_setup())


# --- outputs: drafts and downloads only ---------------------------------------------


def _claim(run: RunState, customer: str):
    setup = load_setup()
    rec = recompute(run, setup)
    findings = claimed_findings(run, rec, customer)
    if not findings:
        raise HTTPException(400, f"no approved lines for {customer}")
    jobs = {m.item.job.job_id: m.item.job for m in rec.matches.matches}
    lines = {l.line_id: l for l in rec.matches.lines}
    return setup, findings, jobs, lines


@app.post("/api/runs/{run_id}/dispute")
async def dispute(run_id: str, customer: str = Body(..., embed=True)) -> dict[str, Any]:
    run = get_run(run_id)
    setup, findings, jobs, lines = _claim(run, customer)
    d = await draft(runner().ask, setup.haulier, customer, findings, jobs, lines)
    return {"customer": d.customer, "subject": d.subject, "body": d.body, "total": str(d.total),
            "used_template": d.used_template, "lines": len(findings)}


@app.get("/api/runs/{run_id}/evidence.pdf")
def evidence(run_id: str, customer: str) -> Response:
    run = get_run(run_id)
    setup, findings, jobs, lines = _claim(run, customer)
    pdf = evidence_pdf(run, setup.haulier, customer, findings, jobs, lines)
    name = f"tally-evidence-{customer.lower().replace(' ', '-')}.pdf"
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/runs/{run_id}/shifts.csv")
def shifts_csv(run_id: str) -> Response:
    """The shift calendar log, for the haulier's records."""
    report = build_report(get_run(run_id), load_setup())
    buf = io.StringIO()
    cols = ["reg", "date", "kind", "customer", "guarantee", "earned", "paid", "top_up_paid", "claim", "pending"]
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(report["shifts"])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="tally-shifts-{run_id}.csv"'})

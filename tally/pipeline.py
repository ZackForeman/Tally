"""Runs the eight stages over a batch of uploads and reports progress as events.

Each document flows through route -> extract -> verify on its own, in
parallel, so the progress board ticks per file. Matching, money and the
report are plain code and re-run in milliseconds whenever a person corrects a
reading or settles a match.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from tally.agents.adjudicate import adjudicate_all
from tally.agents.audit import audit
from tally.agents.extract import extract
from tally.agents.gemini import Asker, FilePart, guess_mime
from tally.agents.router import DocKind, route
from tally.agents.verify import VerifyStatus, build, verify
from tally.engine.match import MatchResult, Status, adjudicate, match
from tally.engine.merge import merge_jobs
from tally.engine.models import Invoice, Job, Setup
from tally.engine.money import Reconciliation, reconcile
from tally.engine.worklog import parse_work_log
from tally.store import Store


class Attempt(BaseModel):
    reader: str
    reading: dict[str, Any]
    failures: list[dict[str, str]]


class Doc(BaseModel):
    doc_id: str
    name: str
    path: str
    mime: str
    kind: str | None = None
    route_reason: str | None = None
    status: str = "queued"  # queued, routing, extracting, verifying, rereading, passed, fixed, review, confirmed, skipped, error
    attempts: list[Attempt] = Field(default_factory=list)
    jobs: list[Job] = Field(default_factory=list)
    invoice: Invoice | None = None
    problems: list[str] = Field(default_factory=list)
    error: str | None = None


class RunState(BaseModel):
    run_id: str
    created: str
    status: str = "running"  # running, done
    docs: list[Doc] = Field(default_factory=list)
    duplicates: list[str] = Field(default_factory=list)  # uploads skipped as re-uploads
    adjudications: dict[str, tuple[str | None, str]] = Field(default_factory=dict)
    audit_status: str = "pending"  # pending, running, done, error
    audit: list[dict[str, Any]] = Field(default_factory=list)
    approvals: dict[str, bool] = Field(default_factory=dict)
    seconds: float | None = None

    def jobs(self) -> list[Job]:
        return [j for d in self.docs for j in d.jobs]

    def lines(self):
        return [l for d in self.docs if d.invoice for l in d.invoice.lines]


class Runner:
    """Owns live runs: state, event fan-out to SSE listeners, and persistence."""

    def __init__(self, store: Store, ask: Asker | None, setup_loader):
        self.store = store
        self.ask = ask
        self.load_setup = setup_loader
        self.runs: dict[str, RunState] = {}
        self._listeners: dict[str, list[asyncio.Queue]] = {}
        self._seq: dict[str, int] = {}
        self._t0: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()

    # --- state ---------------------------------------------------------------

    def get(self, run_id: str) -> RunState | None:
        if run_id not in self.runs:
            raw = self.store.load_run(run_id)
            if raw is None:
                return None
            self.runs[run_id] = RunState.model_validate_json(raw)
        return self.runs[run_id]

    def save(self, run: RunState) -> None:
        self.store.save_run(run.run_id, run.created, run.model_dump_json())

    # --- events --------------------------------------------------------------

    def listen(self, run_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._listeners.setdefault(run_id, []).append(q)
        return q

    def unlisten(self, run_id: str, q: asyncio.Queue) -> None:
        if q in self._listeners.get(run_id, []):
            self._listeners[run_id].remove(q)

    def is_live(self, run_id: str) -> bool:
        return run_id in self._t0

    async def emit(self, run_id: str, type_: str, **data: Any) -> None:
        seq = self._seq[run_id] = self._seq.get(run_id, 0) + 1
        t = round(time.monotonic() - self._t0.get(run_id, time.monotonic()), 2)
        event = {"seq": seq, "t": t, "type": type_, **data}
        self.store.add_event(run_id, seq, t, event)
        for q in self._listeners.get(run_id, []):
            q.put_nowait(event)

    # --- run -----------------------------------------------------------------

    def start(self, files: list[tuple[str, bytes]]) -> RunState:
        created = datetime.now().isoformat(timespec="seconds")
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + hashlib.sha1(created.encode()).hexdigest()[:4]
        run = RunState(run_id=run_id, created=created)
        folder = self.store.run_dir(run_id)
        seen: set[str] = set()
        for name, data in files:
            digest = hashlib.sha256(data).hexdigest()
            if digest in seen:
                run.duplicates.append(name)
                continue
            seen.add(digest)
            doc_id = digest[:12]
            path = folder / f"{doc_id}{Path(name).suffix.lower()}"
            path.write_bytes(data)
            run.docs.append(Doc(doc_id=doc_id, name=name, path=str(path), mime=guess_mime(Path(name))))
        self.runs[run_id] = run
        self.save(run)
        self._t0[run_id] = time.monotonic()
        self._spawn(self._run(run))
        return run

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, run: RunState) -> None:
        setup = self.load_setup()
        started = time.monotonic()
        await self.emit(run.run_id, "run", status="started", docs=[d.model_dump(include={"doc_id", "name"}) for d in run.docs])
        await asyncio.gather(*(self._doc(run, d, setup) for d in run.docs))

        await self.emit(run.run_id, "stage", stage="match", status="running")
        rec = recompute(run, setup)
        await self.settle(run, rec, setup)
        rec = recompute(run, setup)

        run.status = "done"
        run.seconds = round(time.monotonic() - started, 1)
        self.save(run)
        await self.emit(run.run_id, "report", seconds=run.seconds, owed=str(rec.owed()))
        self._spawn(self._audit(run, rec))

    async def _doc(self, run: RunState, doc: Doc, setup: Setup) -> None:
        async def status(s: str, message: str = "", **extra: Any) -> None:
            doc.status = s
            await self.emit(run.run_id, "doc", doc_id=doc.doc_id, status=s, message=message, **extra)

        try:
            file = FilePart(Path(doc.path).read_bytes(), doc.mime, doc.name)
            await status("routing")
            kind, reason = await route(self.ask, file)
            doc.kind, doc.route_reason = kind, reason
            if kind is DocKind.OTHER:
                await status("skipped", f"not a ticket or invoice: {reason}", kind=kind)
                return
            if kind is DocKind.WORK_LOG:
                doc.jobs, doc.problems = parse_work_log(file.data, doc.name, doc.doc_id)
                await status("passed", f"{len(doc.jobs)} rows read", kind=kind)
                return

            await status("extracting", kind=kind)
            reading = await extract(self.ask, file, kind, setup)
            await status("verifying")

            async def notify(s: str, message: str) -> None:
                await status(s, message)

            result = await verify(self.ask, file, kind, reading, setup, doc.doc_id, notify)
            doc.attempts = [
                Attempt(reader=a.reader, reading=a.reading, failures=[vars(f) for f in a.failures])
                for a in result.attempts
            ]
            doc.jobs, doc.invoice = result.jobs, result.invoice
            message = {
                VerifyStatus.PASSED: "all checks passed",
                VerifyStatus.FIXED: "Argon re-read it and the checks now pass",
                VerifyStatus.REVIEW: "still failing checks after two re-reads: needs a person",
            }[result.status]
            await status(result.status.value, message, records=len(doc.jobs) or (len(doc.invoice.lines) if doc.invoice else 0))
        except Exception as e:  # one bad file must not sink the run
            doc.error = f"{type(e).__name__}: {e}"
            await status("error", doc.error)
        finally:
            self.save(run)

    async def settle(self, run: RunState, rec: Reconciliation, setup: Setup) -> None:
        """Ask Argon about candidate matches it hasn't already decided."""
        open_ = [m for m in rec.matches.by_status(Status.CANDIDATE) if m.item.item_id not in run.adjudications]
        if not open_ or self.ask is None:
            return
        await self.emit(run.run_id, "stage", stage="adjudicate", status="running", count=len(open_))
        try:
            decisions = await adjudicate_all(self.ask, open_)
        except Exception as e:
            await self.emit(run.run_id, "stage", stage="adjudicate", status="error", message=str(e))
            return
        run.adjudications.update(decisions)
        for item_id, (line_id, reasoning) in decisions.items():
            await self.emit(run.run_id, "adjudicated", item_id=item_id, line_id=line_id, reasoning=reasoning)
        self.save(run)

    async def _audit(self, run: RunState, rec: Reconciliation) -> None:
        if self.ask is None:
            await self.emit(run.run_id, "end")
            self._t0.pop(run.run_id, None)
            return
        run.audit_status = "running"
        await self.emit(run.run_id, "stage", stage="audit", status="running")
        try:
            run.audit = await audit(self.ask, rec)
            run.audit_status = "done"
            await self.emit(run.run_id, "audit", patterns=run.audit)
        except Exception as e:
            run.audit_status = "error"
            await self.emit(run.run_id, "stage", stage="audit", status="error", message=str(e))
        self.save(run)
        await self.emit(run.run_id, "end")
        self._t0.pop(run.run_id, None)

    # --- a person's changes ---------------------------------------------------

    async def correct(self, run: RunState, doc: Doc, reading: dict[str, Any]) -> list[dict[str, str]]:
        """A person's corrected reading. Checks still run, but a person's word is final."""
        jobs, invoice, failures = build(doc.kind, reading, doc.doc_id, doc.name)
        doc.attempts.append(Attempt(reader="person", reading=reading, failures=[vars(f) for f in failures]))
        doc.jobs, doc.invoice = jobs, invoice
        doc.status = "confirmed"
        setup = self.load_setup()
        await self.settle(run, recompute(run, setup), setup)
        self.save(run)
        return [vars(f) for f in failures]


def recompute(run: RunState, setup: Setup) -> Reconciliation:
    """Match and price from the current readings. Pure code; safe to call often."""
    result = match(merge_jobs(run.jobs()), run.lines(), setup.settings)
    result = apply_decisions(result, run.adjudications, setup)
    return reconcile(result, setup.rate_cards, setup.settings)


def apply_decisions(result: MatchResult, decisions: dict, setup: Setup) -> MatchResult:
    """Apply decisions that still fit: corrections can change the candidates under them."""
    usable, taken = {}, {m.line.line_id for m in result.matches if m.line}
    for m in result.by_status(Status.CANDIDATE):
        decision = decisions.get(m.item.item_id)
        if decision is None:
            continue
        line_id, reasoning = decision
        if line_id is None or (line_id in {c.line_id for c in m.candidates} and line_id not in taken):
            usable[m.item.item_id] = (line_id, reasoning)
            if line_id:
                taken.add(line_id)
    return adjudicate(result, usable, setup.settings)

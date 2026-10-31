# Tally

Tally reads a haulier's self-bill invoices and delivery tickets, finds every job
the customer forgot to pay for (or paid short), and drafts the dispute. The
models read the documents, plain Python counts the money, and the haulier
approves every line before anything leaves.

The full design is in `Tally — Self-Bill Reconciliation Agent.md`.

## Layout

```
tally/
  engine/          deterministic core, no model calls
    models.py      Job, InvoiceLine, Invoice, RateCard, Settings
    normalize.py   ticket / reg / customer keys; snap regs to the fleet list
    shifts.py      day / night / weekend shift calendar
    match.py       matching rules 1-3, duplicates, adjudication hook
    money.py       pay rules, line flags, shift guarantee, evidence warnings
    checks.py      verify-stage hard checks (net weight, VAT, reg format...)
    merge.py       one job per load when a ticket and a log row both record it
    worklog.py     CSV/XLSX work log reader
  agents/          Gemini stages: gemini.py (client), router, extract, verify,
                   adjudicate, audit, dispute
  pipeline.py      runs the stages, emits progress events, re-prices on corrections
  app.py           FastAPI routes + server-sent events
  report.py        dashboard data; export.py evidence PDF; store.py SQLite
  tests/
  demo/            fictional demo set
  static/          single-page UI
```

## Run

```
uv sync
uv run uvicorn tally.app:app --port 8000      # then open http://localhost:8000
```

Put the Gemini key in `.env` as `GEMINI_KEY` (see `.env.example`). Set-up (fleet,
rate cards) is copied from `tally/demo/setup.json` into `data/setup.json` on first
run; edit it from the Set-up button. Runs, uploads and the SQLite file live in
`data/`, which is git-ignored. Never commit real tickets or invoices.

## Test

```
uv run pytest                                  # offline, includes a scripted end-to-end run
uv run python -m tally.demo.smoke --run        # live run through Gemini, checks the answer
```

The smoke set (`data/smoke`, fictional) covers a missing load, a misread net
weight, unpaid waiting time and a chute job paid at the non-chute rate. It should
come out at £282.73 on ticked lines; `--run` prints PASS/FAIL per claim. Gemini quirks are recorded in `AGENTS.md`.

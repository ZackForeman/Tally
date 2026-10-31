# AGENTS.md: quirks found while building Tally

Lessons that cost time once. Read before changing the Gemini calls.

## Gemini Interactions API (google-genai 2.34)

- The skill lives in `.agents/skills/gemini-api-dev/` (installed by `npx skills add`). Claude Code
  only auto-loads skills from `.claude/skills/`, so read it directly.
- JSON output: `response_format={"type": "text", "mime_type": "application/json", "schema": ...}`
  plus `response_mime_type="application/json"`. The TypedDict calls the key `schema_`; send `schema`.
- Nullable fields: `{"type": ["string", "null"]}` works. Add `"format": "date"` to date fields, or
  Flash returns UK `01/09/2026` and the self-bill fails its checks, costing an Argon re-read.
- Images go in as `{"type": "image", "data": b64, "mime_type": ...}`, PDFs as
  `{"type": "document", ...}`. Inline limit is 100 MB per request (50 MB for PDFs).
- Background runs: poll `interactions.get(id)` only while status is `queued` or `in_progress`.
  `requires_action` never resolves on its own; polling on "not terminal" loops forever.
- `background=True` needs `store=True` (the default). Every other call sends `store=False`, so
  customers' tickets aren't retained server-side.
- Argon: `thinking_level` only. No `temperature`, `top_p`, `top_k`, `thinking_budget`.
- The key is `GEMINI_KEY` in `.env`, not the SDK default `GEMINI_API_KEY`; `Gemini()` reads both.

## Measured timings (smoke set, 4 files)

- Flash-Lite route ~1 s, Flash extract 12-15 s, Argon high re-read 14-21 s, whole run 46 s.
- Audit (Argon high, background) ~30 s after the report, so it never blocks the dashboard.

## Behaviour to keep

- When a document really is inconsistent (prints net 2.6 t against gross - tare = 2.0 t), Argon
  transcribes it as written and the doc goes to review. Never prompt it to make the numbers agree.
- `uv run python -m tally.demo.smoke --run` writes the smoke set (2 self-bills, 5 conveyance notes,
  a work log) and checks the live answer: £282.73 ticked, £287.23 in all. The cases and their
  hand-worked amounts are in the module docstring.
- Test tickets need signatures drawn as handwriting. A typed "Received by: P. Jones" is read,
  fairly, as no signature, which marks every line weak and unticks it.

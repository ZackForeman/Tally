# Tally — Self-Bill Reconciliation Agent

Oct 30, 2026 · @Zack Foreman

## The problem and the person

Tally reads a haulier's self-bill invoices and delivery tickets, finds every job the customer forgot to pay for, and drafts the dispute. The pitch: "Last summer I recovered £9,333 for a haulage firm by checking self-bills against tickets by hand. Tally does it in under a minute."

**The named person.** The office manager at a small haulage subcontractor (3–10 trucks). Customers such as quarries and surfacing contractors write the invoice for them (self-billing). The haulier never sees a job list until the remittance arrives, and missed or short-paid loads are common.

**Why it's hard by hand.** The evidence is messy and spread across documents:

- Weighbridge tickets (printed, carbon copies, photographed in a cab)
- Waste transfer notes (mostly handwritten)
- The driver's work log or diary
- Monthly self-bill PDFs, one per customer

The two tickets you photographed show the real-world mess Tally has to handle: the same truck written as NX66AYE on one and NX66AYF on the other, a weight written as "14.5 − 12.5 = 2.0", a licence field holding a vehicle reg, and an unsigned receiver box.

**What Tally finds.** Jobs missing from the invoice, jobs paid at the wrong tonnage or rate, and paperwork gaps that would weaken a claim.

## How it passes the four judging tests

Each test in the builder guide maps to a specific design choice, so you can name them out loud in the demo.

| Judging test | How Tally meets it | Say this in the demo |
| --- | --- | --- |
| Specific problem, real person | A haulage office manager checking self-bills; your £9,333 recovery | "I did this by hand for weeks." |
| Needed Gemini 4 Argon | Multi-file synthesis across a quarter of invoices and ticket photos in one 1M-token context; an autonomous verification loop on every extraction; Argon orchestrating Flash subagents | "Argon checks its own reading of every ticket before a pound is counted." |
| People stay in control of money | Code, not the model, calculates every amount. Nothing is sent: the haulier approves each claimed line, then copies or downloads the dispute pack | "It drafts. You decide." |
| Runs live in under 3 minutes | About 20 synthetic documents plus one ticket photographed on stage; parallel extraction; cached fallback run | Live run on the judges' screen |

## Architecture

Argon is the lead: it re-reads anything that fails a check, settles ambiguous matches, audits the whole set and drafts the dispute. Fast Flash models do the per-file reading in parallel, and plain Python does every calculation involving money.

&#91;embedded content: Tally pipeline · 12 stages, 3 kinds of step\]

| Model | Job in Tally | Thinking level |
| --- | --- | --- |
| `gemini-3.5-flash-lite` | Classify each upload | n/a (fastest) |
| `gemini-3.8-flash` | Extract fields from each document, in parallel | default |
| `gemini-4-argon` | Re-read failed extractions | `high` |
| `gemini-4-argon` | Adjudicate ambiguous matches; draft disputes | `medium` |
| `gemini-4-argon` | Audit the whole set in one context | `high`, `background=True` |

## Pipeline stages

Eight stages run in order; stages 3 and 4 loop per document until the extraction passes its checks or goes to a person.

1. **Ingest.** Drag in a batch: self-bill PDFs, ticket photos (JPG/PNG), and an optional work log (CSV or XLSX). Each file gets an ID and a hash so re-uploads are ignored.
2. **Route** (`gemini-3.5-flash-lite`, low latency). Classify each file: self-bill invoice, weighbridge ticket, waste transfer note, work log, or other. Anything "other" is shown to the user and skipped.
3. **Extract** (`gemini-3.8-flash`, one subagent per document, in parallel). Return JSON to a fixed schema (next section). Every field carries the exact text it was read from and a confidence of high, medium or low.
4. **Verify** (Python checks, then `gemini-4-argon` with `thinking_level: "high"` on failures). Code runs hard checks: net = gross − tare; invoice lines sum to the invoice net; VAT = 20% of net; dates fall in the invoice period; reg matches the UK format. A failed check sends the original image and the failed rule back to Argon to re-read. Two failed attempts → the review queue.
5. **Match** (pure Python, no model). Pair each delivery ticket with an invoice line using the rules in the next section. Money is never calculated by the model.
6. **Adjudicate** (`gemini-4-argon`, medium thinking). Only for ambiguous pairs (two candidate lines, a reg one character off). Argon picks one or says "no match" and writes one sentence of reasoning, shown in the UI.
7. **Audit pass** (`gemini-4-argon`, whole set in one context). Argon reads every invoice and ticket together and reports patterns no single row shows: a customer that drops Friday loads, a rate that changed mid-month without notice, a run of tickets with no receiver signature.
8. **Report and dispute.** Dashboard totals come from the match results. For each customer, Argon drafts a dispute email from the approved lines only, with a table of jobs and the ticket images attached as evidence. The user edits, approves and exports.

Check the exact Interactions API fields for image input and JSON schema output in the `gemini-api-dev` skill before you write the calls; the builder guide does not spell them out.

## What 51 real tickets changed

The 51 tickets (27 Aug–18 Sep 2026, seven trucks) come in seven layouts and break three first-draft assumptions: one ticket is one paid job, the reg is a reliable key, and printed totals belong to this load. They shape the design only; the demo still uses fictional look-alikes.

| Layout | Count | How it arrives | Main trap |
| --- | --- | --- | --- |
| Conway conveyance note | 27 | Dot-matrix, often photographed sideways | SENT and ORDERED are running totals for the order; use NET. The WCON ref repeats across loads, so key on the conveyance note number |
| Eurovia delivery note (ePOD) | 11 | Phone screenshot, clean | Tare often dated days earlier; arrival vs start of unloading shows waits of up to 3 hours |
| Eurovia waste transfer note | 8 | Handwritten | Return weights look estimated ("15.5 − 12.5 = 3.0" four times); units mixed ("14080 − 12300 = 1,780 TON"); linked to its delivery note by a handwritten M20AA ref |
| SRC ticket | 2 | Carbon copy on a clipboard | Net sometimes handwritten, tare blank |
| SIMS weighbridge ticket | 1 | Handwritten plus faded thermal print | Printed weights nearly unreadable |
| Mibau Stema POD | 1 | PDF | Haulier field names the customer, not Pak; three different dates on one job |
| Eurovia docket (old form) | 1 | Handwritten | Night job; docket and its WTN dated a day apart |

| Finding | Change to Tally |
| --- | --- |
| Eight WTNs are return legs; two full loads came back (17.56 t and 19.34 t) | A job has legs: delivery and return. Flag a return leg the self-bill doesn't pay |
| Recycled loads (Conway's Chelsfield runs, SRC "Recycled Out") are a fixed rate, while part loads of other material are simply paid on their weight | Classify recycled loads by material and check they get the fixed rate, not tonnes × miles |
| Daywork on Conway notes: "4hrs Daywork (Chute)", "1hrs Daywork", 3 h 55 on site | Apply the chute and non-chute rules in Pay rules |
| Night jobs at 00:45 and 22:51; weekend premiums | Shift calendar; flag night or weekend loads paid at the day rate |
| Same driver on three trucks; AYE/AYF misread; similar regs (S121PAK, B21PAK) | Eight-truck fleet list at set-up; snap each read to the nearest fleet reg; add driver as a match key |
| Night jobs cross midnight | Match on the load timestamp, ±1 day, not the printed date |
| Eurovia notes print "Loads Today: 9" | Gap check: loads the customer's own counter records with no ticket in the set become a "find this ticket" prompt |
| Internal transfers (Conway's Erith internal account) are never signed | Don't mark these as weak evidence |

**Open questions for you.**

- What is Eurovia's waiting rate, and how much free time comes before it starts?
- Do Eurovia and SRC pay return loads, and at what rates? The £55 and £100 rates are Conway's.

## Pay rules

Each self-bill line is priced one of four ways, and FM Conway, Eurovia and SRC each have their own rates, including separate night-shift and weekend rates.

| Work | How it's paid | Where Tally reads it |
| --- | --- | --- |
| Haulage | Tonnes × miles × the customer's rate per tonne-mile, at the day, night or weekend rate | Net weight; Conway MILEAGE or Eurovia radial miles; load time sets the shift |
| Recycled aggregate | Fixed rate per load, about £67.50, whatever the tonnage | Material description, e.g. recycled asphalt or 6F5 crushed hardcore |
| Shift guarantee | Each truck earns at least £200 per day shift and £350 per night or weekend shift; any shortfall is owed as a top-up | Everything the truck earned in the shift: loads, returns, waiting and daywork |
| Conway chute daywork | Full hours written on the ticket × £50/h | Daywork note, e.g. "4hrs Daywork (Chute)" |
| Conway non-chute daywork | (Hours on site − 30 min) × £40/h | Time on site to time off site |
| Conway part-load return | £55 for any return that isn't a full load | "Returned material" box or a WTN return weight |
| Conway full-load return | £100 | "FULL RETURN" note, or return weight equal to the load |
| Eurovia waiting | Charged at Eurovia's own rate (to confirm) | Ship-to arrival vs start of unloading |

**Shifts.** Night is 18:00 to 04:00 and day is 04:00 to 18:00. A night shift belongs to the date it starts, so a load at 01:30 on Tuesday counts towards Monday's night shift. Any shift on a Saturday or Sunday is a weekend shift. One customer has a truck for the whole day, so that customer owes the guarantee.

**Guarantee maths.** Tally corrects every line first, then compares everything the truck earned in the shift with the guarantee. The claim for a shift is the larger of the two minus what was paid. A missing load in a shift that was already topped up to £200 is therefore worth less than its face value, and Tally shows that reduction rather than overclaiming.

SRC tickets print no mileage, so Tally takes miles from the self-bill line when it shows them, and otherwise from a site-distance table you fill in once.

**Shift calendar.** Tally tags every load day, night or weekend and shows a month calendar per truck. Each day shows the truck's shifts, what it earned against its guarantee, and any night or weekend load paid at the day rate. The log exports as CSV for your records.

**Fleet, entered at set-up.** Eight trucks: K21PAK, S121PAK, E21PAK, E27PAK, G27PAK, NX66AYF, LX64AGU and B21PAK. K21PAK and S121PAK are separate trucks.

## Data model and matching rules

Three record types cover every document; matching runs from the ticket side, because a ticket is proof a job happened.

**Job** (one per ticket or work-log row): `ticket_no`, `date`, `vehicle_reg`, `customer`, `from_site`, `to_site`, `material`, `gross_kg`, `tare_kg`, `net_t`, `signatures {driver, receiver}`, `source_file`, `evidence[]` (field → quoted text + confidence).

**InvoiceLine**: `invoice_no`, `invoice_date`, `customer`, `job_date`, `ticket_no`, `vehicle_reg`, `description`, `tonnes`, `rate`, `net_amount`, `source_file`.

**RateCard** (one per customer, entered once): rate per tonne-mile for day, night and weekend work; the recycled fixed rate per load; Conway's daywork rates (£50/h chute, £40/h non-chute less 30 minutes).

**Matching rules, in order.** The first rule that finds exactly one line wins.

| Rule | Key | Tolerance | Result if it matches |
| --- | --- | --- | --- |
| 1 | Issuer's ticket number (Conway conveyance note, Eurovia delivery note, SRC ticket) | Exact, after stripping spaces, dashes and leading zeros | Matched |
| 2 | Load date + vehicle reg + driver | Date ±1 day on load timestamp; reg snapped to the fleet list (edit distance ≤ 1) | Matched, flagged "reg mismatch" if snapped |
| 3 | Date + customer + tonnage | Date ±1 day; tonnes ±0.5 t | Candidate → Argon adjudicates |
| none | — | — | **Missing from invoice** |

**Flags on matched pairs.**

| Flag | Test | Amount owed |
| --- | --- | --- |
| Missing job | No invoice line | Pay rule applied to the ticket |
| Short tonnage | Invoice tonnes < ticket net by more than 0.1 t | Difference × miles × rate |
| Short miles | Invoice miles < ticket miles | Difference × tonnes × rate |
| Low rate | Rate per tonne-mile below the rate card | Difference × tonnes × miles |
| Shift rate not paid | Night or weekend load paid at the day rate | Night or weekend amount − amount paid |
| Recycled wrong rate | Recycled load not paid the fixed rate | Fixed rate − amount paid |
| Guarantee not paid | Corrected earnings for a shift fall below £200 (day) or £350 (night or weekend), with no top-up | Guarantee − that total − top-up paid |
| Return not paid | Return on the ticket or WTN with no invoice line | £55 part load, £100 full load (Conway) |
| Full return paid as part | Full-load return paid at £55 | £45 |
| Daywork not paid | Conway daywork on the ticket, missing or short on the invoice | Chute: hours × £50; non-chute: (hours − 0.5) × £40 |
| Waiting not paid | Wait beyond free time with no invoice line | Billable hours × the customer's waiting rate |
| Missing ticket | Customer's "loads today" counter exceeds tickets held | £0; prompts a search for the ticket |
| Weak evidence | No receiver signature (except internal accounts), low-confidence field, or snapped reg | £0, but warned before claiming |
| Duplicate | Same ticket on two invoice lines | Shown as overpaid, for honesty |

Tolerances are settings, so a customer who rounds tonnage doesn't flood the list.

## Human control and privacy

Tally never sends anything or claims money on its own; the haulier approves every line that goes into a dispute.

- **Model reads, code counts.** Every £ figure comes from Python arithmetic on approved fields, so a hallucinated number cannot reach a total.
- **Evidence beside every flag.** Clicking a flagged job shows the ticket image with the read fields highlighted, the quoted text, and Argon's one-line reasoning.
- **Review queue.** Low-confidence fields and failed checks wait for a person to confirm or correct. Corrections are kept and re-run through matching.
- **Approve per line.** A dispute draft includes only lines ticked "claim". Weak-evidence lines are unticked by default.
- **No send button.** The pack is copied or downloaded as an email plus a PDF of evidence. A Gmail draft (never a send) is a stretch goal.
- **Data.** Demo data is fictional. Real use keeps files in the haulier's own storage, sends documents only to the Gemini API, and keeps the API key on the server. Never commit real tickets or invoices to the repo.

## Tech stack and repo layout

Use Python on the back end, because the matching and money logic is easiest to test there, and a single-page front end with no build step.

| Layer | Choice | Why |
| --- | --- | --- |
| Models | `google-genai` ≥ 2.3.0, Interactions API | Required by the guide; one API for models and agents |
| Back end | FastAPI + `asyncio.gather` for parallel extraction | Fast to write; parallel calls keep the run under a minute |
| Matching | pandas + `rapidfuzz` for reg distance | Deterministic, unit-testable |
| Storage | SQLite (one file) | Enough for a demo; easy to reset between runs |
| Front end | One HTML page, Tailwind CDN, vanilla JS | No build step to break on the day |
| Progress | Server-sent events | Judges watch each document tick through route → extract → verify |
| Export | Dispute email as text + evidence PDF (`reportlab`) | Nothing leaves without the user |
| Hosting | Run locally on the laptop; Cloud Run as a stretch goal | Venue Wi-Fi is the biggest risk |

```
tally/
  app.py              FastAPI routes + SSE progress
  agents/
    router.py         Flash-Lite classifier
    extract.py        Flash extraction subagents + schema
    verify.py         rule checks + Argon re-read loop
    adjudicate.py     Argon tie-breaks
    audit.py          Argon whole-set pattern pass
    dispute.py        Argon email drafting
  engine/
    match.py          matching rules (no model calls)
    money.py          owed amounts, ageing, totals
  tests/
    test_match.py     known-answer test on the demo set
  demo/               synthetic invoices, tickets, work log
  static/index.html   upload, dashboard, review, dispute
  AGENTS.md           quirks Argon solved during the build
```

`tests/test_match.py` is Argon's verification loop during the build: the demo set has a known answer, and the test fails until the pipeline finds it exactly.

## Build plan for the day

Build the money logic first and freeze features at 15:30, leaving 90 minutes to rehearse. Times assume a 9:00 start; shift them to the event's schedule.

&#91;embedded content: hackathon day · 6 phases, 5 gates\]

Each gate is a test, not a feeling: if `test_match.py` isn't green on all 20 files by 13:00, cut the audit pass and keep going.

## Demo script and demo data

The demo runs in 2 minutes 40 seconds and ends on one number the judges can check: £1,715.22 owed from 21 line errors and 3 unpaid guarantees.

| Time | On screen | You say |
| --- | --- | --- |
| 0:00–0:20 | Photo of a real (blurred) self-bill and a cab ticket | "Last summer I found £9,333 a haulage firm was owed, by hand, over weeks. Here's why it's hard." |
| 0:20–0:40 | Drop 20 files; photograph a paper ticket with your phone and add it | "Six self-bills, a work log, and the tickets. One is handwritten, live." |
| 0:40–1:20 | Progress board: each file routes, extracts, verifies; one ticket fails the net-weight check and Argon re-reads it | "Argon checks its own reading. This one said 2.6 t; gross minus tare says 2.0, so it went back and fixed it." |
| 1:20–1:50 | Dashboard: £1,715.22 owed, by customer, oldest first; the audit finding | "Six loads never invoiced, three guarantees unpaid, a full return paid as a part return. And where the guarantee already covered a gap, it takes £310.61 off rather than overclaiming." |
| 1:50–2:20 | Open one flag: ticket image beside the invoice line; tick lines; draft dispute | "It drafts, I approve. Nothing is sent." |
| 2:20–2:40 | Copy email + evidence PDF | "Every subcontractor paid by self-bill needs this: haulage, construction, couriers." |

**Demo data (all fictional).** Haulier: Kestrel Haulage Ltd, four trucks (KX21 HLG, KX19 BRT, LN70 TMP, KX68 DWV). Customers: Greystone Aggregates, Marsh Lane Surfacing, Hollins Quarry Products. August–September 2026.

- Work log: 350 rows across 133 truck-shifts (316 loads, 17 waiting claims, 11 daywork claims, 6 returns) with miles, shift and pay basis, plus a rate card (already generated as CSV)
- Six self-bill PDFs rendered from the invoice CSV, with 11 lines left out (6 loads, 2 daywork, 2 waiting, 1 return), 10 paid short (2 tonnage, 2 miles, 3 night or weekend rate, 2 recycled loads at an old flat rate, 1 full return paid as part) and 3 guarantee top-ups forgotten
- Two invoice lines with a reg one character off, to show rule 2. The audit pass has a real pattern to find: two of the three forgotten guarantee top-ups are night shifts on the same truck, KX68 DWV
- About 12 ticket images in three fictional look-alike layouts: a dot-matrix conveyance note with running-total columns, a clean ePOD screenshot, and a handwritten waste transfer note for a return leg. Fill them with fake data and photograph them in a cab, one sideways. Include one part load, one return leg, one 2-hour wait and one ticket missing from the invoice.

Plant one deliberate read error: write a 6 that looks like a 0 on one ticket, so the net-weight check catches it live.

## Risks and fallbacks

The two risks that sink live demos are slow calls and bad Wi-Fi; both have a fallback ready before judging.

| Risk | Fallback |
| --- | --- |
| Argon high-thinking re-read takes too long on stage | Cap re-reads at 2; run `high` only on failed checks; keep an HTTP timeout of 15 minutes off the stage path; show a spinner per file, not a blank screen |
| Venue Wi-Fi drops | Phone hotspot; a "replay last run" button that streams a saved run from SQLite at real speed |
| Extraction misreads a key field | Verification loop + review queue; rehearse the demo set until `test_match.py` passes 5 runs in a row |
| Judges ask "why not just a spreadsheet?" | Handwritten WTNs and cab photos don't arrive as spreadsheets; show the AYE/AYF reg case |
| Judges ask about trusting AI with money | "The model reads and explains; code counts; a person approves." |
| Running out of time | Cut in this order: audit pass, Gmail draft, rate card upload (infer rates instead). Never cut verify or the dispute draft |
| Legacy parameters break calls | No `temperature`, `top_p`, `top_k` or `thinking_budget` with `gemini-4-argon`; install the Gemini API skills first |

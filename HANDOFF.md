# HANDOFF — state as of 2026-10-27

## Status
Complete, locally runnable prototype. All P0 and P1 items from `02_BUILD_AND_HANDOFF.md` are implemented. P2 (real Meta send, real POS, deployment) is not.

## What was actually run and verified
- `python -m pytest -q tests` → **42 passed** (Python 3.13, FastAPI 0.162, Starlette 1.10, uvicorn 0.54). Covers: seed counts (60 customers, 313 bills), A/B benchmarks (2.25x/25 days overdue/high priority; 0.61x/on track), reset repeatability, confidence labels, same-day bills, duplicate bill IDs, recalculation after new bill, phone validation, exclusions (opt-out/unknown/invalid/cooldown), demo contacts never live-eligible, caps and cost maths, simulation end-to-end + no double dispatch, restart recovery, live blocked without config, Meta sender with **mocked** HTTP (timeout→UNKNOWN, token never leaked), CSV preview/commit/idempotency, attribution, every route.
- Real server run (`uvicorn`) and viewed in headless Chromium: dashboard, customers, profile, new-campaign pages rendered; no console errors; fixed a mobile horizontal-overflow bug on the dashboard (re-measured: none at 390px width on the dashboard and customers pages).
- Live scheduler thread verified: a campaign scheduled for a past minute went Scheduled → Done (5 × SIMULATED) within one worker cycle.
- (Verified) ZIP was unzipped into a clean directory, a fresh venv installed from requirements.txt, and all 42 tests passed there.

## NOT verified
- Any real WhatsApp/Meta call. `MetaCloudApiSender` is untested against the real API; payload shape follows Meta's documented template-message format from memory — re-check against current Meta docs before first live test.
- Mobile layout of pages other than dashboard/customers beyond the overflow check; dark mode visually; Windows/macOS run path; Python <3.13.

## Next actions (in order)
1. Client meeting: ask the 5 discovery questions in `02_BUILD_AND_HANDOFF.md` §8 (billing software, export fields, consent capture, existing WABA, volumes).
2. Obtain Meta credentials + an approved **marketing** template + one opted-in test number → put in `.env` (never in chat) → test one live send; add webhook for delivered/read.
3. Build the real POS import once the export format is known (add a column preset in `ledger.guess_mapping`; XLSX via openpyxl).
4. Optional: second business-type dataset (salon/café), chart polish, auth if it will be hosted.

## Resume prompt for a fresh chat
> I'm continuing ReturnLoop (FastAPI + SQLite, in `returnloop/`). Read `HANDOFF.md` and `PROJECT_NOTES.md`, run `python -m pytest -q tests`, then do next action #__ . Don't regenerate the project; make targeted changes only.

## Quick reference
Run: `uvicorn app.main:app --port 8000` · Reset demo: Settings → type RESET · Scheduler off for tests: `RETURNLOOP_NO_SCHEDULER=1`.

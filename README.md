# ReturnLoop — Customer Retention & Campaign Manager (prototype)

Works **beside** a shop's billing/POS software. It learns each customer's own purchase rhythm, flags who is
overdue, ranks who is worth contacting, and prepares targeted WhatsApp offers with the cost shown first.
First demo: a fictional clothing shop. **Everything is demo data and WhatsApp sending is simulated by default.**

## Run it (local, no accounts needed)

Prerequisite: Python 3.11+ (developed and tested on 3.13).

```bash
cd returnloop
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                 # optional; app runs fine without it
uvicorn app.main:app --port 8000
```
Open http://localhost:8000. On first start the database (`data/returnloop.db`) is created and seeded with
60 fictional customers and 313 bills. No login (single-user prototype).

## 5–10 minute demo path
1. **Overview** – KPIs, priority list with plain-language reasons. *Aarav Mehta* (Customer A: usual 20 days, 45 days away = **2.25x, 25 days overdue, high priority**) vs *Bhavna Rao* (Customer B: usual 90 days, 55 days away = **0.61x, on track**).
2. **Customers** – search, segment filter, open a profile (visit-rhythm chart, bills, spend, history).
3. **Add customer / Add bill** – saves instantly; metrics, segments and dashboard recalculate. Duplicate bill IDs are rejected.
4. **Segments** – every rule visible, eligible counts and cost.
5. **Campaigns → New** – choose segment, cap recipients/budget, edit the template, see WhatsApp-style previews of *real* customers, included/excluded with reasons, cost, then **Run simulation**.
6. **Campaign page** – per-customer history (`SIMULATED`, never "sent") and *observed returns after campaign*.
7. **Import bills** – upload `/import/sample.csv`, map columns, preview, import; re-import shows duplicates skipped.

Reset the demo any time: **Settings → Reset demo data** (type `RESET`). Results are repeatable.

## CSV import format
Required per row: customer **phone**, **date** (ISO, `dd/mm/yyyy`, `dd-mm-yyyy`, `dd Mon yyyy`, optional time), **net amount**
(negative = return). Optional: customer name, bill/invoice ID, item/category. Columns are mapped in the UI (auto-guessed).
Rows with a bill ID already in the database (or repeated in the file) are skipped, so re-imports are safe. Rows without a
valid phone are saved to an *unmatched* list (not campaign-eligible) that you can assign by hand. New customers from CSV
start with consent = Unknown. Excel: save as CSV first (XLSX not supported yet).

## Environment variables (`.env`, see `.env.example`)
`APP_ENV`, `DATABASE_URL`, `WHATSAPP_MODE` (`simulation`|`live`), `META_ACCESS_TOKEN`, `META_PHONE_NUMBER_ID`,
`META_WABA_ID`, `META_APP_SECRET`, `META_WEBHOOK_VERIFY_TOKEN`, `META_API_VERSION`, `MESSAGE_RATE_INR` (default 1.20),
`MESSAGE_RATE_EFFECTIVE_DATE`, `GST_PERCENT`. Meta values are read server-side only and never shown in the UI or logs.
Live sending stays disabled until `WHATSAPP_MODE=live` **and** all four of token / phone-number-id / WABA-id / API version are set.

## Tests
```bash
python -m pytest -q tests        # 42 tests: engine, seed, campaigns, senders (mocked HTTP), CSV, all routes
```

## Current limitations (be upfront with the client)
- **WhatsApp is simulated.** `MetaCloudApiSender` is written against Meta's Cloud API but has **never been run against the real API** (needs credentials, an approved marketing template and an opted-in test recipient). Demo contacts can never be live-send recipients.
- ₹1.20/message is a **planning assumption, not a verified tariff** — check the current Meta/provider rate card.
- Scheduled campaigns run only **while the app is running** (worker checks every ~20 s); not a 24/7 service. After a restart, due campaigns run once (no duplicates).
- No real POS connection (vendor unknown); CSV/manual entry only. No XLSX. No login/RBAC/CSRF, single user, local SQLite — fine for a demo, not production.
- Delivery/read receipts (Meta webhooks) are not implemented; live statuses stop at `SUBMITTED`/`UNKNOWN`/`FAILED`.
- Observed returns are correlations, not proof a campaign caused a purchase.

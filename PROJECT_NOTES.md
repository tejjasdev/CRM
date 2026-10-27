# PROJECT_NOTES — architecture & decisions

## Stack (and why)
FastAPI + server-rendered Jinja templates + SQLite (stdlib `sqlite3`), plain CSS, no JS framework, no build step,
no external CDN/fonts (works offline). One process, one DB file, `pip install` and run. Chosen over React/Vite
because the demo needs forms, tables and filters, not client-side state; fewer moving parts = fewer demo-day failures.

## Layout
```
app/main.py            routes, scheduler thread, startup (init/seed/recovery)
app/db.py              schema, settings defaults, helpers
app/config.py          .env loading; meta_config() (server only) / live_readiness() (booleans only)
app/utils.py           INR/Indian-grouping formatting, phone normalisation
app/services/engine.py   metrics, cadence, status, segments, priority, eligibility, dashboard, attribution
app/services/ledger.py   add customer/bill, duplicate checks, CSV parse/preview/import, unmatched assignment
app/services/campaigns.py audience, cost, caps, create, dispatch (atomic claim), recovery, cancel
app/services/senders.py  SimulatedSender, MetaCloudApiSender
app/services/render.py   whitelisted {{placeholder}} templating
app/services/seed.py     deterministic fictional data, relative to today
tests/                   test_core.py (services), test_web.py (routes)
```

## Data model (SQLite)
`customers`(phone normalised `91XXXXXXXXXX` UNIQUE, consent Yes/No/Unknown, is_demo) ·
`transactions`(customer_id NULL = unmatched, bill_id UNIQUE NOCASE when present, amount net/negative=return,
source, import_id, possible_dup) · `imports` · `campaigns`(mode SIMULATION|LIVE_META, status, frozen rate/markup/GST,
est cost) · `campaign_recipients`(rendered_text, status, provider id, error; UNIQUE per campaign+customer) ·
`activity` · `settings`(key/value). Metrics are **never stored**: recomputed from transactions on every request
(60 customers → instant; add caching/indexed aggregates if customer count grows to tens of thousands).

## Formulas
- **bill** = valid txn with amount > 0; **return** = amount < 0 (reduces spend, not a bill/visit); **visit** = distinct date with ≥1 bill.
- `lifetime_spend` = sum of valid net amounts; `average_bill` = gross bills ÷ bill count; `repeat rate` = customers with ≥2 visits ÷ customers with ≥1 visit.
- **usual interval** = median of the last 5 inter-visit gaps; with exactly 2 visits (one gap) = 0.5·gap + 0.5·baseline (labelled *Preliminary*). 0–1 visit → no personal interval.
- `overdue_ratio = days_since_last_visit ÷ usual`; `days_overdue = days_since − usual`.
- **Confidence**: 0 visits *No purchase history*, 1 *Insufficient history*, 2 *Preliminary*, 3 *Moderate*, 4+ *Higher confidence*.
- **Status** (thresholds editable): ratio <1.1 On track · 1.1–<1.5 Due soon · 1.5–<2.0 Overdue · ≥2.0 High priority · Inactive if ratio ≥3.0, or (≥120 days AND ratio ≥1.5). One-time buyers: Inactive after 120 days, "second-visit window" 7–45 days.
- **Declining**: ≥4 visits and latest gap ≥1.5× median of earlier gaps. **VIP**: spend ≥ ₹25,000 and ≥3 visits.
- **Priority score (0–100, only for actionable customers)** = overdue (ratio capped at 3 → up to 50; ×0.6 if lapsed; flat 15 for second-visit window) + value (spend/VIP threshold, up to 30) + frequency (visits capped at 8 → up to 12) + declining (8). Components are shown on the profile.
- **Eligible** = consent Yes ∧ valid phone ∧ not in cooldown ∧ actionable. LIVE additionally excludes `is_demo`.
- **Cost** = n × (rate + markup), plus GST on that. Default ₹1.20 is a planning placeholder. Budget cap compares to the GST-inclusive per-message figure.
- **Attribution**: bills (>0) by recipients dated after dispatch and within N days (default 14), labelled "observed returns", never "caused by".

## Campaign / sender design
`create_campaign` freezes the audience (recipients QUEUED with rendered text) → `dispatch_campaign` atomically flips
SCHEDULED→RUNNING (`UPDATE … WHERE status='SCHEDULED'`, rowcount check) so it cannot dispatch twice, re-validates each
recipient at send time, commits per recipient. Immediate and scheduled runs use the same path; a daemon thread polls every 20 s.
Restart recovery: RUNNING simulation → resumes only QUEUED rows; RUNNING live → QUEUED rows become `UNKNOWN` (never auto-resent).
`MetaCloudApiSender`: POST `/{version}/{phone_number_id}/messages`, template message; 2xx+message id → `SUBMITTED`; timeout/network/5xx → `UNKNOWN`
(no retry); 4xx → `FAILED`; token scrubbed from any stored error. Statuses DELIVERED/READ are defined but need webhooks (deferred).
Cooldown counts simulated sends too, so a repeated demo run correctly shows everyone in cooldown (use Reset demo data).

## Decisions / departures from the brief
- Added a **Second-visit window** segment (12th) for one-time buyers, per the brief's "second-visit encouragement" note.
- Unknown-consent customers are excluded in **simulation too** (not only live) so the demo shows the real rule.
- Returns (negative bills) supported; same-day bills = separate bills, one visit; no-bill-ID duplicates are flagged for review, not merged.
- CSV: stored upload (hash-named) between preview and commit; XLSX deferred.
- No auth/CSRF (local single-user prototype). Settings reset by Reset demo.

## Deferred
Real Meta verification + webhooks for delivery/read; Meta template listing/validation; real POS connector; XLSX; second business-type dataset;
auth/RBAC; always-on worker/deployment; backups; charts beyond the two on the dashboard.

## Change log
- 2026-10-27: first complete prototype (P0 + CSV import, scheduling, caps, attribution, settings).

"""ReturnLoop - FastAPI app (server-rendered). Run: uvicorn app.main:app"""
from __future__ import annotations

import os
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, db
from .services import campaigns as camp_svc, engine, ledger, render, seed
from .utils import display_phone, fmt_date, fmt_dt, fmt_inr, normalize_phone

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.filters["inr"] = fmt_inr
templates.env.filters["inr2"] = lambda v: fmt_inr(v, 2)
templates.env.filters["dt"] = fmt_dt
templates.env.filters["d"] = fmt_date
templates.env.filters["phone"] = lambda s: display_phone(s)
templates.env.globals["SEGMENTS"] = engine.SEGMENTS
templates.env.globals["SEG_LABEL"] = engine.SEGMENT_LABEL
templates.env.globals["STATUS_LABEL"] = engine.STATUS_LABEL
templates.env.globals["ALLOWED_PLACEHOLDERS"] = render.ALLOWED

_stop = threading.Event()


def _scheduler_loop():
    while not _stop.is_set():
        try:
            with db.get_conn() as c:
                for cid in camp_svc.due_scheduled(c):
                    camp_svc.dispatch_campaign(c, cid)
        except Exception as e:      # keep the worker alive; error is visible in server log
            print("scheduler error:", type(e).__name__, e)
        _stop.wait(20)


@asynccontextmanager
async def lifespan(app: FastAPI):
    with db.get_conn() as c:
        db.init_db(c)
        camp_svc.recover_after_restart(c)
        has = c.execute("SELECT COUNT(*) n FROM customers").fetchone()["n"]
        flag = c.execute("SELECT value FROM settings WHERE key='demo_seeded'").fetchone()
        if has == 0 and not flag:
            seed.seed(c)
            db.set_setting(c, "demo_seeded", "1")
    t = None
    if not os.environ.get("RETURNLOOP_NO_SCHEDULER"):
        t = threading.Thread(target=_scheduler_loop, daemon=True, name="scheduler")
        t.start()
    yield
    _stop.set()


app = FastAPI(title="ReturnLoop", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


# ------------------------------------------------------------------ helpers
def page(request: Request, name: str, conn, active: str, status_code: int = 200, **kw):
    s = db.get_settings(conn)
    ready = config.live_readiness()
    ctx = {"settings": s, "active": active, "live_ready": ready, "flash": request.query_params.get("msg"),
           "flash_kind": request.query_params.get("kind", "ok"), "today": date.today(),
           "data_source": s.get("data_source", "Demo data"), "data_updated": s.get("data_updated", ""), **kw}
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def redirect(url: str, msg: str = "", kind: str = "ok"):
    if msg:
        from urllib.parse import quote
        url += ("&" if "?" in url else "?") + f"msg={quote(msg)}&kind={kind}"
    return RedirectResponse(url, status_code=303)


def _stats(conn):
    s = db.get_settings(conn)
    return s, engine.load_all(conn, s)


# ------------------------------------------------------------------ dashboard
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, period: int = 30):
    period = period if period in (30, 90, 365) else 30
    with db.get_conn() as c:
        s, st = _stats(c)
        d = engine.dashboard(c, st, s, period)
        queue = [x for x in st.values() if x["actionable"]]
        queue.sort(key=lambda x: (-x["contactable"], -x["priority_score"]))
        queue = sorted(queue, key=lambda x: (-x["priority_score"]))[:8]
        counts = engine.segment_counts(st)
        activity = c.execute("SELECT * FROM activity ORDER BY id DESC LIMIT 8").fetchall()
        recent_c = c.execute("SELECT * FROM campaigns ORDER BY id DESC LIMIT 4").fetchall()
        attr = {r["id"]: engine.campaign_attribution(c, r["id"]) for r in recent_c}
        total_observed = sum(a["returns"] for a in (engine.campaign_attribution(c, r["id"]) for r in
                             c.execute("SELECT id FROM campaigns WHERE status IN ('DONE','DONE_WITH_ERRORS')")))
        months = _monthly_sales(c)
        status_mix = _status_mix(st)
        return page(request, "dashboard.html", c, "dashboard", d=d, queue=queue, counts=counts, activity=activity,
                    recent_c=recent_c, attr=attr, observed=total_observed, months=months, mix=status_mix, period=period)


def _monthly_sales(conn, n=6):
    today = date.today()
    out = []
    y, m = today.year, today.month
    keys = []
    for _ in range(n):
        keys.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    keys.reverse()
    for y, m in keys:
        r = conn.execute("SELECT COALESCE(SUM(amount),0) s FROM transactions WHERE status='valid' AND "
                         "customer_id IS NOT NULL AND substr(txn_date,1,7)=?", (f"{y}-{m:02d}",)).fetchone()
        out.append({"label": date(y, m, 1).strftime("%b"), "value": float(r["s"])})
    mx = max([o["value"] for o in out] + [1])
    for o in out:
        o["h"] = round(o["value"] / mx * 100)
    return out


def _status_mix(stats):
    order = [("ON_TRACK", "On track"), ("DUE_SOON", "Due soon"), ("OVERDUE", "Overdue"),
             ("HIGH_PRIORITY", "High priority"), ("INACTIVE", "Lapsed"), ("ONE_TIME", "One-time"), ("NO_HISTORY", "No bills")]
    tot = max(len(stats), 1)
    return [{"key": k, "label": l, "n": sum(1 for s in stats.values() if s["status"] == k),
             "pct": sum(1 for s in stats.values() if s["status"] == k) / tot * 100} for k, l in order]


# ------------------------------------------------------------------ customers
@app.get("/customers", response_class=HTMLResponse)
def customers(request: Request, q: str = "", segment: str = "all", sort: str = "priority", page_n: int = 1):
    with db.get_conn() as c:
        s, st = _stats(c)
        seg = segment if segment in engine.SEGMENT_LABEL else "all"
        rows = [x for x in st.values() if x["segments"][seg]]
        if q.strip():
            ql = q.strip().lower()
            qd = re.sub(r"\D", "", q)
            rows = [x for x in rows if ql in x["name"].lower() or (qd and x["phone"] and qd in x["phone"])]
        keys = {"priority": lambda x: (-x["priority_score"], x["name"].lower()),
                "spend": lambda x: (-x["lifetime_spend"], x["name"].lower()),
                "last_visit": lambda x: (x["recency_days"] is None, x["recency_days"] or 0),
                "name": lambda x: x["name"].lower()}
        rows.sort(key=keys.get(sort, keys["priority"]))
        per = 25
        total = len(rows)
        pages = max((total + per - 1) // per, 1)
        page_n = min(max(page_n, 1), pages)
        return page(request, "customers.html", c, "customers", rows=rows[(page_n - 1) * per: page_n * per], total=total,
                    q=q, segment=seg, sort=sort, page_n=page_n, pages=pages, counts=engine.segment_counts(st))


@app.get("/customers/new", response_class=HTMLResponse)
def customer_new(request: Request):
    with db.get_conn() as c:
        return page(request, "customer_form.html", c, "customers", errors=[], form={"consent": "Unknown"})


@app.post("/customers/new", response_class=HTMLResponse)
def customer_create(request: Request, name: str = Form(""), phone: str = Form(""), email: str = Form(""),
                    notes: str = Form(""), tags: str = Form(""), consent: str = Form("Unknown")):
    with db.get_conn() as c:
        cid, errs = ledger.add_customer(c, name, phone, email, notes, tags, consent)
        if errs:
            return page(request, "customer_form.html", c, "customers", status_code=422, errors=errs,
                        form=dict(name=name, phone=phone, email=email, notes=notes, tags=tags, consent=consent))
        return redirect(f"/customers/{cid}", f"Customer {name.strip()} saved.")


@app.get("/customers/{cid}", response_class=HTMLResponse)
def customer_profile(request: Request, cid: int):
    with db.get_conn() as c:
        s, st = _stats(c)
        cu = st.get(cid)
        if not cu:
            return page(request, "notfound.html", c, "customers", status_code=404, what="Customer")
        txns = c.execute("SELECT t.*, i.filename FROM transactions t LEFT JOIN imports i ON i.id=t.import_id "
                         "WHERE t.customer_id=? ORDER BY t.txn_date DESC, t.id DESC", (cid,)).fetchall()
        months: dict[str, dict] = {}
        for t in txns:
            if t["status"] != "valid":
                continue
            k = t["txn_date"][:7]
            months.setdefault(k, {"spend": 0.0, "bills": 0})
            months[k]["spend"] += t["amount"]
            months[k]["bills"] += 1 if t["amount"] > 0 else 0
        hist = c.execute("""SELECT cr.*, c.name AS cname, c.mode, c.dispatched_at, c.created_at AS ccreated
                            FROM campaign_recipients cr JOIN campaigns c ON c.id=cr.campaign_id
                            WHERE cr.customer_id=? ORDER BY cr.id DESC""", (cid,)).fetchall()
        gap_chart = None
        if cu["intervals"]:
            gaps = cu["intervals"][-12:]
            mx = max(gaps + [cu["recency_days"] or 0, cu["usual_interval"] or 0, 1])
            gap_chart = {"gaps": [{"v": g, "h": round(g / mx * 100)} for g in gaps],
                         "now": {"v": cu["recency_days"], "h": round((cu["recency_days"] or 0) / mx * 100)},
                         "usual_h": round((cu["usual_interval"] or 0) / mx * 100)}
        return page(request, "customer.html", c, "customers", cu=cu, txns=txns,
                    months=sorted(months.items(), reverse=True)[:12], hist=hist, gap_chart=gap_chart)


@app.post("/customers/{cid}/consent")
def customer_consent(cid: int, consent: str = Form(...)):
    with db.get_conn() as c:
        ok = ledger.update_consent(c, cid, consent)
    return redirect(f"/customers/{cid}", "Consent updated." if ok else "Invalid consent value.", "ok" if ok else "err")


# ------------------------------------------------------------------ transactions
def _resolve_customer(conn, text: str):
    text = (text or "").strip()
    if not text:
        return None
    norm = normalize_phone(re.sub(r"^.*[·|—-]\s*(?=[+\d])", "", text)) or normalize_phone(text)
    if norm:
        r = conn.execute("SELECT id FROM customers WHERE phone=?", (norm,)).fetchone()
        if r:
            return r["id"]
    base = re.split(r"\s[·|—]\s", text)[0].strip().lower()
    rows = conn.execute("SELECT id FROM customers WHERE lower(name)=?", (base,)).fetchall()
    return rows[0]["id"] if len(rows) == 1 else None


@app.get("/transactions/new", response_class=HTMLResponse)
def txn_new(request: Request, customer: int | None = None):
    with db.get_conn() as c:
        pre = ""
        if customer:
            r = c.execute("SELECT name, phone FROM customers WHERE id=?", (customer,)).fetchone()
            if r:
                pre = f"{r['name']} · {display_phone(r['phone'])}"
        return page(request, "txn_form.html", c, "transactions", errors=[], form={"customer_q": pre, "source": "manual",
                    "when": datetime.now().strftime("%Y-%m-%dT%H:%M")}, options=_cust_options(c))


def _cust_options(conn):
    return [f"{r['name']} · {display_phone(r['phone'])}" for r in
            conn.execute("SELECT name, phone FROM customers ORDER BY name").fetchall()]


@app.post("/transactions/new", response_class=HTMLResponse)
def txn_create(request: Request, customer_q: str = Form(""), new_name: str = Form(""), new_phone: str = Form(""),
               when: str = Form(""), amount: str = Form(""), bill_id: str = Form(""), category: str = Form(""),
               items: str = Form(""), source: str = Form("manual"), no_phone: str = Form("")):
    form = dict(customer_q=customer_q, new_name=new_name, new_phone=new_phone, when=when, amount=amount,
                bill_id=bill_id, category=category, items=items, source=source, no_phone=no_phone)
    with db.get_conn() as c:
        errs = []
        cid = None
        created_new = False
        if customer_q.strip():
            cid = _resolve_customer(c, customer_q)
            if cid is None:
                errs.append("No unique customer matches that search. Pick from the list, or use 'New customer' below.")
        elif new_phone.strip() or new_name.strip():
            norm = normalize_phone(new_phone)
            ex = c.execute("SELECT id FROM customers WHERE phone=?", (norm,)).fetchone() if norm else None
            if ex:
                cid = ex["id"]
            else:
                cid, e = ledger.add_customer(c, new_name, new_phone, source="manual")
                errs += e
                created_new = not e
        elif not no_phone:
            errs.append("Choose a customer, create a new one, or tick 'No phone available' to save to the review list.")
        dt = ledger.parse_dt(when.replace("T", " "))
        if source not in ("manual", "pos_entry"):
            source = "manual"
        if not errs:
            _, e = ledger.add_transaction(c, cid, dt, amount, bill_id, category, items, source,
                                          raw_name=new_name or None, raw_phone=new_phone or None)
            errs += e
        if errs:
            c.rollback()
            return page(request, "txn_form.html", c, "transactions", status_code=422, errors=errs, form=form,
                        options=_cust_options(c))
        if cid is None:
            return redirect("/import", "Bill saved to the unmatched review list (not campaign-eligible).")
        return redirect(f"/customers/{cid}", "Bill saved. Metrics and segments recalculated.")


# ------------------------------------------------------------------ segments
@app.get("/segments", response_class=HTMLResponse)
def segments(request: Request):
    with db.get_conn() as c:
        s, st = _stats(c)
        settings = s
        info = []
        for key, label, rule in engine.SEGMENTS:
            aud = camp_svc.build_audience(st, settings, key)
            info.append({"key": key, "label": label, "rule": rule, "n": len(engine.segment_members(st, key)),
                         "eligible": len(aud["included"]), "cost": aud["cost"]["total"]})
        return page(request, "segments.html", c, "segments", info=info)


# ------------------------------------------------------------------ import
@app.get("/import", response_class=HTMLResponse)
def import_home(request: Request):
    with db.get_conn() as c:
        return page(request, "import.html", c, "import", **_import_ctx(c))


def _import_ctx(c, **extra):
    return {"history": c.execute("SELECT * FROM imports ORDER BY id DESC LIMIT 10").fetchall(),
            "unmatched": c.execute("SELECT * FROM transactions WHERE customer_id IS NULL ORDER BY txn_date DESC LIMIT 50").fetchall(),
            "dups": c.execute("SELECT t.*, cu.name FROM transactions t JOIN customers cu ON cu.id=t.customer_id "
                              "WHERE t.possible_dup=1 ORDER BY t.txn_date DESC LIMIT 25").fetchall(),
            "fields": ledger.FIELDS, "preview": None, "errors": [], **extra}


@app.post("/import/preview", response_class=HTMLResponse)
async def import_preview(request: Request, file: UploadFile = File(...)):
    raw = await file.read()
    with db.get_conn() as c:
        if not raw or len(raw) > 5_000_000:
            return page(request, "import.html", c, "import", status_code=422,
                        **_import_ctx(c, errors=["Choose a non-empty CSV file (max 5 MB)."]))
        headers, rows = ledger.parse_csv(raw)
        if not headers or not rows:
            return page(request, "import.html", c, "import", status_code=422,
                        **_import_ctx(c, errors=["Could not read any rows. Is it a CSV with a header row?"]))
        h = ledger.save_upload(raw)
        mapping = ledger.guess_mapping(headers)
        pv = ledger.process_rows(c, rows, mapping, write=False)
        c.rollback()
        return page(request, "import.html", c, "import", **_import_ctx(
            c, preview=pv, headers=headers, mapping=mapping, file_hash=h, filename=file.filename or "upload.csv"))


@app.post("/import/commit", response_class=HTMLResponse)
async def import_commit(request: Request):
    f = await request.form()
    h, fn = f.get("file_hash", ""), f.get("filename", "upload.csv")
    mapping = {k: f.get(f"map_{k}") for k, _, _ in ledger.FIELDS if f.get(f"map_{k}")}
    with db.get_conn() as c:
        raw = ledger.load_upload(h)
        errs = []
        if raw is None:
            errs.append("Upload expired - please choose the file again.")
        for k, label, req in ledger.FIELDS:
            if req and k not in mapping:
                errs.append(f"Map a column for '{label}'.")
        headers, rows = ledger.parse_csv(raw) if raw else ([], [])
        if any(v not in headers for v in mapping.values()):
            errs.append("Mapping refers to an unknown column.")
        if errs:
            return page(request, "import.html", c, "import", status_code=422, **_import_ctx(c, errors=errs))
        if f.get("action") == "preview":
            pv = ledger.process_rows(c, rows, mapping, write=False)
            c.rollback()
            return page(request, "import.html", c, "import", **_import_ctx(
                c, preview=pv, headers=headers, mapping=mapping, file_hash=h, filename=fn))
        res = ledger.process_rows(c, rows, mapping, write=True, filename=fn, file_hash=h)
        return page(request, "import.html", c, "import", **_import_ctx(c, result=res))


@app.post("/import/assign/{txn_id}")
def import_assign(txn_id: int, phone: str = Form(""), name: str = Form("")):
    with db.get_conn() as c:
        errs = ledger.assign_unmatched(c, txn_id, phone, name)
    return redirect("/import", errs[0] if errs else "Bill assigned to customer.", "err" if errs else "ok")


@app.get("/import/sample.csv")
def sample_csv():
    from fastapi.responses import Response
    body = ("Mobile,Bill Date,Total,Invoice No,Customer,Item\n"
            "98100 11111,2026-09-02,2450,SAMPLE-001,Ravi Test,Shirts\n"
            "98100 11111,2026-10-01,1800,SAMPLE-002,Ravi Test,Jeans\n"
            "98100 22222,2026-10-05,3200,SAMPLE-003,Sita Test,Kurtas\n"
            "98100 33333,05/10/2026,900,SAMPLE-004,Mohan Test,Accessories\n")
    return Response(body, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=returnloop_sample.csv"})


# ------------------------------------------------------------------ campaigns
DEFAULT_TEMPLATE = ("Hi {{name}}, it's been {{days_since_last_visit}} days since your last visit to {{shop_name}}. "
                    "Here's {{offer}} - use code {{offer_code}} before {{expiry}}.")


@app.get("/campaigns", response_class=HTMLResponse)
def campaigns_list(request: Request):
    with db.get_conn() as c:
        rows = c.execute("SELECT * FROM campaigns ORDER BY id DESC").fetchall()
        info = []
        for r in rows:
            n = {x["status"]: x["n"] for x in c.execute(
                "SELECT status, COUNT(*) n FROM campaign_recipients WHERE campaign_id=? GROUP BY status", (r["id"],))}
            info.append({"c": r, "n": n, "attr": engine.campaign_attribution(c, r["id"])})
        return page(request, "campaigns.html", c, "campaigns", info=info,
                    month_spend=camp_svc.month_spend(c))


def _form_defaults(settings, segment="offer_eligible"):
    return {"name": "", "segment": segment, "offer": "10% off your next bill", "offer_code": "COMEBACK10",
            "expiry": (date.today() + timedelta(days=14)).isoformat(), "min_bill": "", "template": DEFAULT_TEMPLATE,
            "mode": "SIMULATION", "max_recipients": "", "budget_cap": "", "scheduled_at": "", "send_when": "now",
            "meta_template_name": "", "meta_language": "en", "meta_params": "name,offer,offer_code,expiry"}


@app.get("/campaigns/new", response_class=HTMLResponse)
def campaign_new(request: Request, segment: str = "offer_eligible"):
    with db.get_conn() as c:
        s, st = _stats(c)
        seg = segment if segment in engine.SEGMENT_LABEL else "offer_eligible"
        form = _form_defaults(s, seg)
        return page(request, "campaign_new.html", c, "campaigns", form=form, errors=[], prev=_preview(c, st, s, form))


def _preview(conn, st, settings, form):
    mode = "LIVE_META" if form.get("mode") == "LIVE_META" else "SIMULATION"
    mr = int(form["max_recipients"]) if str(form.get("max_recipients") or "").isdigit() else None
    try:
        bc = float(form.get("budget_cap") or 0) or None
    except ValueError:
        bc = None
    aud = camp_svc.build_audience(st, settings, form["segment"], mode, mr, bc)
    cdict = {"offer": form.get("offer"), "offer_code": form.get("offer_code"), "expiry": form.get("expiry"),
             "min_bill": form.get("min_bill")}
    samples = []
    for s in aud["included"][:5]:
        samples.append({"c": s, "text": render.render(form["template"], render.values_for(s, cdict, settings))})
    budget_cap = float(settings.get("monthly_budget_cap") or 0)
    return {"aud": aud, "samples": samples, "unknown": render.unknown_placeholders(form["template"]),
            "month_spend": camp_svc.month_spend(conn), "monthly_cap": budget_cap,
            "excluded_rows": aud["excluded"][:40]}


@app.post("/campaigns/new", response_class=HTMLResponse)
async def campaign_post(request: Request):
    f = dict((await request.form()).items())
    form = {**_form_defaults({}), **{k: (v if isinstance(v, str) else "") for k, v in f.items()}}
    action = f.get("action", "preview")
    with db.get_conn() as c:
        s, st = _stats(c)
        if form["segment"] not in engine.SEGMENT_LABEL:
            form["segment"] = "offer_eligible"
        if form.get("send_when") != "later":
            form["scheduled_at"] = ""
        if action == "create":
            cid, errs = camp_svc.create_campaign(c, form, st, s)
            if errs:
                return page(request, "campaign_new.html", c, "campaigns", status_code=422, form=form, errors=errs,
                            prev=_preview(c, st, s, form))
            if not form.get("scheduled_at"):
                r = camp_svc.dispatch_campaign(c, cid)
                msg = ("Simulation complete - nothing was actually sent." if form.get("mode") != "LIVE_META"
                       else f"Live run finished: {r.get('status')}.")
                return redirect(f"/campaigns/{cid}", msg)
            return redirect(f"/campaigns/{cid}", "Campaign scheduled. It runs while this app is running.")
        return page(request, "campaign_new.html", c, "campaigns", form=form, errors=[], prev=_preview(c, st, s, form))


@app.get("/campaigns/{cid}", response_class=HTMLResponse)
def campaign_detail(request: Request, cid: int):
    with db.get_conn() as c:
        camp = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        if not camp:
            return page(request, "notfound.html", c, "campaigns", status_code=404, what="Campaign")
        recips = c.execute("SELECT cr.*, cu.name, cu.phone FROM campaign_recipients cr JOIN customers cu ON cu.id=cr.customer_id "
                           "WHERE cr.campaign_id=? ORDER BY cr.id", (cid,)).fetchall()
        counts: dict[str, int] = {}
        for r in recips:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        return page(request, "campaign.html", c, "campaigns", camp=camp, recips=recips, counts=counts,
                    attr=engine.campaign_attribution(c, cid))


@app.post("/campaigns/{cid}/cancel")
def campaign_cancel(cid: int):
    with db.get_conn() as c:
        ok = camp_svc.cancel_campaign(c, cid)
    return redirect(f"/campaigns/{cid}", "Campaign cancelled." if ok else "Only scheduled campaigns can be cancelled.",
                    "ok" if ok else "err")


@app.post("/campaigns/{cid}/run-now")
def campaign_run_now(cid: int):
    with db.get_conn() as c:
        r = camp_svc.dispatch_campaign(c, cid)
    return redirect(f"/campaigns/{cid}", "Dispatched." if r["claimed"] else "Already dispatched or not scheduled.",
                    "ok" if r["claimed"] else "err")


# ------------------------------------------------------------------ settings
SETTING_FIELDS = ["shop_name", "business_type", "baseline_days", "due_soon_ratio", "overdue_ratio", "high_priority_ratio",
                  "inactive_ratio", "inactive_days", "decline_factor", "vip_min_spend", "second_visit_window_days",
                  "one_time_lapse_days", "cooldown_days", "monthly_budget_cap", "message_rate_inr", "markup_inr",
                  "gst_percent", "rate_date", "attribution_days"]
NUMERIC = set(SETTING_FIELDS) - {"shop_name", "business_type", "rate_date"}


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    with db.get_conn() as c:
        return page(request, "settings.html", c, "settings", baselines=db.BUSINESS_BASELINES, errors=[])


@app.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request):
    f = dict((await request.form()).items())
    with db.get_conn() as c:
        errs = []
        for k in SETTING_FIELDS:
            v = str(f.get(k, "")).strip()
            if k in NUMERIC:
                try:
                    if float(v) < 0:
                        raise ValueError
                except ValueError:
                    errs.append(f"'{k}' must be a non-negative number.")
        if not str(f.get("shop_name", "")).strip():
            errs.append("Shop name is required.")
        try:
            if not (float(f.get("due_soon_ratio", 0)) < float(f.get("overdue_ratio", 0)) < float(f.get("high_priority_ratio", 0))
                    < float(f.get("inactive_ratio", 0))):
                errs.append("Thresholds must increase: due soon < overdue < high priority < inactive.")
        except ValueError:
            pass
        if errs:
            return page(request, "settings.html", c, "settings", status_code=422, baselines=db.BUSINESS_BASELINES, errors=errs)
        bt = f.get("business_type", "other")
        old = db.get_settings(c)
        for k in SETTING_FIELDS:
            db.set_setting(c, k, str(f.get(k, "")).strip())
        if bt != old.get("business_type") and bt in db.BUSINESS_BASELINES and str(f.get("baseline_days")) == str(old.get("baseline_days")):
            db.set_setting(c, "baseline_days", db.BUSINESS_BASELINES[bt])
        db.log_activity(c, "settings", "Settings updated")
        return redirect("/settings", "Settings saved. All segments recalculated with the new rules.")


@app.post("/settings/reset-demo")
def reset_demo(confirm: str = Form("")):
    if confirm.strip().upper() != "RESET":
        return redirect("/settings", "Type RESET to confirm. Nothing was changed.", "err")
    with db.get_conn() as c:
        r = seed.seed(c)
        db.set_setting(c, "demo_seeded", "1")
    return redirect("/", f"Demo data reset: {r['customers']} customers, {r['bills']} bills.")


@app.get("/health")
def health():
    return {"ok": True}

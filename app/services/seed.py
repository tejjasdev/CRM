"""Deterministic fictional demo data (clothing shop). All dates are relative to `today`,
so the Customer A / Customer B benchmark holds on any day. Fictional people only."""
import random
from datetime import date, datetime, timedelta, time

from .. import db
from . import engine, render

FIRST = ["Aarav", "Bhavna", "Chirag", "Divya", "Esha", "Farhan", "Gauri", "Harsh", "Isha", "Jatin",
         "Kavya", "Lakshay", "Meera", "Nikhil", "Olivia", "Pranav", "Qadir", "Riya", "Sanjay", "Tanvi",
         "Uday", "Varun", "Waseem", "Yamini", "Zoya", "Anjali", "Bharat", "Charu", "Dev", "Ekta",
         "Faiz", "Gita", "Hemant", "Ira", "Jaya", "Kabir", "Leela", "Manav", "Nandini", "Omkar",
         "Pooja", "Rohan", "Sneha", "Tarun", "Urvashi", "Vikram", "Warda", "Yash", "Zara", "Aditya",
         "Bina", "Chetan", "Deepa", "Eshaan", "Fatima", "Girish", "Hina", "Imran", "Juhi", "Karan"]
LAST = ["Mehta", "Rao", "Sharma", "Iyer", "Khan", "Patel", "Nair", "Gupta", "Joshi", "Reddy",
        "Singh", "Das", "Menon", "Kapoor", "Bose", "Verma", "Shah", "Pillai", "Chopra", "Malhotra"]
CATS = ["Shirts", "Jeans", "Kurtas", "Dresses", "Footwear", "Accessories", "T-Shirts", "Ethnic wear"]

TEMPLATE_WINBACK = ("Hi {{name}}, we miss you at {{shop_name}}! It's been {{days_since_last_visit}} days. "
                    "Use code {{offer_code}} for {{offer}}. Valid till {{expiry}}.")


def _visits(rng, last_off, interval, n, jitter=2):
    """Visit day-offsets (days before today), most recent first."""
    offs, cur = [], last_off
    for i in range(n):
        offs.append(max(cur, 0))
        cur += max(2, interval + rng.randint(-jitter, jitter))
    return offs


def build_spec(rng):
    """Return list of customer specs: dict(name_idx, visits[offsets], amt(lo,hi), consent, ...)."""
    S = []

    def add(kind, offs, amt, consent="Yes", tags="", phone_bad=False, extra_same_day=0, note="", amts=None):
        S.append(dict(kind=kind, offs=offs, amt=amt, consent=consent, tags=tags, phone_bad=phone_bad,
                      extra_same_day=extra_same_day, note=note, amts=amts))

    # --- benchmarks ---
    add("A", [45 + 20 * i for i in range(6)], (4100, 4700), tags="demo-benchmark-A", note="Benchmark A: every 20 days, absent 45 days.",
        amts=[4200, 4650, 4300, 4500, 4100, 4700])
    add("B", [55 + 90 * i for i in range(5)], (2800, 3300), tags="demo-benchmark-B", note="Benchmark B: every 90 days, absent 55 days.")
    # --- VIPs (6) ---
    for i, (iv, lo) in enumerate([(28, 8), (30, 14), (26, 30), (32, 40), (35, 52), (29, 20)]):
        add("VIP", _visits(rng, lo // 2 + 1 if i % 2 else int(iv * 1.2), iv, 11, 3), (4500, 9000), tags="vip",
            extra_same_day=1 if i < 2 else 0)
    # --- regular on-track (15) ---
    for i in range(15):
        iv = rng.choice([21, 28, 35, 40, 45, 60])
        add("REG", _visits(rng, int(iv * rng.uniform(0.15, 0.85)), iv, 6, 2), (1500, 3800),
            consent="No" if i == 4 else "Yes", extra_same_day=1 if i in (3, 9) else 0)
    # --- due soon (5) ---
    for i in range(5):
        iv = rng.choice([25, 30, 40, 50, 35])
        add("DUE", _visits(rng, int(iv * 1.3), iv, 5, 2), (1800, 3600), consent="Unknown" if i == 3 else "Yes")
    # --- overdue (6, ratio ~1.5-1.9) ---
    for i in range(6):
        iv = rng.choice([22, 30, 40, 28, 35, 45])
        add("OVER", _visits(rng, int(iv * 1.7), iv, 5, 2), (1800, 4200),
            consent="Unknown" if i == 2 else "Yes", phone_bad=(i == 5))
    # --- high priority (3 + A) ---
    for i in range(3):
        iv = rng.choice([24, 30, 36])
        add("HIGH", _visits(rng, int(iv * 2.4), iv, 5, 1), (3000, 5200), tags="regular")
    # --- declining frequency (4): gaps widen ---
    for i in range(4):
        base = rng.choice([20, 24, 28, 22])
        gaps = [base, base + 1, base - 1, base, base + 2, base * 2]    # latest gap doubles
        offs, cur = [int(base * 0.6)], int(base * 0.6)
        for g in reversed(gaps):
            cur += g
            offs.append(cur)
        add("DECL", offs, (2000, 4200))
    # --- inactive / lapsed (6) ---
    for i in range(6):
        iv = rng.choice([25, 30, 35, 40, 28, 33])
        add("INACT", _visits(rng, int(iv * 3.6), iv, 5, 2), (1500, 3500),
            consent="No" if i == 1 else "Yes", phone_bad=False)
    # --- preliminary (2 visits) (4) ---
    for i in range(4):
        gap = rng.choice([18, 25, 32, 40])
        add("PRELIM", [rng.randint(8, 30), 0 + 0][:1] + [0], (1400, 3000))  # placeholder, fixed below
        S[-1]["offs"] = [rng.randint(8, 35)]
        S[-1]["offs"].append(S[-1]["offs"][0] + gap)
    # --- one-time buyers (6): 3 in second-visit window, 3 lapsed ---
    for off in (12, 22, 38, 150, 210, 290):
        add("ONE", [off], (1200, 3400), consent="Unknown" if off == 22 else "Yes")
    # --- brand new, no bills (3) ---
    for i in range(3):
        add("NEW", [], (0, 0), consent="Unknown" if i < 2 else "Yes", note="Added at the counter, no bill yet.")
    return S


def seed(conn, today: date | None = None):
    today = today or date.today()
    rng = random.Random(20261027)
    db.wipe_all(conn)
    spec = build_spec(rng)
    names, seen = [], set()
    ni = 0
    while len(names) < len(spec):
        n = f"{FIRST[ni % len(FIRST)]} {LAST[(ni * 7 + ni // len(FIRST)) % len(LAST)]}"
        ni += 1
        if n not in seen:
            seen.add(n)
            names.append(n)
    names[0], names[1] = "Aarav Mehta", "Bhavna Rao"          # benchmark customers (A and B)
    seen_names = set()
    for i, n in enumerate(names):
        if n in seen_names:                                    # keep names unique after the override
            names[i] = n + " Jr"
        seen_names.add(names[i])

    ts_now = db.now_iso()
    cust_ids, txn_rows = [], []
    for i, s in enumerate(spec):
        phone_digits = f"9000{10000 + i * 13:06d}"[:10]
        raw = phone_digits[:5] + " " + phone_digits[5:]
        phone = "91" + phone_digits
        if s["phone_bad"]:
            raw, phone = "98765-4", None
        first_off = max(s["offs"]) if s["offs"] else rng.choice([2, 5, 9])
        created = datetime.combine(today - timedelta(days=first_off), time(10, 0)).isoformat(sep=" ")
        email = f"demo{i + 1}@example.invalid" if i % 3 == 0 else None
        cur = conn.execute(
            "INSERT INTO customers(name, phone, phone_raw, email, notes, tags, consent, is_demo, source, created_at)"
            " VALUES(?,?,?,?,?,?,?,1,'demo',?)",
            (names[i], phone, raw, email, s["note"] or "DEMO DATA - fictional customer.", s["tags"], s["consent"], created))
        cid = cur.lastrowid
        cust_ids.append(cid)
        lo, hi = s["amt"]
        for j, off in enumerate(sorted(s["offs"], reverse=True)):
            d = today - timedelta(days=off)
            amount = s["amts"][j % len(s["amts"])] if s["amts"] else rng.randrange(int(lo), int(hi), 50)
            # fixed amounts for A are listed most-recent-first; order does not matter for totals
            hh = rng.randint(11, 19)
            txn_rows.append((cid, datetime.combine(d, time(hh, rng.choice([0, 15, 30, 45]))), float(amount),
                             rng.choice(CATS)))
            if s["extra_same_day"] and j == 2:     # second bill on the same day = same visit, separate bill
                txn_rows.append((cid, datetime.combine(d, time(min(hh + 1, 20), 5)), float(rng.randrange(400, 1500, 50)),
                                 "Accessories"))

    # returns: a couple of refunds to exercise net-amount handling
    bills_by_c = {}
    for r in txn_rows:
        bills_by_c.setdefault(r[0], []).append(r)
    ret_n = 0
    for cid in cust_ids[2:]:
        rows = sorted(bills_by_c.get(cid, []), key=lambda r: r[1])
        if len(rows) >= 5 and ret_n < 3 and cid % 5 == 0:
            base = rows[1]
            txn_rows.append((cid, base[1] + timedelta(days=2), -round(base[2] * 0.25, 0), "Return - exchange"))
            ret_n += 1

    txn_rows.sort(key=lambda r: r[1])
    for n, (cid, dt, amt, cat) in enumerate(txn_rows, start=1):
        conn.execute(
            "INSERT INTO transactions(customer_id, bill_id, txn_date, amount, category, source, created_at)"
            " VALUES(?,?,?,?,?,'demo',?)",
            (cid, f"DEMO-{n:05d}" if amt > 0 else f"DEMO-R{n:05d}", dt.isoformat(sep=" "), amt, cat, ts_now))
    conn.commit()
    settings = db.get_settings(conn)

    # --- past campaigns for history / attribution / cooldown demos ---
    stats = engine.load_all(conn, settings, today)
    bills_by_cust = {}
    for r in conn.execute("SELECT customer_id, txn_date FROM transactions WHERE amount > 0"):
        bills_by_cust.setdefault(r["customer_id"], []).append(datetime.fromisoformat(r["txn_date"]).date())

    def make_campaign(name, days_ago, offer, code, recipients, expiry_days=14):
        disp = datetime.combine(today - timedelta(days=days_ago), time(10, 0)).isoformat(sep=" ")
        cur = conn.execute(
            """INSERT INTO campaigns(name, offer, offer_code, expiry, template, segment, mode, status,
               rate, markup, gst_percent, est_recipients, est_cost, attribution_days, created_at, dispatched_at, completed_at)
               VALUES(?,?,?,?,?,?,'SIMULATION','DONE',1.2,0,18,?,?,14,?,?,?)""",
            (name, offer, code, (today - timedelta(days=days_ago - expiry_days)).isoformat(),
             TEMPLATE_WINBACK, "offer_eligible", len(recipients), round(len(recipients) * 1.2 * 1.18, 2), disp, disp, disp))
        camp_id = cur.lastrowid
        cdict = {"offer": offer, "offer_code": code, "expiry": (today - timedelta(days=days_ago - expiry_days)).strftime("%d %b")}
        for cid in recipients:
            st = dict(stats[cid])
            st["recency_days"] = (st["recency_days"] or 0) + days_ago
            text = render.render(TEMPLATE_WINBACK, render.values_for(st, cdict, settings))
            conn.execute(
                "INSERT INTO campaign_recipients(campaign_id, customer_id, rendered_text, status, updated_at) VALUES(?,?,?,'SIMULATED',?)",
                (camp_id, cid, text, disp))
        db.log_activity(conn, "campaign", f"[DEMO] Past simulated campaign '{name}' to {len(recipients)} customers")
        return camp_id

    # 35 days ago: recipients = some who bought within 14 days afterwards, some who did not
    lo_d, hi_d = today - timedelta(days=34), today - timedelta(days=21)
    returned, silent = [], []
    for cid, s in stats.items():
        if not (s["consent"] == "Yes" and s["phone"] and s["visit_count"] >= 3):
            continue
        ds = bills_by_cust.get(cid, [])
        if any(lo_d <= d <= hi_d for d in ds):
            returned.append(cid)
        elif any(d < today - timedelta(days=35) for d in ds) and not any(d > today - timedelta(days=35) for d in ds):
            silent.append(cid)
    old_recipients = returned[:5] + silent[:7]
    if old_recipients:
        make_campaign("Festive comeback (past demo run)", 35, "10% off your next bill", "FEST10", old_recipients)

    # 5 days ago: five eligible-looking customers now in cooldown
    cool = [cid for cid, s in stats.items()
            if s["consent"] == "Yes" and s["phone"] and s["status"] in (engine.DUE_SOON, engine.OVERDUE, engine.INACTIVE, engine.HIGH_PRIORITY)
            and s["name"] not in ("Aarav Mehta", "Bhavna Rao")]
    make_campaign("Weekend win-back (past demo run)", 5, "Flat 15% off", "WB15", cool[:5])

    db.log_activity(conn, "seed", "Demo data loaded (fictional customers and bills).")
    db.set_setting(conn, "data_source", "Demo data")
    db.set_setting(conn, "data_updated", ts_now)
    conn.commit()
    return {"customers": len(cust_ids), "bills": len([r for r in txn_rows if r[2] > 0]),
            "returns": len([r for r in txn_rows if r[2] < 0])}

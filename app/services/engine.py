"""Retention engine: metrics, cadence, status, segments, priority, eligibility.

Everything is recomputed from persisted transactions on demand (no UI-only totals).
Definitions (also documented in PROJECT_NOTES.md):
  * bill      = valid transaction with amount > 0
  * return    = valid transaction with amount < 0 (reduces spend, is not a bill or a visit)
  * visit     = distinct calendar date that has >= 1 bill
  * interval  = day gap between consecutive visit dates
  * usual interval = median of the last 5 intervals; with only one observed gap it is
                     blended 50/50 with the business baseline (labelled Preliminary)
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from statistics import median

from ..utils import to_date, fmt_inr

# ---------- statuses ----------
NO_HISTORY, ONE_TIME, ON_TRACK, DUE_SOON, OVERDUE, HIGH_PRIORITY, INACTIVE = (
    "NO_HISTORY", "ONE_TIME", "ON_TRACK", "DUE_SOON", "OVERDUE", "HIGH_PRIORITY", "INACTIVE")

STATUS_LABEL = {
    NO_HISTORY: "No purchase history", ONE_TIME: "One-time buyer", ON_TRACK: "On track",
    DUE_SOON: "Due soon", OVERDUE: "Overdue", HIGH_PRIORITY: "High priority",
    INACTIVE: "Inactive / lapsed",
}
ACTIONABLE = {DUE_SOON, OVERDUE, HIGH_PRIORITY, INACTIVE}

SEGMENTS = [
    ("all", "All customers", "Every customer record."),
    ("new", "New / no history", "Customer record exists but no valid bill yet."),
    ("one_time", "One-time buyers", "Exactly one distinct visit date."),
    ("second_visit", "Second-visit window",
     "One-time buyers whose single visit was between 7 days and the configured second-visit window ago."),
    ("repeat", "Repeat customers", "Two or more distinct visit dates."),
    ("regular", "Regular / on-track",
     "3+ visits and days since last visit is below 1.1x their usual interval."),
    ("vip", "High-spending / VIP", "Lifetime net spend at or above the VIP threshold and 3+ visits."),
    ("due_soon", "Due soon", "Repeat customers at 1.1x to <1.5x their own usual interval."),
    ("overdue", "Overdue regulars",
     "Repeat customers at 1.5x or more of their usual interval, not yet lapsed (includes high priority at 2.0x+)."),
    ("declining", "Declining frequency",
     "4+ visits and the latest gap is at least 1.5x their earlier typical gap."),
    ("inactive", "Inactive / lapsed",
     "3x+ usual interval missed, or 120+ days away and already overdue; one-time buyers after 120 days."),
    ("offer_eligible", "Offer-eligible",
     "Due soon / overdue / lapsed (or in the second-visit window) AND consent = Yes, valid phone, outside cooldown."),
]
SEGMENT_LABEL = {k: l for k, l, _ in SEGMENTS}
SEGMENT_RULE = {k: r for k, _, r in SEGMENTS}


def _f(settings, key, default=0.0):
    try:
        return float(settings.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _confidence(visits: int) -> str:
    if visits <= 0:
        return "No purchase history"
    if visits == 1:
        return "Insufficient history"
    if visits == 2:
        return "Preliminary"
    if visits == 3:
        return "Moderate"
    return "Higher confidence"


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def analyze_customer(cust: dict, txns: list[dict], settings: dict, today: date,
                     last_contact: date | None = None) -> dict:
    """Return the full metric dict for one customer."""
    valid = [t for t in txns if t["status"] == "valid"]
    bills = [t for t in valid if t["amount"] > 0]
    returns = [t for t in valid if t["amount"] < 0]
    spend = sum(t["amount"] for t in valid)
    bill_count = len(bills)
    gross = sum(t["amount"] for t in bills)
    visit_dates = sorted({to_date(t["txn_date"]) for t in bills})
    visits = len(visit_dates)
    intervals = [(visit_dates[i] - visit_dates[i - 1]).days for i in range(1, visits)]

    def spend_since(days):
        cut = today - timedelta(days=days)
        return sum(t["amount"] for t in valid if to_date(t["txn_date"]) > cut)

    baseline = _f(settings, "baseline_days", 45)
    first_visit = visit_dates[0] if visit_dates else None
    last_visit = visit_dates[-1] if visit_dates else None
    recency = (today - last_visit).days if last_visit else None

    usual = ratio = days_over = None
    if intervals:
        recent = intervals[-5:]
        med = median(recent)
        usual = (0.5 * med + 0.5 * baseline) if visits == 2 else med
        usual = max(round(usual, 1), 1.0)
        ratio = recency / usual
        days_over = recency - usual

    conf = _confidence(visits)

    # ----- status -----
    due_r, over_r, high_r = (_f(settings, "due_soon_ratio", 1.1), _f(settings, "overdue_ratio", 1.5),
                             _f(settings, "high_priority_ratio", 2.0))
    inact_r, inact_d = _f(settings, "inactive_ratio", 3.0), _f(settings, "inactive_days", 120)
    one_lapse = _f(settings, "one_time_lapse_days", 120)
    win = _f(settings, "second_visit_window_days", 45)

    if visits == 0:
        status = NO_HISTORY
    elif visits == 1:
        status = INACTIVE if recency >= one_lapse else ONE_TIME
    else:
        if ratio >= inact_r or (recency >= inact_d and ratio >= over_r):
            status = INACTIVE
        elif ratio >= high_r:
            status = HIGH_PRIORITY
        elif ratio >= over_r:
            status = OVERDUE
        elif ratio >= due_r:
            status = DUE_SOON
        else:
            status = ON_TRACK

    # ----- declining -----
    declining = False
    decline_note = ""
    if len(intervals) >= 3:
        earlier = median(intervals[:-1][-5:])
        latest = intervals[-1]
        if earlier > 0 and latest >= _f(settings, "decline_factor", 1.5) * earlier:
            declining = True
            decline_note = f"latest gap {latest}d vs earlier typical {earlier:g}d"

    vip = spend >= _f(settings, "vip_min_spend", 25000) and visits >= 3
    avg_bill = (gross / bill_count) if bill_count else 0.0

    # ----- contact eligibility -----
    reasons_excl = []
    if cust["consent"] == "No":
        reasons_excl.append("Opted out")
    elif cust["consent"] == "Unknown":
        reasons_excl.append("Consent unknown")
    if not cust["phone"]:
        reasons_excl.append("Invalid phone")
    cooldown = int(_f(settings, "cooldown_days", 14))
    in_cooldown = bool(last_contact and (today - last_contact).days < cooldown)
    if in_cooldown:
        reasons_excl.append(f"Cooldown (contacted {(today - last_contact).days}d ago)")
    contactable = not reasons_excl

    second_visit = (status == ONE_TIME and 7 <= recency <= win)
    actionable = status in ACTIONABLE or second_visit
    offer_eligible = contactable and actionable

    # ----- priority score (visible factors, 0-100) -----
    f_over = f_value = f_freq = f_decl = 0.0
    if status in ACTIONABLE and ratio is not None:
        f_over = min(ratio, 3.0) / 3.0 * 50
        if status == INACTIVE:
            f_over *= 0.6          # lapsed: still worth winning back but less urgent than late regulars
    elif second_visit:
        f_over = 15
    f_value = min(spend / max(_f(settings, "vip_min_spend", 25000), 1), 1.5) / 1.5 * 30
    f_freq = min(visits, 8) / 8 * 12
    f_decl = 8 if declining else 0
    score = round(f_over + f_value + f_freq + f_decl) if actionable else 0

    # ----- plain-language reason -----
    if visits == 0:
        reason = "No bills recorded yet."
    elif visits == 1:
        reason = (f"Single visit {recency} days ago ({fmt_inr(spend)}). No personal visit interval can be "
                  f"estimated from one visit.")
        if second_visit:
            reason += " Within the second-visit window - a good moment for a nudge."
        elif status == INACTIVE:
            reason += " One-time buyer, now lapsed."
    else:
        pref = "Preliminary: " if visits == 2 else ""
        reason = (f"{pref}Usually returns every ~{usual:g} days; now {recency} days since last visit "
                  f"({ratio:.2f}x usual interval")
        reason += (f", {days_over:.0f} days past it" if days_over > 0 else ", still within it") + f"). "
        reason += f"{_plural(visits, 'visit')}, lifetime spend {fmt_inr(spend)}"
        if vip:
            reason += " (VIP)"
        reason += "."
        if declining:
            reason += f" Visits are slowing ({decline_note})."
    if actionable and not contactable:
        reason += " Cannot be messaged: " + ", ".join(reasons_excl).lower() + "."

    # ----- segment memberships -----
    seg = {
        "all": True,
        "new": visits == 0,
        "one_time": visits == 1,
        "second_visit": second_visit,
        "repeat": visits >= 2,
        "regular": visits >= 3 and status == ON_TRACK,
        "vip": vip,
        "due_soon": status == DUE_SOON,
        "overdue": status in (OVERDUE, HIGH_PRIORITY),
        "declining": declining,
        "inactive": status == INACTIVE,
        "offer_eligible": offer_eligible,
    }

    return {
        "id": cust["id"], "name": cust["name"], "phone": cust["phone"], "phone_raw": cust["phone_raw"],
        "email": cust["email"], "notes": cust["notes"], "tags": cust["tags"], "consent": cust["consent"],
        "is_demo": bool(cust["is_demo"]), "created_at": cust["created_at"],
        "lifetime_spend": spend, "bill_count": bill_count, "visit_count": visits,
        "avg_bill": avg_bill, "return_total": sum(t["amount"] for t in returns),
        "spend_30": spend_since(30), "spend_90": spend_since(90), "spend_365": spend_since(365),
        "first_visit": first_visit, "last_visit": last_visit, "recency_days": recency,
        "intervals": intervals, "visit_dates": visit_dates,
        "usual_interval": usual, "overdue_ratio": ratio, "days_overdue": days_over,
        "confidence": conf, "status": status, "status_label": STATUS_LABEL[status],
        "declining": declining, "vip": vip, "second_visit": second_visit,
        "contactable": contactable, "exclusions": reasons_excl, "in_cooldown": in_cooldown,
        "last_contact": last_contact, "actionable": actionable, "offer_eligible": offer_eligible,
        "priority_score": score,
        "priority_factors": {"overdue": round(f_over), "value": round(f_value),
                             "frequency": round(f_freq), "declining": round(f_decl)},
        "reason": reason, "segments": seg,
    }


def load_all(conn, settings: dict, today: date | None = None) -> dict[int, dict]:
    """Analyze every customer from persisted records. Returns {customer_id: metrics}."""
    today = today or date.today()
    custs = [dict(r) for r in conn.execute("SELECT * FROM customers ORDER BY id")]
    by_c: dict[int, list] = {}
    for r in conn.execute("SELECT * FROM transactions WHERE customer_id IS NOT NULL ORDER BY txn_date"):
        by_c.setdefault(r["customer_id"], []).append(dict(r))
    contact: dict[int, date] = {}
    for r in conn.execute(
        """SELECT cr.customer_id AS cid, MAX(COALESCE(c.dispatched_at, c.created_at)) AS ts
           FROM campaign_recipients cr JOIN campaigns c ON c.id = cr.campaign_id
           WHERE cr.status NOT IN ('FAILED','QUEUED') GROUP BY cr.customer_id"""):
        if r["ts"]:
            contact[r["cid"]] = to_date(r["ts"])
    return {c["id"]: analyze_customer(c, by_c.get(c["id"], []), settings, today, contact.get(c["id"]))
            for c in custs}


def segment_members(stats: dict[int, dict], key: str) -> list[dict]:
    rows = [s for s in stats.values() if s["segments"].get(key)]
    rows.sort(key=lambda s: (-s["priority_score"], s["name"].lower()))
    return rows


def segment_counts(stats: dict[int, dict]) -> dict[str, int]:
    return {k: sum(1 for s in stats.values() if s["segments"][k]) for k, _, _ in SEGMENTS}


def dashboard(conn, stats: dict[int, dict], settings: dict, period_days: int, today: date | None = None) -> dict:
    today = today or date.today()
    cut = today - timedelta(days=period_days)
    allc = list(stats.values())
    buyers = [s for s in allc if s["visit_count"] >= 1]
    repeaters = [s for s in allc if s["visit_count"] >= 2]
    txn = [dict(r) for r in conn.execute(
        "SELECT * FROM transactions WHERE status='valid' AND customer_id IS NOT NULL")]
    total_sales = sum(t["amount"] for t in txn)
    bills = [t for t in txn if t["amount"] > 0]
    period_sales = sum(t["amount"] for t in txn if to_date(t["txn_date"]) > cut)
    new_period = sum(1 for s in allc if to_date(s["created_at"]) > cut)
    camp = conn.execute(
        "SELECT COUNT(*) n FROM campaigns WHERE status NOT IN ('CANCELLED')").fetchone()["n"]
    msgs = {r["status"]: r["n"] for r in conn.execute(
        "SELECT cr.status, COUNT(*) n FROM campaign_recipients cr GROUP BY cr.status")}
    unmatched = conn.execute(
        "SELECT COUNT(*) n FROM transactions WHERE customer_id IS NULL").fetchone()["n"]
    possible = conn.execute(
        "SELECT COUNT(*) n FROM transactions WHERE possible_dup=1").fetchone()["n"]
    return {
        "total_customers": len(allc), "new_customers": new_period,
        "repeat_rate": (len(repeaters) / len(buyers) * 100) if buyers else 0.0,
        "repeat_n": len(repeaters), "buyer_n": len(buyers),
        "total_sales": total_sales, "period_sales": period_sales,
        "avg_bill": (sum(t["amount"] for t in bills) / len(bills)) if bills else 0.0,
        "bill_count": len(bills), "visit_count": sum(s["visit_count"] for s in allc),
        "vip_count": sum(1 for s in allc if s["vip"]),
        "due_soon": sum(1 for s in allc if s["status"] == DUE_SOON),
        "overdue": sum(1 for s in allc if s["status"] in (OVERDUE, HIGH_PRIORITY)),
        "campaigns": camp, "msgs": msgs, "unmatched": unmatched, "possible_dups": possible,
        "period_days": period_days,
    }


def campaign_attribution(conn, campaign_id: int) -> dict:
    """Bills by recipients within the attribution window after dispatch.
    Labelled 'observed return after campaign' - correlation, not proof of causation."""
    c = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if not c or not (c["dispatched_at"] or c["created_at"]):
        return {"returns": 0, "sales": 0.0, "customers": [], "window_open": False}
    start = to_date(c["dispatched_at"] or c["created_at"])
    end = start + timedelta(days=int(c["attribution_days"] or 14))
    rows = conn.execute(
        """SELECT t.customer_id, cu.name, t.txn_date, t.amount, t.bill_id
           FROM transactions t
           JOIN campaign_recipients cr ON cr.customer_id = t.customer_id AND cr.campaign_id = ?
           JOIN customers cu ON cu.id = t.customer_id
           WHERE t.status='valid' AND t.amount > 0 AND date(t.txn_date) > date(?) AND date(t.txn_date) <= date(?)
           AND cr.status NOT IN ('FAILED','QUEUED')
           ORDER BY t.txn_date""", (campaign_id, start.isoformat(), end.isoformat())).fetchall()
    custs = {}
    for r in rows:
        custs.setdefault(r["customer_id"], {"name": r["name"], "id": r["customer_id"], "sales": 0.0, "bills": 0})
        custs[r["customer_id"]]["sales"] += r["amount"]
        custs[r["customer_id"]]["bills"] += 1
    return {"returns": len(custs), "sales": sum(r["amount"] for r in rows), "customers": list(custs.values()),
            "window_start": start, "window_end": end, "window_open": date.today() <= end}

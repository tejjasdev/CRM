"""Audience building, cost estimates, campaign creation, dispatch and recovery."""
from __future__ import annotations

import math
from datetime import date, datetime

from .. import config, db
from . import engine, render
from .senders import get_sender


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def cost_of(n: int, rate: float, markup: float, gst: float) -> dict:
    base = n * rate
    mk = n * markup
    tax = (base + mk) * gst / 100.0
    return {"count": n, "base": round(base, 2), "markup": round(mk, 2), "gst": round(tax, 2),
            "total": round(base + mk + tax, 2), "per_msg": round((rate + markup) * (1 + gst / 100.0), 4)}


def rates(settings: dict, camp: dict | None = None) -> tuple[float, float, float]:
    if camp and camp.get("rate") is not None:
        return _f(camp["rate"]), _f(camp.get("markup")), _f(camp.get("gst_percent"))
    return _f(settings.get("message_rate_inr"), 1.2), _f(settings.get("markup_inr")), _f(settings.get("gst_percent"), 18)


def build_audience(stats: dict[int, dict], settings: dict, segment: str, mode: str = "SIMULATION",
                   max_recipients: int | None = None, budget_cap: float | None = None) -> dict:
    """Included (priority order) and excluded-with-reason lists, plus cost. Built from actual records."""
    rate, markup, gst = rates(settings)
    per_msg = cost_of(1, rate, markup, gst)["per_msg"]
    members = engine.segment_members(stats, segment)
    eligible, excluded = [], []
    for s in members:
        why = list(s["exclusions"])
        if mode == "LIVE_META" and s["is_demo"]:
            why.append("Demo contact (never live-eligible)")
        (excluded if why else eligible).append((s, why))
    limit = len(eligible)
    notes = []
    if max_recipients and max_recipients > 0 and max_recipients < limit:
        limit = max_recipients
        notes.append(f"recipient cap {max_recipients}")
    if budget_cap and budget_cap > 0 and per_msg > 0:
        by_budget = int(math.floor(budget_cap / per_msg + 1e-9))
        if by_budget < limit:
            limit = by_budget
            notes.append(f"budget cap {budget_cap:g}")
    included = [s for s, _ in eligible[:limit]]
    for s, _ in eligible[limit:]:
        excluded.append((s, ["Over " + " / ".join(notes) + " (lower priority)"]))
    reason_counts: dict[str, int] = {}
    for _, why in excluded:
        for w in why:
            key = "Cooldown" if w.startswith("Cooldown") else w.split(" (lower")[0] if w.startswith("Over") else w
            reason_counts[key] = reason_counts.get(key, 0) + 1
    return {"included": included, "excluded": excluded, "reason_counts": reason_counts,
            "segment_total": len(members), "cost": cost_of(len(included), rate, markup, gst),
            "rate": rate, "markup": markup, "gst": gst, "cap_notes": notes}


def month_spend(conn) -> float:
    ym = datetime.now().strftime("%Y-%m")
    r = conn.execute("SELECT COALESCE(SUM(est_cost),0) s FROM campaigns WHERE status NOT IN ('CANCELLED') "
                     "AND substr(created_at,1,7)=? AND name NOT LIKE '%(past demo run)'", (ym,)).fetchone()
    return float(r["s"])


def validate(form: dict, settings: dict, mode: str, aud: dict, conn) -> list[str]:
    errs = []
    if not (form.get("name") or "").strip():
        errs.append("Campaign name is required.")
    if not (form.get("template") or "").strip():
        errs.append("Message template is required.")
    bad = render.unknown_placeholders(form.get("template", ""))
    if bad:
        errs.append("Unknown placeholders: " + ", ".join("{{%s}}" % b for b in bad))
    if not aud["included"]:
        errs.append("No eligible recipients - nothing to send.")
    cap = _f(settings.get("monthly_budget_cap"))
    if cap > 0 and month_spend(conn) + aud["cost"]["total"] > cap + 1e-9:
        errs.append(f"Monthly budget cap {cap:g} would be exceeded "
                    f"(already planned {month_spend(conn):.2f} + this {aud['cost']['total']:.2f}).")
    if mode == "LIVE_META":
        ready = config.live_readiness()
        if not ready["ready"]:
            errs.append("Live mode unavailable - missing: " + ", ".join(ready["missing"]))
        if not (form.get("meta_template_name") or "").strip():
            errs.append("An approved Meta template name is required for live sends.")
        if not form.get("confirm_live"):
            errs.append("Tick the live confirmation (audience, template and cost).")
    if form.get("scheduled_at"):
        try:
            datetime.fromisoformat(form["scheduled_at"])
        except ValueError:
            errs.append("Schedule date/time is invalid.")
    return errs


def create_campaign(conn, form: dict, stats: dict, settings: dict) -> tuple[int | None, list[str]]:
    mode = "LIVE_META" if form.get("mode") == "LIVE_META" else "SIMULATION"
    aud = build_audience(stats, settings, form["segment"], mode,
                         int(form["max_recipients"]) if str(form.get("max_recipients") or "").isdigit() else None,
                         _f(form.get("budget_cap")) or None)
    errs = validate(form, settings, mode, aud, conn)
    if errs:
        return None, errs
    now = db.now_iso()
    sched = form.get("scheduled_at") or ""
    sched_iso = datetime.fromisoformat(sched).isoformat(sep=" ") if sched else now
    rate, markup, gst = aud["rate"], aud["markup"], aud["gst"]
    cur = conn.execute(
        """INSERT INTO campaigns(name, offer, offer_code, expiry, min_bill, template, segment, mode, status,
           max_recipients, budget_cap, rate, markup, gst_percent, est_recipients, est_cost, attribution_days,
           meta_template_name, meta_language, meta_params, created_at, scheduled_at)
           VALUES(?,?,?,?,?,?,?,?,'SCHEDULED',?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (form["name"].strip(), form.get("offer"), form.get("offer_code"), form.get("expiry") or None,
         _f(form.get("min_bill")) or None, form["template"], form["segment"], mode,
         int(form["max_recipients"]) if str(form.get("max_recipients") or "").isdigit() else None,
         _f(form.get("budget_cap")) or None, rate, markup, gst, len(aud["included"]), aud["cost"]["total"],
         int(_f(settings.get("attribution_days"), 14)), (form.get("meta_template_name") or "").strip() or None,
         (form.get("meta_language") or "en").strip(), (form.get("meta_params") or "").strip() or None, now, sched_iso))
    cid = cur.lastrowid
    cdict = {"offer": form.get("offer"), "offer_code": form.get("offer_code"),
             "expiry": form.get("expiry"), "min_bill": form.get("min_bill")}
    for s in aud["included"]:
        text = render.render(form["template"], render.values_for(s, cdict, settings))
        conn.execute("INSERT INTO campaign_recipients(campaign_id, customer_id, rendered_text, status, updated_at)"
                     " VALUES(?,?,?,'QUEUED',?)", (cid, s["id"], text, now))
    label = "LIVE" if mode == "LIVE_META" else "SIMULATION"
    db.log_activity(conn, "campaign", f"Campaign '{form['name'].strip()}' created ({label}, {len(aud['included'])} recipients"
                                      f"{', scheduled' if sched else ''})")
    conn.commit()
    return cid, []


def dispatch_campaign(conn, campaign_id: int, today: date | None = None) -> dict:
    """Idempotent: only one caller can claim a SCHEDULED campaign (atomic status flip)."""
    now = db.now_iso()
    cur = conn.execute("UPDATE campaigns SET status='RUNNING', dispatched_at=COALESCE(dispatched_at, ?) "
                       "WHERE id=? AND status='SCHEDULED'", (now, campaign_id))
    conn.commit()
    if cur.rowcount == 0:
        return {"claimed": False}
    # a campaign's dispatched_at must reflect the real dispatch moment
    conn.execute("UPDATE campaigns SET dispatched_at=? WHERE id=?", (now, campaign_id))
    conn.commit()
    camp = dict(conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone())
    settings = db.get_settings(conn)
    stats = engine.load_all(conn, settings, today)
    sender = get_sender(camp["mode"])
    params_keys = [p.strip() for p in (camp["meta_params"] or "").split(",") if p.strip()]
    cdict = {"offer": camp["offer"], "offer_code": camp["offer_code"], "expiry": camp["expiry"], "min_bill": camp["min_bill"]}
    counts = {"sent": 0, "failed": 0, "skipped": 0}
    recips = conn.execute("SELECT * FROM campaign_recipients WHERE campaign_id=? AND status='QUEUED' ORDER BY id",
                          (campaign_id,)).fetchall()
    for r in recips:
        s = stats.get(r["customer_id"])
        # re-validate at send time (consent may have changed since scheduling)
        why = list(s["exclusions"]) if s else ["Customer missing"]
        if s and s["in_cooldown"]:
            why = [w for w in why]  # already included
        if camp["mode"] == "LIVE_META" and s and s["is_demo"]:
            why.append("Demo contact")
        if why:
            conn.execute("UPDATE campaign_recipients SET status='FAILED', error=?, updated_at=? WHERE id=?",
                         ("Skipped at send time: " + ", ".join(why), db.now_iso(), r["id"]))
            counts["skipped"] += 1
            conn.commit()
            continue
        vals = render.values_for(s, cdict, settings)
        text = render.render(camp["template"], vals)
        res = sender.send(s["phone"], text, template_name=camp["meta_template_name"],
                          language=camp["meta_language"], params=[vals.get(k, "") for k in params_keys])
        conn.execute("UPDATE campaign_recipients SET rendered_text=?, status=?, provider_message_id=?, error=?, updated_at=? WHERE id=?",
                     (text, res["status"], res["provider_id"], res["error"], db.now_iso(), r["id"]))
        conn.commit()          # persist per recipient: a crash never loses recorded results
        counts["failed" if res["status"] in ("FAILED", "UNKNOWN") else "sent"] += 1
    final = "DONE_WITH_ERRORS" if (counts["failed"] or counts["skipped"]) else "DONE"
    conn.execute("UPDATE campaigns SET status=?, completed_at=? WHERE id=?", (final, db.now_iso(), campaign_id))
    db.log_activity(conn, "campaign", f"Campaign '{camp['name']}' finished: {final} "
                                      f"({counts['sent']} {'simulated' if camp['mode'] == 'SIMULATION' else 'submitted'}, "
                                      f"{counts['failed']} failed/unknown, {counts['skipped']} skipped)")
    conn.commit()
    return {"claimed": True, **counts, "status": final}


def due_scheduled(conn) -> list[int]:
    return [r["id"] for r in conn.execute(
        "SELECT id FROM campaigns WHERE status='SCHEDULED' AND scheduled_at <= ? ORDER BY scheduled_at", (db.now_iso(),))]


def recover_after_restart(conn):
    """Campaigns left RUNNING by a crash/stop. Simulation: safe to resume (only QUEUED rows get processed).
    Live: in-flight rows may or may not have reached Meta -> mark UNKNOWN, never resend."""
    for r in conn.execute("SELECT id, mode FROM campaigns WHERE status='RUNNING'").fetchall():
        if r["mode"] == "LIVE_META":
            conn.execute("UPDATE campaign_recipients SET status='UNKNOWN', error='Interrupted by restart; verify in Meta before resending', "
                         "updated_at=? WHERE campaign_id=? AND status='QUEUED'", (db.now_iso(), r["id"]))
            conn.execute("UPDATE campaigns SET status='DONE_WITH_ERRORS', completed_at=? WHERE id=?", (db.now_iso(), r["id"]))
        else:
            conn.execute("UPDATE campaigns SET status='SCHEDULED' WHERE id=?", (r["id"],))
    conn.commit()


def cancel_campaign(conn, campaign_id: int) -> bool:
    cur = conn.execute("UPDATE campaigns SET status='CANCELLED' WHERE id=? AND status='SCHEDULED'", (campaign_id,))
    conn.execute("UPDATE campaign_recipients SET status='FAILED', error='Campaign cancelled' "
                 "WHERE campaign_id=? AND status='QUEUED'", (campaign_id,)) if cur.rowcount else None
    conn.commit()
    return cur.rowcount > 0

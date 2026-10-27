import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    for k in ("META_ACCESS_TOKEN", "META_PHONE_NUMBER_ID", "META_WABA_ID", "META_API_VERSION"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("WHATSAPP_MODE", "simulation")
    from app import db
    c = db.connect()
    db.init_db(c)
    yield c
    c.close()


def _seeded(conn):
    from app import db
    from app.services import seed, engine
    seed.seed(conn)
    s = db.get_settings(conn)
    return s, engine.load_all(conn, s)


def _by_name(stats, name):
    return next(v for v in stats.values() if v["name"] == name)


def test_seed_counts_and_benchmarks(conn):
    s, st = _seeded(conn)
    assert len(st) == 60
    bills = conn.execute("SELECT COUNT(*) n FROM transactions WHERE amount>0").fetchone()["n"]
    assert 300 <= bills <= 320
    a, b = _by_name(st, "Aarav Mehta"), _by_name(st, "Bhavna Rao")
    assert a["usual_interval"] == 20 and a["recency_days"] == 45
    assert round(a["overdue_ratio"], 2) == 2.25 and round(a["days_overdue"]) == 25
    assert a["status"] == "HIGH_PRIORITY"
    assert b["usual_interval"] == 90 and b["recency_days"] == 55
    assert round(b["overdue_ratio"], 2) == 0.61 and b["status"] == "ON_TRACK"


def test_reset_is_repeatable(conn):
    from app.services import seed, engine
    from app import db
    s, st1 = _seeded(conn)
    seed.seed(conn)
    st2 = engine.load_all(conn, db.get_settings(conn))
    f = lambda st: sorted((v["name"], v["status"], v["priority_score"], round(v["lifetime_spend"])) for v in st.values())
    assert f(st1) == f(st2)


def test_confidence_labels(conn):
    from app.services import engine
    s = {}
    cust = {"id": 1, "name": "T", "phone": "919000000001", "phone_raw": "", "email": None, "notes": "", "tags": "",
            "consent": "Yes", "is_demo": 0, "created_at": "2026-01-01 10:00:00"}
    today = date(2026, 6, 30)

    def tx(days):
        return [{"status": "valid", "amount": 1000.0, "txn_date": (today - timedelta(days=d)).isoformat()} for d in days]
    labels = {n: engine.analyze_customer(cust, tx([10 + 20 * i for i in range(n)]), s, today)["confidence"] for n in range(0, 6)}
    assert labels[0] == "No purchase history"
    assert labels[1] == "Insufficient history"
    assert labels[2] == "Preliminary"
    assert labels[3] == "Moderate"
    assert labels[4] == labels[5] == "Higher confidence"
    one = engine.analyze_customer(cust, tx([10]), s, today)
    assert one["usual_interval"] is None and one["overdue_ratio"] is None


def test_same_day_bills_one_visit_and_duplicate_bill_id(conn):
    from app.services import ledger, engine
    from app import db
    cid, e = ledger.add_customer(conn, "Test Person", "98100 12345", consent="Yes")
    assert not e
    d = datetime.now() - timedelta(days=3)
    _, e1 = ledger.add_transaction(conn, cid, d, 1000, "B-1")
    _, e2 = ledger.add_transaction(conn, cid, d + timedelta(hours=1), 500, "B-2")
    _, e3 = ledger.add_transaction(conn, cid, d, 1000, "b-1")        # duplicate id (case-insensitive)
    assert not e1 and not e2 and e3
    st = engine.load_all(conn, db.get_settings(conn))[cid]
    assert st["bill_count"] == 2 and st["visit_count"] == 1 and st["lifetime_spend"] == 1500
    assert st["status"] == "ONE_TIME" and st["confidence"] == "Insufficient history"


def test_new_transaction_recalculates(conn):
    from app.services import ledger, engine
    from app import db
    s, st = _seeded(conn)
    a = _by_name(st, "Aarav Mehta")
    before_n = a["bill_count"]
    _, errs = ledger.add_transaction(conn, a["id"], datetime.now(), 4000, "NEW-1")
    assert not errs
    a2 = engine.load_all(conn, db.get_settings(conn))[a["id"]]
    assert a2["bill_count"] == before_n + 1 and a2["recency_days"] == 0 and a2["status"] == "ON_TRACK"
    assert a2["lifetime_spend"] == a["lifetime_spend"] + 4000


def test_phone_validation_and_duplicates(conn):
    from app.services import ledger
    _, e = ledger.add_customer(conn, "X", "12345")
    assert e
    cid, e = ledger.add_customer(conn, "Y", "+91 98100-12345")
    assert not e
    _, e = ledger.add_customer(conn, "Z", "09810012345")
    assert e and "already belongs" in e[0]


def test_audience_exclusions_and_demo_never_live(conn):
    from app.services import campaigns
    from app import db
    s, st = _seeded(conn)
    settings = db.get_settings(conn)
    aud = campaigns.build_audience(st, settings, "overdue", "SIMULATION")
    assert all(x["consent"] == "Yes" and x["phone"] and not x["in_cooldown"] for x in aud["included"])
    reasons = " ".join(w for _, why in aud["excluded"] for w in why)
    assert "Consent unknown" in reasons or "Cooldown" in reasons or "Invalid phone" in reasons
    live = campaigns.build_audience(st, settings, "all", "LIVE_META")
    assert live["included"] == []                       # every seeded contact is demo
    assert any("Demo contact" in w for _, why in live["excluded"] for w in why)


def test_cooldown_excludes(conn):
    from app.services import campaigns
    from app import db
    s, st = _seeded(conn)
    cooling = [v for v in st.values() if v["in_cooldown"]]
    assert len(cooling) >= 5
    aud = campaigns.build_audience(st, db.get_settings(conn), "all", "SIMULATION")
    assert not {c["id"] for c in cooling} & {i["id"] for i in aud["included"]}


def test_caps_and_cost(conn):
    from app.services import campaigns
    from app import db
    s, st = _seeded(conn)
    settings = db.get_settings(conn)
    full = campaigns.build_audience(st, settings, "offer_eligible", "SIMULATION")
    n = len(full["included"])
    assert n > 3
    cap = campaigns.build_audience(st, settings, "offer_eligible", "SIMULATION", max_recipients=3)
    assert len(cap["included"]) == 3
    # highest priority first
    assert [x["id"] for x in cap["included"]] == [x["id"] for x in full["included"][:3]]
    c = campaigns.cost_of(10, 1.2, 0, 18)
    assert c["base"] == 12.0 and c["gst"] == 2.16 and c["total"] == 14.16
    per = full["cost"]["per_msg"]
    bud = campaigns.build_audience(st, settings, "offer_eligible", "SIMULATION", budget_cap=per * 4.5)
    assert len(bud["included"]) == 4


FORM = dict(name="Test", offer="10% off", offer_code="T10", expiry="2026-12-31",
            template="Hi {{name}}, {{days_since_last_visit}} days! Code {{offer_code}} from {{shop_name}}",
            segment="offer_eligible", mode="SIMULATION")


def test_simulation_campaign_end_to_end(conn):
    from app.services import campaigns
    from app import db
    s, st = _seeded(conn)
    settings = db.get_settings(conn)
    cid, errs = campaigns.create_campaign(conn, dict(FORM), st, settings)
    assert cid and not errs
    r = campaigns.dispatch_campaign(conn, cid)
    assert r["claimed"] and r["status"] == "DONE"
    rows = conn.execute("SELECT status, rendered_text FROM campaign_recipients WHERE campaign_id=?", (cid,)).fetchall()
    assert rows and {x["status"] for x in rows} == {"SIMULATED"}
    assert all("{{" not in x["rendered_text"] for x in rows)
    again = campaigns.dispatch_campaign(conn, cid)       # no duplicate dispatch
    assert again["claimed"] is False
    assert conn.execute("SELECT COUNT(*) n FROM campaign_recipients WHERE campaign_id=?", (cid,)).fetchone()["n"] == len(rows)


def test_second_campaign_respects_cooldown(conn):
    from app.services import campaigns, engine
    from app import db
    s, st = _seeded(conn)
    settings = db.get_settings(conn)
    cid, _ = campaigns.create_campaign(conn, dict(FORM), st, settings)
    campaigns.dispatch_campaign(conn, cid)
    st2 = engine.load_all(conn, settings)
    aud = campaigns.build_audience(st2, settings, "offer_eligible", "SIMULATION")
    assert aud["included"] == []                          # everyone just contacted is in cooldown


def test_live_blocked_without_config(conn):
    from app.services import campaigns
    from app import db
    s, st = _seeded(conn)
    f = dict(FORM, mode="LIVE_META", meta_template_name="promo_v1", confirm_live="on")
    cid, errs = campaigns.create_campaign(conn, f, st, db.get_settings(conn))
    assert cid is None and any("Live mode unavailable" in e for e in errs)


def test_scheduled_not_dispatched_early_and_recovery(conn):
    from app.services import campaigns
    from app import db
    s, st = _seeded(conn)
    settings = db.get_settings(conn)
    when = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    cid, errs = campaigns.create_campaign(conn, dict(FORM, scheduled_at=when), st, settings)
    assert cid and campaigns.due_scheduled(conn) == []
    conn.execute("UPDATE campaigns SET scheduled_at='2020-01-01 00:00:00' WHERE id=?", (cid,))
    conn.commit()
    assert cid in campaigns.due_scheduled(conn)
    # simulate a crash mid-run, then restart recovery
    conn.execute("UPDATE campaigns SET status='RUNNING' WHERE id=?", (cid,))
    conn.commit()
    campaigns.recover_after_restart(conn)
    assert campaigns.dispatch_campaign(conn, cid)["claimed"]
    assert not campaigns.dispatch_campaign(conn, cid)["claimed"]


def test_meta_sender_disabled_and_never_leaks_token(conn, monkeypatch):
    from app.services.senders import MetaCloudApiSender
    res = MetaCloudApiSender().send("919000000001", "x", template_name="t", language="en", params=[])
    assert res["status"] == "FAILED" and "disabled" in res["error"]

    import httpx
    monkeypatch.setenv("WHATSAPP_MODE", "live")
    for k, v in (("META_ACCESS_TOKEN", "SECRET123"), ("META_PHONE_NUMBER_ID", "1"), ("META_WABA_ID", "2"),
                 ("META_API_VERSION", "v99.0")):
        monkeypatch.setenv(k, v)

    def boom(request):
        raise httpx.ReadTimeout("timeout SECRET123", request=request)
    client = httpx.Client(transport=httpx.MockTransport(boom))
    res = MetaCloudApiSender(client).send("919000000001", "x", template_name="t", language="en", params=["a"])
    assert res["status"] == "UNKNOWN" and "SECRET123" not in (res["error"] or "")

    ok = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"messages": [{"id": "wamid.1"}]})))
    res = MetaCloudApiSender(ok).send("919000000001", "x", template_name="t", language="en", params=[])
    assert res["status"] == "SUBMITTED" and res["provider_id"] == "wamid.1"


CSV = (b"Mobile,Bill Date,Total,Invoice No,Customer\n"
       b"98100 00001,2026-01-05,1200,INV-1,Ravi\n"
       b"98100 00001,05/02/2026,800,INV-2,Ravi\n"
       b"98100 00002,2026-02-10,500,INV-3,Sita\n"
       b"98100 00002,2026-02-10,500,INV-3,Sita\n"
       b"not-a-phone,2026-02-11,300,INV-4,\n"
       b"98100 00003,bad-date,300,INV-5,X\n"
       b"98100 00004,2026-02-12,abc,INV-6,Y\n")


def test_csv_import_preview_commit_idempotent(conn):
    from app.services import ledger
    headers, rows = ledger.parse_csv(CSV)
    m = ledger.guess_mapping(headers)
    assert m["phone"] == "Mobile" and m["bill_id"] == "Invoice No" and m["amount"] == "Total"
    pv = ledger.process_rows(conn, rows, m, write=False)
    assert conn.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"] == 0     # preview writes nothing
    assert (pv["inserted"], pv["duplicates"], pv["invalid"], pv["unmatched"]) == (4, 1, 2, 1)
    res = ledger.process_rows(conn, rows, m, write=True, filename="t.csv")
    assert res["inserted"] == 4 and res["new_customers"] == 2
    assert conn.execute("SELECT COUNT(*) n FROM customers").fetchone()["n"] == 2
    assert conn.execute("SELECT COUNT(*) n FROM transactions WHERE customer_id IS NULL").fetchone()["n"] == 1
    again = ledger.process_rows(conn, rows, m, write=True, filename="t.csv")
    assert again["inserted"] == 0 and again["duplicates"] == 5
    new = conn.execute("SELECT consent FROM customers").fetchall()
    assert {r["consent"] for r in new} == {"Unknown"}


def test_attribution_window(conn):
    from app.services import engine
    s, st = _seeded(conn)
    att = engine.campaign_attribution(conn, 1)
    assert att["returns"] >= 1 and att["sales"] > 0

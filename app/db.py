"""SQLite access, schema and settings."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from . import config

BUSINESS_BASELINES = {"clothing": 45, "salon": 30, "cafe": 14, "gym": 30,
                      "clinic": 90, "retail": 30, "other": 45}

DEFAULT_SETTINGS = {
    "shop_name": "Urban Threads (Demo)",
    "business_type": "clothing",
    "timezone": "Asia/Kolkata",
    "baseline_days": "45",
    "due_soon_ratio": "1.1",
    "overdue_ratio": "1.5",
    "high_priority_ratio": "2.0",
    "inactive_ratio": "3.0",
    "inactive_days": "120",
    "decline_factor": "1.5",
    "vip_min_spend": "25000",
    "second_visit_window_days": "45",
    "one_time_lapse_days": "120",
    "cooldown_days": "14",
    "monthly_budget_cap": "0",
    "message_rate_inr": config.env("MESSAGE_RATE_INR", "1.20") or "1.20",
    "markup_inr": "0",
    "gst_percent": config.env("GST_PERCENT", "18") or "18",
    "rate_date": config.env("MESSAGE_RATE_EFFECTIVE_DATE", ""),
    "attribution_days": "14",
    "default_mode": "SIMULATION",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS customers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  phone TEXT UNIQUE,            -- normalized '91XXXXXXXXXX' or NULL when invalid
  phone_raw TEXT,
  email TEXT,
  notes TEXT DEFAULT '',
  tags TEXT DEFAULT '',
  consent TEXT NOT NULL DEFAULT 'Unknown' CHECK(consent IN ('Yes','No','Unknown')),
  is_demo INTEGER NOT NULL DEFAULT 0,
  source TEXT DEFAULT 'manual',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS imports(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filename TEXT, file_hash TEXT, created_at TEXT NOT NULL,
  total_rows INTEGER DEFAULT 0, inserted INTEGER DEFAULT 0, new_customers INTEGER DEFAULT 0,
  matched INTEGER DEFAULT 0, duplicates INTEGER DEFAULT 0, unmatched INTEGER DEFAULT 0,
  invalid INTEGER DEFAULT 0, skipped INTEGER DEFAULT 0, possible_dups INTEGER DEFAULT 0,
  mapping_json TEXT
);
CREATE TABLE IF NOT EXISTS transactions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id INTEGER REFERENCES customers(id),   -- NULL = unmatched, needs review
  bill_id TEXT COLLATE NOCASE,
  txn_date TEXT NOT NULL,                          -- local shop time, ISO
  amount REAL NOT NULL,                            -- net; negative = return/refund
  category TEXT, items TEXT,
  source TEXT NOT NULL DEFAULT 'manual',           -- demo | manual | pos_entry | csv
  import_id INTEGER REFERENCES imports(id),
  status TEXT NOT NULL DEFAULT 'valid',            -- valid | cancelled
  possible_dup INTEGER NOT NULL DEFAULT 0,
  raw_phone TEXT, raw_name TEXT,
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_txn_bill ON transactions(bill_id)
  WHERE bill_id IS NOT NULL AND bill_id <> '';
CREATE INDEX IF NOT EXISTS idx_txn_cust ON transactions(customer_id, txn_date);
CREATE TABLE IF NOT EXISTS campaigns(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, offer TEXT, offer_code TEXT, expiry TEXT, min_bill REAL,
  template TEXT NOT NULL,
  segment TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'SIMULATION',         -- SIMULATION | LIVE_META
  status TEXT NOT NULL,                            -- SCHEDULED | RUNNING | DONE | DONE_WITH_ERRORS | CANCELLED
  max_recipients INTEGER, budget_cap REAL,
  rate REAL, markup REAL, gst_percent REAL,
  est_recipients INTEGER DEFAULT 0, est_cost REAL DEFAULT 0,
  attribution_days INTEGER DEFAULT 14,
  meta_template_name TEXT, meta_language TEXT, meta_params TEXT,
  created_at TEXT NOT NULL, scheduled_at TEXT, dispatched_at TEXT, completed_at TEXT
);
CREATE TABLE IF NOT EXISTS campaign_recipients(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  rendered_text TEXT NOT NULL,
  status TEXT NOT NULL,         -- QUEUED SIMULATED SUBMITTED DELIVERED READ FAILED UNKNOWN
  provider_message_id TEXT, error TEXT, updated_at TEXT,
  UNIQUE(campaign_id, customer_id)
);
CREATE TABLE IF NOT EXISTS activity(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config.database_path()), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def get_conn():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def init_db(conn: sqlite3.Connection):
    conn.executescript(SCHEMA)
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?,?)", (k, v))
    conn.commit()


def get_settings(conn) -> dict:
    d = dict(DEFAULT_SETTINGS)
    for r in conn.execute("SELECT key, value FROM settings"):
        d[r["key"]] = r["value"]
    return d


def set_setting(conn, key: str, value):
    conn.execute("INSERT INTO settings(key, value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def log_activity(conn, kind: str, text: str):
    conn.execute("INSERT INTO activity(ts, kind, text) VALUES(?,?,?)", (now_iso(), kind, text))


def wipe_all(conn):
    for t in ("campaign_recipients", "campaigns", "transactions", "imports",
              "customers", "activity", "settings"):
        conn.execute(f"DELETE FROM {t}")
    conn.execute("DELETE FROM sqlite_sequence")
    conn.commit()
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?,?)", (k, v))
    conn.commit()

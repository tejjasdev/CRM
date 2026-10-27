"""Customer + transaction writes and CSV import. All validation lives here (not in templates)."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import date, datetime

from .. import config, db
from ..utils import normalize_phone, display_phone

CONSENTS = ("Yes", "No", "Unknown")


# ------------------------------------------------------------------ customers
def add_customer(conn, name, phone, email="", notes="", tags="", consent="Unknown", source="manual",
                 is_demo=0) -> tuple[int | None, list[str]]:
    errs = []
    name = (name or "").strip()
    if not name:
        errs.append("Name is required.")
    norm = normalize_phone(phone)
    if not norm:
        errs.append("Enter a valid 10-digit Indian mobile number (starts 6-9; +91 / 0 prefix allowed).")
    if consent not in CONSENTS:
        errs.append("Marketing consent must be Yes, No or Unknown.")
    email = (email or "").strip()
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        errs.append("Email looks invalid.")
    if norm:
        ex = conn.execute("SELECT id, name FROM customers WHERE phone=?", (norm,)).fetchone()
        if ex:
            errs.append(f"This phone number already belongs to {ex['name']} (customer #{ex['id']}).")
    if errs:
        return None, errs
    cur = conn.execute(
        "INSERT INTO customers(name, phone, phone_raw, email, notes, tags, consent, is_demo, source, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (name, norm, (phone or "").strip(), email or None, (notes or "").strip(), (tags or "").strip(),
         consent, is_demo, source, db.now_iso()))
    db.log_activity(conn, "customer", f"Customer added: {name}")
    touch(conn, "Manual entry")
    conn.commit()
    return cur.lastrowid, []


def update_consent(conn, customer_id: int, consent: str) -> bool:
    if consent not in CONSENTS:
        return False
    conn.execute("UPDATE customers SET consent=? WHERE id=?", (consent, customer_id))
    db.log_activity(conn, "customer", f"Consent set to {consent} for customer #{customer_id}")
    conn.commit()
    return True


def touch(conn, source_label: str):
    db.set_setting(conn, "data_updated", db.now_iso())
    cur = conn.execute("SELECT value FROM settings WHERE key='data_source'").fetchone()
    has_demo = conn.execute("SELECT 1 FROM customers WHERE is_demo=1 LIMIT 1").fetchone()
    has_real = conn.execute("SELECT 1 FROM customers WHERE is_demo=0 LIMIT 1").fetchone()
    if has_demo and has_real:
        label = "Demo data + your entries"
    elif has_real:
        label = "CSV import / manual" if source_label.startswith("CSV") else "Manual entry"
    else:
        label = "Demo data"
    db.set_setting(conn, "data_source", label)


# --------------------------------------------------------------- transactions
def parse_amount(v) -> float | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-")
    s = re.sub(r"[^\d.]", "", s)
    if not s or s == ".":
        return None
    try:
        a = float(s)
    except ValueError:
        return None
    return -a if neg else a


_DATE_FMTS = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d",
              "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%d-%m-%Y %H:%M", "%d-%m-%Y",
              "%d/%m/%y", "%d-%m-%y", "%d %b %Y", "%d %B %Y", "%d.%m.%Y"]


def parse_dt(v) -> datetime | None:
    s = str(v or "").strip()
    if not s:
        return None
    for f in _DATE_FMTS:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    return None


def add_transaction(conn, customer_id, txn_dt: datetime | None, amount, bill_id="", category="", items="",
                    source="manual", import_id=None, raw_phone=None, raw_name=None,
                    commit=True) -> tuple[int | None, list[str]]:
    errs = []
    if txn_dt is None:
        errs.append("A valid date is required.")
    elif txn_dt.date() > date.today():
        errs.append("Transaction date cannot be in the future.")
    amt = parse_amount(amount) if not isinstance(amount, (int, float)) else float(amount)
    if amt is None or amt == 0:
        errs.append("Enter a non-zero net amount (negative = return/refund).")
    bill_id = (bill_id or "").strip()
    if bill_id:
        ex = conn.execute("SELECT id FROM transactions WHERE bill_id=?", (bill_id,)).fetchone()
        if ex:
            errs.append(f"Bill ID {bill_id} already exists - it was not counted twice.")
    if customer_id is not None and not conn.execute("SELECT 1 FROM customers WHERE id=?", (customer_id,)).fetchone():
        errs.append("Customer not found.")
    if errs:
        return None, errs
    possible = 0
    if not bill_id and customer_id is not None:
        # no external ID: do not silently merge; flag for review instead
        possible = 1 if conn.execute(
            "SELECT 1 FROM transactions WHERE customer_id=? AND date(txn_date)=date(?) AND amount=?",
            (customer_id, txn_dt.isoformat(sep=" "), amt)).fetchone() else 0
    cur = conn.execute(
        "INSERT INTO transactions(customer_id, bill_id, txn_date, amount, category, items, source, import_id,"
        " possible_dup, raw_phone, raw_name, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (customer_id, bill_id or None, txn_dt.isoformat(sep=" "), amt, (category or "").strip() or None,
         (items or "").strip() or None, source, import_id, possible, raw_phone, raw_name, db.now_iso()))
    if commit:
        who = "unmatched" if customer_id is None else f"customer #{customer_id}"
        db.log_activity(conn, "transaction", f"Bill {bill_id or '(no id)'} {amt:+,.0f} saved for {who}")
        touch(conn, "Manual entry")
        conn.commit()
    return cur.lastrowid, ([] if not possible else [])


def assign_unmatched(conn, txn_id: int, phone: str, name: str) -> list[str]:
    norm = normalize_phone(phone)
    if not norm:
        return ["Enter a valid mobile number to assign this bill."]
    row = conn.execute("SELECT id FROM customers WHERE phone=?", (norm,)).fetchone()
    if row:
        cid = row["id"]
    else:
        cid, errs = add_customer(conn, name or f"Customer {norm[-4:]}", phone, source="manual")
        if errs:
            return errs
    conn.execute("UPDATE transactions SET customer_id=? WHERE id=? AND customer_id IS NULL", (cid, txn_id))
    touch(conn, "Manual entry")
    conn.commit()
    return []


# ------------------------------------------------------------------ CSV import
FIELDS = [("phone", "Customer phone", True), ("date", "Date / time", True), ("amount", "Net amount", True),
          ("name", "Customer name", False), ("bill_id", "Bill / invoice ID", False), ("category", "Item / category", False)]
_GUESS = {
    "phone": ["phone", "mobile", "contact", "cell", "whatsapp", "customer phone", "mobile no"],
    "date": ["date", "datetime", "bill date", "invoice date", "txn date", "time"],
    "amount": ["amount", "total", "net", "net amount", "bill amount", "grand total", "value"],
    "name": ["name", "customer", "customer name"],
    "bill_id": ["bill", "bill id", "bill no", "invoice", "invoice no", "invoice id", "receipt"],
    "category": ["category", "item", "items", "product", "department"],
}


def parse_csv(raw: bytes) -> tuple[list[str], list[dict]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rdr = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = [h.strip() for h in (rdr.fieldnames or []) if h]
    rows = []
    for r in rdr:
        rows.append({(k or "").strip(): (v or "").strip() for k, v in r.items() if k})
    return headers, rows


def guess_mapping(headers: list[str]) -> dict:
    lower = {h.lower(): h for h in headers}
    out = {}
    for key, names in _GUESS.items():
        for n in names:
            if n in lower:
                out[key] = lower[n]
                break
        else:
            for h in headers:
                if any(n in h.lower() for n in names):
                    out[key] = h
                    break
    return out


def save_upload(raw: bytes) -> str:
    h = hashlib.sha256(raw).hexdigest()[:24]
    (config.import_dir() / f"{h}.csv").write_bytes(raw)
    return h


def load_upload(h: str) -> bytes | None:
    if not re.fullmatch(r"[0-9a-f]{24}", h or ""):
        return None
    p = config.import_dir() / f"{h}.csv"
    return p.read_bytes() if p.exists() else None


def process_rows(conn, rows: list[dict], mapping: dict, write: bool, filename: str = "", file_hash: str = "") -> dict:
    """Classify (and optionally write) every row. With write=False nothing is persisted."""
    res = {"total": len(rows), "inserted": 0, "new_customers": 0, "matched": 0, "duplicates": 0,
           "unmatched": 0, "invalid": 0, "skipped": 0, "possible_dups": 0, "preview": [], "errors": []}
    import_id = None
    if write:
        cur = conn.execute("INSERT INTO imports(filename, file_hash, created_at, mapping_json) VALUES(?,?,?,?)",
                           (filename, file_hash, db.now_iso(), json.dumps(mapping)))
        import_id = cur.lastrowid
    seen_bills: set[str] = set()
    seen_phone: dict[str, int] = {}          # simulated new customers during preview
    sig_seen: set[tuple] = set()

    def g(row, key):
        col = mapping.get(key)
        return (row.get(col, "") if col else "").strip()

    for i, row in enumerate(rows, start=2):       # 2 = first data line in the file
        vals = {k: g(row, k) for k, _, _ in FIELDS}
        if not any(vals.values()):
            res["skipped"] += 1
            continue
        verdict, note = "insert", ""
        dt, amt = parse_dt(vals["date"]), parse_amount(vals["amount"])
        norm = normalize_phone(vals["phone"])
        bill = vals["bill_id"]
        if dt is None:
            verdict, note = "invalid", f"Unrecognised date '{vals['date']}'"
        elif dt.date() > date.today():
            verdict, note = "invalid", "Date is in the future"
        elif amt is None or amt == 0:
            verdict, note = "invalid", f"Bad amount '{vals['amount']}'"
        elif bill and (bill.lower() in seen_bills or conn.execute(
                "SELECT 1 FROM transactions WHERE bill_id=?", (bill,)).fetchone()):
            verdict, note = "duplicate", f"Bill {bill} already imported"
        if verdict == "insert":
            if bill:
                seen_bills.add(bill.lower())
            cust = conn.execute("SELECT id FROM customers WHERE phone=?", (norm,)).fetchone() if norm else None
            if not norm:
                verdict, note = "unmatched", "Missing/invalid phone - saved for review, not campaign-eligible"
            elif cust or norm in seen_phone:
                verdict, note = "matched", "Existing customer"
            else:
                verdict, note = "new_customer", "New customer will be created (consent = Unknown)"
        # possible duplicate when there is no bill id
        possible = False
        if verdict in ("matched", "new_customer", "unmatched") and not bill:
            sig = (norm, dt.date(), amt)
            if sig in sig_seen:
                possible = True
            elif norm:
                c = conn.execute("SELECT id FROM customers WHERE phone=?", (norm,)).fetchone()
                if c and conn.execute("SELECT 1 FROM transactions WHERE customer_id=? AND date(txn_date)=date(?) AND amount=?",
                                      (c["id"], dt.isoformat(sep=" "), amt)).fetchone():
                    possible = True
            sig_seen.add(sig)
        if possible:
            res["possible_dups"] += 1
            note += " (possible duplicate - flagged for review, still imported)"

        if verdict == "invalid":
            res["invalid"] += 1
            res["errors"].append(f"Line {i}: {note}")
        elif verdict == "duplicate":
            res["duplicates"] += 1
        else:
            res["inserted"] += 1
            if verdict == "unmatched":
                res["unmatched"] += 1
            elif verdict == "matched":
                res["matched"] += 1
            elif verdict == "new_customer":
                res["new_customers"] += 1
                if norm not in seen_phone:
                    seen_phone[norm] = 1
                if write:
                    cust = conn.execute("SELECT id FROM customers WHERE phone=?", (norm,)).fetchone()
                    if not cust:
                        nm = vals["name"] or f"Customer {norm[-4:]}"
                        conn.execute(
                            "INSERT INTO customers(name, phone, phone_raw, consent, is_demo, source, created_at)"
                            " VALUES(?,?,?, 'Unknown', 0, 'csv', ?)", (nm, norm, vals["phone"], db.now_iso()))
            if write:
                cust = conn.execute("SELECT id FROM customers WHERE phone=?", (norm,)).fetchone() if norm else None
                txid, errs = add_transaction(conn, cust["id"] if cust else None, dt, amt, bill, vals["category"],
                                             source="csv", import_id=import_id, raw_phone=vals["phone"],
                                             raw_name=vals["name"], commit=False)
                if errs:      # race / validation at write time -> count as duplicate/invalid, never crash
                    res["inserted"] -= 1
                    res["duplicates"] += 1
                elif possible:
                    conn.execute("UPDATE transactions SET possible_dup=1 WHERE id=?", (txid,))
        if len(res["preview"]) < 15:
            res["preview"].append({"line": i, "verdict": verdict, "note": note, **vals,
                                   "phone_norm": display_phone(norm, vals["phone"]) if norm else vals["phone"],
                                   "date_parsed": dt.strftime("%d %b %Y %H:%M") if dt else "",
                                   "amount_parsed": amt})
    if write:
        conn.execute("""UPDATE imports SET total_rows=?, inserted=?, new_customers=?, matched=?, duplicates=?, unmatched=?,
                        invalid=?, skipped=?, possible_dups=? WHERE id=?""",
                     (res["total"], res["inserted"], res["new_customers"], res["matched"], res["duplicates"],
                      res["unmatched"], res["invalid"], res["skipped"], res["possible_dups"], import_id))
        db.log_activity(conn, "import", f"CSV import '{filename}': {res['inserted']} bills added, "
                                        f"{res['duplicates']} duplicates, {res['invalid']} invalid")
        touch(conn, "CSV import")
        conn.commit()
        res["import_id"] = import_id
    return res

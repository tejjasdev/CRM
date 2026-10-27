"""Formatting + phone helpers (pure functions)."""
import re
from datetime import date, datetime


def fmt_inr(x, decimals: int = 0) -> str:
    if x is None:
        return "—"
    neg = x < 0
    x = abs(float(x))
    s = f"{x:.{decimals}f}"
    ip, _, fp = s.partition(".")
    if len(ip) > 3:
        head, tail = ip[:-3], ip[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        ip = ",".join(groups + [tail])
    out = "₹" + ip + ("." + fp if fp else "")
    return "-" + out if neg else out


def to_date(v) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.fromisoformat(str(v)).date()


def fmt_date(v) -> str:
    d = to_date(v)
    return d.strftime("%d %b %Y") if d else "—"


def fmt_dt(v) -> str:
    if not v:
        return "—"
    dt = datetime.fromisoformat(str(v))
    return dt.strftime("%d %b %Y, %H:%M")


def normalize_phone(raw) -> str | None:
    """Return Indian mobile as '91XXXXXXXXXX' or None if invalid."""
    if raw is None:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) == 10 and digits[0] in "6789":
        return "91" + digits
    return None


def display_phone(norm: str | None, raw: str | None = None) -> str:
    if norm and len(norm) == 12:
        return f"+91 {norm[2:7]} {norm[7:]}"
    return (raw or "").strip() or "—"


def first_name(name: str) -> str:
    return (name or "").strip().split(" ")[0] or "there"

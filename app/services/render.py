"""Safe message templating: only whitelisted {{placeholders}}; no code execution."""
import re

ALLOWED = ["name", "offer", "offer_code", "days_since_last_visit", "shop_name", "expiry", "min_bill"]
_PH = re.compile(r"\{\{\s*([a-zA-Z_]+)\s*\}\}")


def unknown_placeholders(template: str) -> list[str]:
    return sorted({m for m in _PH.findall(template or "") if m not in ALLOWED})


def render(template: str, values: dict) -> str:
    def sub(m):
        k = m.group(1)
        if k not in ALLOWED:
            return m.group(0)
        v = values.get(k)
        return "" if v is None else str(v)
    return _PH.sub(sub, template or "")


def values_for(cust: dict, campaign: dict, settings: dict) -> dict:
    first = (cust["name"] or "").strip().split(" ")[0] or "there"
    days = cust.get("recency_days")
    return {
        "name": first,
        "offer": campaign.get("offer") or "",
        "offer_code": campaign.get("offer_code") or "",
        "days_since_last_visit": days if days is not None else "a few",
        "shop_name": settings.get("shop_name", ""),
        "expiry": campaign.get("expiry") or "",
        "min_bill": (f"₹{int(float(campaign['min_bill']))}" if campaign.get("min_bill") else ""),
    }

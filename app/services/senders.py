"""Message senders. SimulatedSender is the default; MetaCloudApiSender is the official
WhatsApp Business Platform (Cloud API) adapter and stays disabled until fully configured.

NOTE: MetaCloudApiSender has NOT been exercised against the real API in this prototype
(needs credentials, an approved marketing template and an opted-in test recipient)."""
from __future__ import annotations

import httpx

from .. import config


class SendResult(dict):
    """status: SIMULATED | SUBMITTED | FAILED | UNKNOWN ; provider_id ; error"""


class SimulatedSender:
    name = "SIMULATION"

    def send(self, to_phone: str, text: str, **_kw) -> SendResult:
        # Nothing leaves this machine. Never reported as sent/delivered.
        return SendResult(status="SIMULATED", provider_id=None, error=None)


class MetaCloudApiSender:
    name = "LIVE_META"

    def __init__(self, client: httpx.Client | None = None):
        self.cfg = config.meta_config()
        self.client = client or httpx.Client(timeout=15.0)

    def _scrub(self, s: str) -> str:
        tok = self.cfg.get("token")
        return s.replace(tok, "[redacted]") if tok and s else s

    def send(self, to_phone: str, text: str, *, template_name: str, language: str,
             params: list[str], **_kw) -> SendResult:
        ready = config.live_readiness()
        if not ready["ready"]:
            return SendResult(status="FAILED", provider_id=None,
                              error="Live sending is disabled: missing " + ", ".join(ready["missing"]))
        url = (f"https://graph.facebook.com/{self.cfg['api_version']}/"
               f"{self.cfg['phone_number_id']}/messages")
        payload = {
            "messaging_product": "whatsapp", "to": to_phone, "type": "template",
            "template": {"name": template_name, "language": {"code": language or "en"},
                         "components": [{"type": "body",
                                         "parameters": [{"type": "text", "text": str(p)} for p in params]}]
                         if params else []},
        }
        try:
            r = self.client.post(url, json=payload,
                                 headers={"Authorization": f"Bearer {self.cfg['token']}"})
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            # Ambiguous: the request may have reached Meta. Never auto-retry.
            return SendResult(status="UNKNOWN", provider_id=None,
                              error=self._scrub(f"No confirmed response ({type(e).__name__}). "
                                                "Do not resend; reconcile in Meta Business Manager."))
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code // 100 == 2 and body.get("messages"):
            return SendResult(status="SUBMITTED", provider_id=body["messages"][0].get("id"), error=None)
        if r.status_code >= 500:
            return SendResult(status="UNKNOWN", provider_id=None,
                              error=self._scrub(f"Provider error {r.status_code}; outcome uncertain - reconcile before resending."))
        err = (body.get("error") or {})
        return SendResult(status="FAILED", provider_id=None,
                          error=self._scrub(f"{r.status_code}: {err.get('message') or 'request rejected'}"))


def get_sender(mode: str):
    return MetaCloudApiSender() if mode == "LIVE_META" else SimulatedSender()

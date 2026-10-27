"""Environment configuration. Secrets are read here and never passed to templates."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env_file():
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_file()


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def app_env() -> str:
    return env("APP_ENV", "demo")


def database_path() -> Path:
    url = env("DATABASE_URL", "sqlite:///./data/returnloop.db")
    if url.startswith("sqlite:///"):
        raw = url[len("sqlite:///"):]
    else:
        raw = url
    p = Path(raw)
    if not p.is_absolute():
        p = ROOT / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def import_dir() -> Path:
    d = database_path().parent / "imports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def meta_config() -> dict:
    """Server-side only. Never serialise this dict into a response or log."""
    return {
        "token": env("META_ACCESS_TOKEN"),
        "phone_number_id": env("META_PHONE_NUMBER_ID"),
        "waba_id": env("META_WABA_ID"),
        "api_version": env("META_API_VERSION"),
    }


def live_readiness() -> dict:
    """Booleans only - safe to show in the UI."""
    cfg = meta_config()
    missing = []
    if env("WHATSAPP_MODE", "simulation").lower() != "live":
        missing.append("WHATSAPP_MODE=live")
    for label, key in (("META_ACCESS_TOKEN", "token"),
                       ("META_PHONE_NUMBER_ID", "phone_number_id"),
                       ("META_WABA_ID", "waba_id"),
                       ("META_API_VERSION", "api_version")):
        if not cfg[key]:
            missing.append(label)
    return {"ready": not missing, "missing": missing,
            "mode_env": env("WHATSAPP_MODE", "simulation").lower()}

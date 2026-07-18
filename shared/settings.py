"""Shared settings store — the single source of truth for service configuration.

The dashboard writes here (via the relay's /api/settings endpoint); the
registration service reads from here at task time. Stored as JSON under
/data/settings.json so it survives container restarts and is shared across the
compose stack via the same volume as accounts.db.

All secrets (captcha keys, SMS keys, proxy creds) live ONLY in this file on the
server. They are never logged and never committed to git.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

_DB_ENV = os.environ.get("DB_PATH")
_DATA_DIR = (Path(_DB_ENV).parent if _DB_ENV else (Path(__file__).resolve().parent.parent / "data"))
SETTINGS_PATH = _DATA_DIR / "settings.json"

_LOCK = threading.Lock()

DEFAULTS: dict[str, Any] = {
    "captcha": {
        "provider": "none",          # none | yescaptcha | twocaptcha | capsolver
        "client_key": "",            # API key for the chosen solver
    },
    "sms": {
        "provider": "none",          # none | smsactivate | herosms
        "api_key": "",
        "country": "us",
    },
    "email": {
        "backend": "local",          # local | moemail | cloudflare | duckmail
        # backend-specific creds go here, e.g. {"api_key": "..."}
        "config": {},
    },
    "proxy": {
        "mode": "none",              # none | static | pool
        "static": "",                # single proxy URL
        "pool_api": "",              # dynamic proxy extraction API
    },
    "registration": {
        "headless": True,
        "concurrency": 1,
        "delay_min": 3,
        "delay_max": 8,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return dict(DEFAULTS)
    try:
        with SETTINGS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return _deep_merge(DEFAULTS, data)
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULTS)


def save_settings(patch: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        current = load_settings()
        merged = _deep_merge(current, patch)
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SETTINGS_PATH.open("w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2, ensure_ascii=False)
        # chmod so the secret file isn't world-readable inside the container.
        try:
            os.chmod(SETTINGS_PATH, 0o600)
        except OSError:
            pass
        return merged


def get_section(name: str) -> dict[str, Any]:
    return load_settings().get(name, {})

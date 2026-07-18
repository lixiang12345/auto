"""Registration service — automated account creation for Grok / Codex / Gemini.

Design
------
- Provider-agnostic plugin interface; each provider implements `register()`.
- Accounts are written into the SHARED SQLite (same file the relay reads), so
  registered accounts become immediately usable by the relay — no extra sync.
- Email verification uses a temp-mail provider; phone verification (where
  required) uses a pluggable SMS provider (configure YOUR own credentials in
  .env — this module does not ship any provider secrets).
- Browser automation via Playwright (headless Chromium). Run inside Docker
  with --no-sandbox; on macOS host you can also run headed for debugging.

This is the "registration" layer of the four-layer system. Claude is NOT
auto-registered here (no public signup plugin exists); Claude accounts are
ingested manually via the dashboard /api/accounts endpoint.

NOTE: automated registration may violate provider Terms of Service. Use only
with accounts/identities you are authorized to create, and at your own risk.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Shared DB layer (single source of truth with the relay).
import sys
sys.path.insert(0, "/app/shared")
from db import create_account  # noqa: E402

DATA_DIR = Path("/data")
EXPORT_CSV = DATA_DIR / "export.csv"


@dataclass
class RegConfig:
    provider: str
    email_provider: str = "temp"
    proxy: Optional[str] = None
    sms_api_key: Optional[str] = None
    sms_country: str = "us"
    headless: bool = True
    count: int = 1
    concurrency: int = 1
    delay: tuple[int, int] = (3, 8)


@dataclass
class RegResult:
    ok: bool
    provider: str
    email: Optional[str] = None
    creds: dict[str, Any] = field(default_factory=dict)
    note: Optional[str] = None
    error: Optional[str] = None


class ProviderPlugin(ABC):
    name: str = "base"

    def __init__(self, cfg: RegConfig):
        self.cfg = cfg

    @abstractmethod
    async def register(self) -> RegResult:
        """Perform one registration. Must be safe to retry on failure."""
        ...


# --- Temp mail (pluggable; defaults to a local disposable mailbox stub) ---

class TempMail:
    """Minimal temp-mail abstraction.

    Ships with a 'local' backend that generates addresses but cannot receive
    mail — replace with a real provider (e.g. your MoeMail/Cloudflare Worker)
    by setting MAIL_BACKEND and credentials in .env. The interface is what
    matters for the pipeline; wire your own receiving backend here.
    """

    def __init__(self, backend: str = "local"):
        self.backend = backend

    def acquire(self) -> tuple[str, str]:
        ts = int(time.time())
        addr = f"auto{ts}@example.invalid"
        return addr, "local-unsupported"

    async def wait_code(self, token: str, timeout: int = 60) -> Optional[str]:
        # Real backends poll their API here.
        return None


# --- Registry ---

PLUGINS: dict[str, type[ProviderPlugin]] = {}


def register_plugin(cls: type[ProviderPlugin]):
    PLUGINS[cls.name] = cls
    return cls


# --- Provider implementations (structural; wire real flows per site) ---

@register_plugin
class GrokPlugin(ProviderPlugin):
    name = "grok"

    async def register(self) -> RegResult:
        mail = TempMail(self.cfg.email_provider)
        email, _ = mail.acquire()
        # Real flow: Playwright -> accounts.x.ai signup -> verify email -> (phone if required) -> export SSO/OIDC.
        # This skeleton returns the structure the relay expects; implement the
        # site-specific steps in browser/ per the documented grok-regkit flow.
        await asyncio.sleep(0)  # placeholder for the automation work
        return RegResult(
            ok=False,
            provider="grok",
            email=email,
            error="skeleton: implement site flow (see docs/registration.md)",
        )


@register_plugin
class CodexPlugin(ProviderPlugin):
    name = "codex"

    async def register(self) -> RegResult:
        mail = TempMail(self.cfg.email_provider)
        email, _ = mail.acquire()
        # Real flow: chatgpt.com signup -> verify -> Codex PKCE OAuth token exchange.
        return RegResult(
            ok=False,
            provider="codex",
            email=email,
            error="skeleton: implement site flow (see docs/registration.md)",
        )


@register_plugin
class GeminiPlugin(ProviderPlugin):
    name = "gemini"

    async def register(self) -> RegResult:
        mail = TempMail(self.cfg.email_provider)
        email, _ = mail.acquire()
        # Real flow: accounts.google.com signup -> verify -> API key issuance.
        return RegResult(
            ok=False,
            provider="gemini",
            email=email,
            error="skeleton: implement site flow (see docs/registration.md)",
        )


# --- Task runner ---

async def run_batch(cfg: RegConfig) -> list[RegResult]:
    plugin_cls = PLUGINS.get(cfg.provider)
    if not plugin_cls:
        return [RegResult(ok=False, provider=cfg.provider, error="unknown provider")]

    sem = asyncio.Semaphore(max(1, cfg.concurrency))
    results: list[RegResult] = []

    async def worker(i: int):
        async with sem:
            if i > 0 and cfg.delay:
                await asyncio.sleep(cfg.delay[0])
            res = await plugin_cls(cfg).register()
            if res.ok:
                create_account(
                    provider=res.provider,
                    email=res.email,
                    auth_type=res.creds.get("auth_type", "oauth"),
                    creds=res.creds,
                    proxy=cfg.proxy,
                    source="auto-register",
                    note=res.note,
                )
                _append_export(res)
            results.append(res)

    await asyncio.gather(*[worker(i) for i in range(cfg.count)])
    return results


def _append_export(res: RegResult) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    header = not EXPORT_CSV.exists()
    with EXPORT_CSV.open("a", newline="", encoding="utf-8") as f:
        import csv
        w = csv.writer(f)
        if header:
            w.writerow(["provider", "email", "auth_type", "proxy", "creds_json"])
        w.writerow([
            res.provider,
            res.email or "",
            res.creds.get("auth_type", "oauth"),
            "",
            json.dumps(res.creds, ensure_ascii=False),
        ])


def config_from_env(provider: str) -> RegConfig:
    return RegConfig(
        provider=provider,
        email_provider=os.environ.get("MAIL_BACKEND", "local"),
        proxy=os.environ.get("REG_PROXY"),
        sms_api_key=os.environ.get("SMS_API_KEY"),
        sms_country=os.environ.get("SMS_COUNTRY", "us"),
        headless=os.environ.get("HEADLESS", "true").lower() != "false",
        count=int(os.environ.get("REG_COUNT", "1")),
        concurrency=int(os.environ.get("REG_CONCURRENCY", "1")),
    )

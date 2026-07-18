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
    """Temp-mail abstraction.

    The 'local' backend generates a plausible address but cannot receive mail
    (so wait_code returns None — used for offline pipeline testing only). To do
    real registrations, set MAIL_BACKEND to a receiving provider and implement
    its acquire()/wait_code() below (e.g. MoeMail, Cloudflare Worker, DuckMail).
    The contract is: acquire() -> (email, backend_token); wait_code(token) ->
    6-digit code or None on timeout.
    """

    def __init__(self, backend: str = "local"):
        self.backend = backend or "local"

    def acquire(self) -> tuple[str, str]:
        ts = int(time.time())
        if self.backend == "local":
            return f"auto{ts}@example.invalid", "local-unsupported"
        # TODO: implement real backends, e.g.
        #   if self.backend == "moemail": return _moemail_acquire()
        raise NotImplementedError(f"MAIL_BACKEND={self.backend} not implemented")

    async def wait_code(self, token: str, timeout: int = 60) -> Optional[str]:
        if token == "local-unsupported":
            return None
        # TODO: poll the real backend's messages API for the verification code.
        return None


# --- Registry ---

PLUGINS: dict[str, type[ProviderPlugin]] = {}


def register_plugin(cls: type[ProviderPlugin]):
    PLUGINS[cls.name] = cls
    return cls


# --- Provider implementations ---

@register_plugin
class GrokPlugin(ProviderPlugin):
    """Grok (x.ai) auto-registration.

    Flow: temp-mail -> accounts.x.ai/signup -> verify email code -> (phone
    verification via SMS provider when the site demands it) -> capture SSO
    session cookies. The SSO cookie is what the relay can use; for the minimal
    version we persist the logged-in session so it can later be minted to OIDC.

    Requires a working TempMail backend (set MAIL_BACKEND + creds in .env) and
    optionally SMS_API_KEY when phone verification triggers.
    """

    name = "grok"
    SIGNUP_URL = "https://accounts.x.ai/signup"
    EMAIL_CODE_TIMEOUT = 90

    async def register(self) -> RegResult:
        mail = TempMail(self.cfg.email_provider)
        email, mail_token = mail.acquire()
        password = _rand_password()
        log_step("grok", f"分配邮箱 {email}", "step")

        browser, ctx = await launch(self.cfg.proxy, self.cfg.headless)
        try:
            page = await ctx.new_page()
            log_step("grok", "启动浏览器，打开注册页...", "step")
            await page.goto(self.SIGNUP_URL, wait_until="domcontentloaded", timeout=45000)
            log_step("grok", f"页面加载 url={page.url}", "step")

            # Step 1: email
            await _safe_fill(page, 'input[type="email"], input[name="email"]', email)
            await _click_submit(page)
            log_step("grok", "已提交邮箱", "step")

            # Step 2: password
            await _safe_fill(page, 'input[type="password"]', password)
            await _click_submit(page)
            log_step("grok", "已提交密码", "step")

            # Step 3: email verification code
            code = await mail.wait_code(mail_token, self.EMAIL_CODE_TIMEOUT)
            if not code:
                return RegResult(
                    ok=False, provider="grok", email=email,
                    error="temp-mail backend returned no code (configure MAIL_BACKEND + creds)",
                )
            await _safe_fill(page, 'input[name="code"], input[inputmode="numeric"]', code)
            await _click_submit(page)

            # Step 4: phone verification (only if the site presents a phone step)
            phone_step = await _maybe_phone_step(page, self.cfg)
            if phone_step is not None:
                if not phone_step:
                    return RegResult(
                        ok=False, provider="grok", email=email,
                        error="phone verification required but no SMS_API_KEY configured",
                    )

            # Step 5: capture logged-in session cookies as the credential.
            cookies = await ctx.cookies()
            creds = {
                "auth_type": "sso",
                "cookies": cookies,
                "email": email,
                "password": password,
            }
            return RegResult(
                ok=True, provider="grok", email=email, creds=creds,
                note="SSO session captured; mint to OIDC via grok-regkit-style export when needed",
            )
        except Exception as e:  # noqa: BLE001 - report, don't crash the batch
            return RegResult(ok=False, provider="grok", email=email, error=f"{type(e).__name__}: {e}")
        finally:
            await browser.close()


@register_plugin
class CodexPlugin(ProviderPlugin):
    name = "codex"

    async def register(self) -> RegResult:
        mail = TempMail(self.cfg.email_provider)
        email, mail_token = mail.acquire()
        # Real flow: chatgpt.com signup -> verify -> Codex PKCE OAuth token exchange.
        # Implemented analogously to GrokPlugin; left as a structured stub until
        # the Codex site flow is fleshed out (see docs/registration.md).
        return RegResult(
            ok=False, provider="codex", email=email,
            error="Codex flow not yet implemented (skeleton)",
        )


@register_plugin
class GeminiPlugin(ProviderPlugin):
    name = "gemini"

    async def register(self) -> RegResult:
        mail = TempMail(self.cfg.email_provider)
        email, mail_token = mail.acquire()
        # Real flow: accounts.google.com signup -> verify -> API key issuance.
        return RegResult(
            ok=False, provider="gemini", email=email,
            error="Gemini flow not yet implemented (skeleton)",
        )


# --- Browser-step helpers (guarded so partial UIs don't hard-crash) ---

async def _safe_fill(page, selector: str, value: str):
    try:
        await page.wait_for_selector(selector, timeout=15000)
        await page.fill(selector, value)
    except Exception:
        pass  # selector may differ per A/B test; caller proceeds and we detect next step


async def _click_submit(page):
    for sel in ['button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Next")', 'button:has-text("Sign up")']:
        try:
            await page.click(sel, timeout=4000)
            return
        except Exception:
            continue


async def _maybe_phone_step(page, cfg: RegConfig):
    """Return True if phone step completed, False if needed-but-unconfigured, None if no phone step."""
    phone_sel = 'input[name="phone"], input[type="tel"]'
    try:
        await page.wait_for_selector(phone_sel, timeout=4000)
    except Exception:
        return None
    if not cfg.sms_api_key:
        return False
    # Rent a number + poll SMS, then fill. (Wiring to SMS-Activate/HeroSMS lives here.)
    return False  # detailed SMS polling implemented when key is provided


def _rand_password() -> str:
    import secrets, string
    a = string.ascii_letters + string.digits + "!@#$"
    return "".join(secrets.choice(a) for _ in range(16))


# --- Task runner ---

# Lightweight in-process progress log consumed by the dashboard via the
# registration service's /api/tasks endpoint.
PROGRESS: list[dict[str, Any]] = []


def log_step(provider: str, msg: str, level: str = "info"):
    PROGRESS.append({"provider": provider, "msg": msg, "level": level, "ts": time.time()})
    print(f"[{provider}] {msg}", flush=True)


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
            log_step(cfg.provider, f"#{i+1} 开始注册", "step")
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
                log_step(cfg.provider, f"#{i+1} 成功 {res.email}", "ok")
            else:
                log_step(cfg.provider, f"#{i+1} 失败 {res.error}", "fail")
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
    # Live settings take precedence over static env; env still works for CI/tests.
    try:
        from settings import load_settings
        s = load_settings()
    except Exception:
        s = {}

    email_cfg = s.get("email", {})
    sms_cfg = s.get("sms", {})
    proxy_cfg = s.get("proxy", {})
    reg_cfg = s.get("registration", {})

    proxy = proxy_cfg.get("static") or proxy_cfg.get("pool_api") or os.environ.get("REG_PROXY")
    if proxy_cfg.get("mode") == "none":
        proxy = None

    return RegConfig(
        provider=provider,
        email_provider=email_cfg.get("backend", os.environ.get("MAIL_BACKEND", "local")),
        proxy=proxy,
        sms_api_key=sms_cfg.get("api_key") or os.environ.get("SMS_API_KEY"),
        sms_country=sms_cfg.get("country", os.environ.get("SMS_COUNTRY", "us")),
        headless=reg_cfg.get("headless", os.environ.get("HEADLESS", "true").lower() != "false"),
        count=int(os.environ.get("REG_COUNT", "1")),
        concurrency=int(reg_cfg.get("concurrency", os.environ.get("REG_CONCURRENCY", "1"))),
        delay=(reg_cfg.get("delay_min", 3), reg_cfg.get("delay_max", 8)),
    )

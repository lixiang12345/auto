"""FastAPI relay: OpenAI-compatible endpoint + account management + observability.

Routes
  POST /v1/chat/completions   OpenAI-compatible, fans out across the pool
  GET  /v1/models             list pooled providers/models
  GET  /health                liveness
  GET  /api/accounts          list accounts (dashboard source)
  POST /api/accounts          ingest a manual account (Claude etc.)
  PATCH/DELETE /api/accounts/:id
  GET  /api/stats             aggregate stats for the dashboard
  POST /api/import-csv        bulk import from any-auto-register export
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

import httpx
import sys
from pathlib import Path

# Make the shared DB layer importable both in Docker (/app/shared) and locally.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from db import (
    create_account,
    get_account,
    import_csv,
    list_accounts,
    record_result,
    update_account,
)
from settings import load_settings, save_settings

app = FastAPI(title="Auto-Deploy Relay", version="0.1.0")

# In-memory request log (bounded). The dashboard polls this.
REQUEST_LOG: list[dict[str, Any]] = []
LOG_MAX = 200

PROVIDER_MODELS: dict[str, list[str]] = {
    "claude": ["claude-opus-4-5", "claude-sonnet-4-5", "claude-haiku-4-5"],
    "codex": ["gpt-5.5", "gpt-5.5-mini", "gpt-5.4"],
    "gemini": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-3-pro"],
    "grok": ["grok-4", "grok-4-fast", "grok-3"],
}

# Round-robin cursor per provider.
_CURSOR: dict[str, int] = {}


class AccountIn(BaseModel):
    provider: str = Field(..., pattern="^(claude|codex|gemini|grok)$")
    auth_type: str = "oauth"
    email: Optional[str] = None
    proxy: Optional[str] = None
    # Provider-specific secret material.
    #  codex/grok/claude(apikey): {"api_key": "..."}
    #  claude(oauth): {"access_token": "...", "refresh_token": "..."}
    creds: dict[str, Any]
    note: Optional[str] = None


def _pick_account(provider: str) -> Optional[dict[str, Any]]:
    active = list_accounts(provider=provider, status="active")
    if not active:
        return None
    idx = _CURSOR.get(provider, 0) % len(active)
    _CURSOR[provider] = idx + 1
    return active[idx]


def _log(entry: dict[str, Any]) -> None:
    entry["ts"] = time.time()
    REQUEST_LOG.append(entry)
    if len(REQUEST_LOG) > LOG_MAX:
        del REQUEST_LOG[: len(REQUEST_LOG) - LOG_MAX]


@app.get("/health")
async def health():
    return {"status": "ok", "accounts": len(list_accounts(status="active"))}


@app.get("/v1/models")
async def models():
    data = []
    for provider, models_ in PROVIDER_MODELS.items():
        for m in models_:
            data.append(
                {
                    "id": f"{provider}/{m}",
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": provider,
                }
            )
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    model = body.get("model", "")
    provider = model.split("/")[0]
    if provider not in PROVIDER_MODELS:
        # fall back to first available provider
        available = [p for p in PROVIDER_MODELS if list_accounts(provider=p, status="active")]
        if not available:
            raise HTTPException(status_code=503, detail="no active accounts in pool")
        provider = available[0]

    stream = bool(body.get("stream"))
    req_id = str(uuid.uuid4())[:8]
    last_err: Optional[str] = None

    # Try each active account (failover).
    candidates = list_accounts(provider=provider, status="active")
    for acct in candidates:
        try:
            from providers import (
                normalize_response,
                stream_normalize,
                translate_request,
            )

            url, headers, payload = translate_request(provider, body, acct["creds"])
            proxy = {"http://": acct["proxy"], "https://": acct["proxy"]} if acct.get("proxy") else None
            async with httpx.AsyncClient(timeout=120, proxy=proxy) as client:
                if stream:
                    async def event_stream():
                        async with client.stream("POST", url, headers=headers, json=payload) as r:
                            async for line in r.aiter_lines():
                                norm = await stream_normalize(provider, line)
                                if norm:
                                    yield norm + "\n"
                        yield "data: [DONE]\n\n"

                    record_result(acct["id"], True)
                    _log({"req_id": req_id, "provider": provider, "account_id": acct["id"],
                          "status": "ok", "stream": True})
                    return StreamingResponse(event_stream(), media_type="text/event-stream")
                else:
                    r = await client.post(url, headers=headers, json=payload)
                    if r.status_code >= 400:
                        last_err = f"{r.status_code} {r.text[:200]}"
                        record_result(acct["id"], False, last_err)
                        continue
                    out = normalize_response(provider, r.json())
                    record_result(acct["id"], True)
                    _log({"req_id": req_id, "provider": provider, "account_id": acct["id"],
                          "status": "ok"})
                    return JSONResponse(out)
        except Exception as e:  # noqa: BLE001 - failover over all candidates
            last_err = str(e)[:200]
            record_result(acct["id"], False, last_err)
            continue

    _log({"req_id": req_id, "provider": provider, "status": "fail", "error": last_err})
    raise HTTPException(status_code=502, detail=f"all accounts failed: {last_err}")


# ---- Account management (dashboard + ingestion) ----

@app.get("/api/accounts")
async def api_accounts(provider: Optional[str] = None, status: Optional[str] = None):
    return list_accounts(provider=provider, status=status)


@app.post("/api/accounts", status_code=201)
async def api_create(acct: AccountIn):
    _validate_creds(acct.provider, acct.auth_type, acct.creds)
    aid = create_account(
        provider=acct.provider,
        auth_type=acct.auth_type,
        creds=acct.creds,
        email=acct.email,
        proxy=acct.proxy,
        source="manual",
        note=acct.note,
    )
    return {"id": aid}


@app.patch("/api/accounts/{account_id}")
async def api_patch(account_id: int, patch: dict[str, Any]):
    acct = get_account(account_id)
    if not acct:
        raise HTTPException(status_code=404, detail="not found")
    update_account(
        account_id,
        status=patch.get("status"),
        proxy=patch.get("proxy"),
        note=patch.get("note"),
        creds=patch.get("creds"),
    )
    return {"ok": True}


@app.delete("/api/accounts/{account_id}")
async def api_delete(account_id: int):
    from db import get_conn

    conn = get_conn()
    try:
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.post("/api/import-csv")
async def api_import_csv(payload: dict[str, Any]):
    path = payload.get("path")
    if not path:
        raise HTTPException(status_code=400, detail="path required")
    count = import_csv(path)
    return {"imported": count}


@app.get("/api/stats")
async def api_stats():
    all_accts = list_accounts()
    by_provider: dict[str, dict[str, int]] = {}
    for a in all_accts:
        d = by_provider.setdefault(a["provider"], {"total": 0, "active": 0, "dead": 0})
        d["total"] += 1
        if a["status"] == "active":
            d["active"] += 1
        elif a["status"] == "dead":
            d["dead"] += 1
    return {
        "total_accounts": len(all_accts),
        "active_accounts": sum(1 for a in all_accts if a["status"] == "active"),
        "by_provider": by_provider,
        "recent_requests": REQUEST_LOG[-50:],
    }


# ---- Live settings (dashboard writes, registration reads) ----

@app.get("/api/settings")
async def api_get_settings():
    return load_settings()


@app.put("/api/settings")
async def api_put_settings(payload: dict[str, Any]):
    return save_settings(payload)


def _validate_creds(provider: str, auth_type: str, creds: dict[str, Any]) -> None:
    if auth_type == "apikey":
        if "api_key" not in creds:
            raise HTTPException(status_code=422, detail=f"{provider} apikey requires 'api_key'")
    elif auth_type == "oauth":
        if provider == "claude" and "access_token" not in creds:
            raise HTTPException(status_code=422, detail="claude oauth requires 'access_token'")

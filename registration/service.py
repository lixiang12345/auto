"""Registration service entrypoint: HTTP API + one-shot CLI.

API
  POST /api/register   {provider, count?, concurrency?, proxy?}  -> starts a batch
  GET  /api/tasks      list recent batch results
  GET  /health

CLI
  python service.py --provider grok --count 3
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import sys
sys.path.insert(0, "/app/registration")
sys.path.insert(0, "/app/shared")

from reg import PLUGINS, RegConfig, config_from_env, run_batch, PROGRESS  # noqa: E402

app = FastAPI(title="Auto-Deploy Registration", version="0.1.0")
TASKS: list[dict] = []


class RegReq(BaseModel):
    provider: str
    count: int = 1
    concurrency: int = 1
    proxy: Optional[str] = None


@app.get("/health")
@app.get("/api/health")
async def health():
    return {"status": "ok", "providers": list(PLUGINS.keys())}


@app.get("/api/tasks")
async def tasks():
    return TASKS[-50:]


@app.get("/api/progress")
async def progress():
    return PROGRESS[-100:]


@app.post("/api/register")
async def register(req: RegReq):
    if req.provider not in PLUGINS:
        raise HTTPException(status_code=400, detail=f"unknown provider {req.provider}")
    # Pull proxy/delay/concurrency from live settings; request overrides count.
    try:
        from settings import load_settings
        s = load_settings()
    except Exception:
        s = {}
    proxy_cfg = s.get("proxy", {})
    reg_cfg = s.get("registration", {})
    proxy = req.proxy or proxy_cfg.get("static") or proxy_cfg.get("pool_api") or os.environ.get("REG_PROXY")
    if proxy_cfg.get("mode") == "none":
        proxy = None
    cfg = RegConfig(
        provider=req.provider,
        proxy=proxy,
        count=req.count,
        concurrency=req.concurrency or reg_cfg.get("concurrency", 1),
        delay=(reg_cfg.get("delay_min", 3), reg_cfg.get("delay_max", 8)),
    )
    results = await run_batch(cfg)
    entry = {
        "provider": req.provider,
        "requested": req.count,
        "succeeded": sum(1 for r in results if r.ok),
        "results": [
            {"ok": r.ok, "email": r.email, "error": r.error, "note": r.note}
            for r in results
        ],
    }
    TASKS.append(entry)
    return entry


def _cli():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--provider", required=True, choices=list(PLUGINS.keys()))
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--proxy", default=None)
    p.add_argument("--timeout", type=int, default=120, help="per-account hard timeout (s)")
    a = p.parse_args()
    cfg = RegConfig(provider=a.provider, proxy=a.proxy, count=a.count, concurrency=a.concurrency)
    try:
        results = asyncio.run(asyncio.wait_for(run_batch(cfg), timeout=a.count * a.timeout + 30))
    except asyncio.TimeoutError:
        print("TIMEOUT: registration exceeded the per-account budget; the site likely blocked or stalled.")
        return
    for r in results:
        print(("OK " if r.ok else "FAIL"), r.provider, r.email, r.error or "")


if __name__ == "__main__":
    import uvicorn
    if os.environ.get("MODE") == "cli":
        _cli()
    else:
        uvicorn.run(app, host="0.0.0.0", port=8001)

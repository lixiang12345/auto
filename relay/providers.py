"""Upstream provider routing for the relay.

Each account in the pool carries provider-specific credentials. This module
turns an (account, openai-style request) into a concrete upstream call and
normalizes the response back into OpenAI chat-completions shape.

Supported providers and their upstream protocols:
  - claude : Anthropic Messages API (https://api.anthropic.com/v1/messages)
  - codex  : OpenAI Chat Completions (https://api.openai.com/v1/chat/completions)
  - gemini : Google Generative Language API (vertex-style REST)
  - grok   : xAI API (https://api.x.ai/v1/chat/completions)

For the minimal version we proxy the request bodies through with light
translation where the provider is already OpenAI-compatible (codex, grok) and
with a messages-format conversion for claude. Gemini is mapped via its
generateContent endpoint.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx


async def translate_request(provider: str, body: dict[str, Any], creds: dict[str, Any]) -> tuple[str, dict[str, str], Any]:
    """Return (url, headers, payload) for the upstream call."""
    if provider == "codex":
        return (
            "https://api.openai.com/v1/chat/completions",
            {"Authorization": f"Bearer {creds['api_key']}", "Content-Type": "application/json"},
            body,
        )
    if provider == "grok":
        return (
            "https://api.x.ai/v1/chat/completions",
            {"Authorization": f"Bearer {creds['api_key']}", "Content-Type": "application/json"},
            body,
        )
    if provider == "claude":
        # OpenAI chat -> Anthropic messages conversion.
        messages = body.get("messages", [])
        system = next((m["content"] for m in messages if m.get("role") == "system"), None)
        conv = [m for m in messages if m.get("role") != "system"]
        model = body.get("model", "claude-sonnet-4-5")
        # Strip the "claude/" prefix some clients send.
        model = model.split("/")[-1]
        payload: dict[str, Any] = {
            "model": model,
            "messages": conv,
            "max_tokens": body.get("max_tokens", 4096),
            "stream": body.get("stream", False),
        }
        if system:
            payload["system"] = system
        if body.get("temperature") is not None:
            payload["temperature"] = body["temperature"]
        return (
            "https://api.anthropic.com/v1/messages",
            {
                "x-api-key": creds["api_key"],
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            payload,
        )
    if provider == "gemini":
        model = body.get("model", "gemini-2.5-pro")
        model = model.split("/")[-1]
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:"
            f"{'streamGenerateContent' if body.get('stream') else 'generateContent'}"
            f"?alt=sse&key={creds['api_key']}"
        )
        # OpenAI chat -> Gemini contents.
        contents = _openai_to_gemini(body.get("messages", []))
        payload: dict[str, Any] = {"contents": contents}
        if body.get("temperature") is not None:
            payload["generationConfig"] = {"temperature": body["temperature"]}
        return url, {"Content-Type": "application/json"}, payload
    raise ValueError(f"unknown provider: {provider}")


def _openai_to_gemini(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        role = "model" if m.get("role") == "assistant" else "user"
        text = m.get("content", "")
        if isinstance(text, list):
            text = " ".join(p.get("text", "") for p in text if isinstance(p, dict))
        out.append({"role": role, "parts": [{"text": str(text)}]})
    return out


def normalize_response(provider: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Convert an upstream response into OpenAI chat-completions shape."""
    if provider in ("codex", "grok"):
        return raw  # already OpenAI-compatible
    if provider == "claude":
        text = "".join(
            b.get("text", "") for b in raw.get("content", []) if b.get("type") == "text"
        )
        return {
            "id": raw.get("id"),
            "object": "chat.completion",
            "model": raw.get("model"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": raw.get("stop_reason", "stop"),
                }
            ],
            "usage": raw.get("usage", {}),
        }
    if provider == "gemini":
        parts = raw.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
        return {
            "id": raw.get("responseId"),
            "object": "chat.completion",
            "model": None,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": raw.get("usageMetadata", {}),
        }
    return raw


async def stream_normalize(provider: str, line: str) -> str | None:
    """Normalize one SSE data line from the upstream into an OpenAI SSE line.

    Returns the full `data: ...` string to forward, or None to skip.
    """
    if not line.startswith("data:"):
        return None
    payload = line[len("data:"):].strip()
    if payload == "[DONE]":
        return line
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError:
        return line
    if provider in ("codex", "grok"):
        return line
    if provider == "claude":
        # Anthropic SSE sends `delta` events with `text`.
        if raw.get("type") == "content_block_delta":
            text = raw.get("delta", {}).get("text", "")
            chunk = {
                "id": raw.get("id"),
                "object": "chat.completion.chunk",
                "choices": [
                    {"index": 0, "delta": {"content": text}, "finish_reason": None}
                ],
            }
            return f"data: {json.dumps(chunk)}"
        if raw.get("type") == "message_stop":
            return "data: [DONE]"
        return None
    if provider == "gemini":
        parts = raw.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
        if text:
            chunk = {
                "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]
            }
            return f"data: {json.dumps(chunk)}"
        return None
    return line

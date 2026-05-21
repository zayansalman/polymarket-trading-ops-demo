"""LLM wrapper — central gateway for HF Inference calls.

Weather analysis and sanity checks route through this module. Benefits:

- One place that enforces the PRD's model routing rules (DeepSeek V3 default,
  Qwen 72B fallback).
- Automatic fallback on provider timeout — the user doesn't care which host
  served the call, only that it came back.
- JSON repair (`json-repair`) for malformed structured outputs.
- Call counter persisted to the `config` table so the dashboard can show
  LLM usage per module per day (PRD — Config tab).
- Structured logging with module + trade_id context for every call.

Uses `huggingface_hub.AsyncInferenceClient` under the hood, routed via
Inference Providers ("auto" = cheapest healthy provider per PRD).
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from huggingface_hub import AsyncInferenceClient
from json_repair import repair_json

from config import (
    HF_TOKEN,
    LLM_DEFAULT_MODEL,
    LLM_FALLBACK_MODEL,
    LLM_PROVIDER_ROUTE,
)
from db import connect
from logging_setup import get_logger

log = get_logger("llm")


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------

DEFAULT_TIMEOUT_S = 60.0
FALLBACK_TIMEOUT_S = 90.0
MAX_RETRIES = 2  # per model — fallback catches the rest


@dataclass
class LLMResult:
    """Result envelope. `raw` is always the model text; `json` is parsed+repaired if requested."""
    text: str
    model: str
    provider: str | None
    duration_s: float
    json: dict | list | None = None


# ----------------------------------------------------------------------
# Client cache — AsyncInferenceClient is cheap but reuse where possible
# ----------------------------------------------------------------------

_clients: dict[str, AsyncInferenceClient] = {}


def _client_for(model: str) -> AsyncInferenceClient:
    if model not in _clients:
        if not HF_TOKEN:
            raise RuntimeError("HF_TOKEN not set — cannot make LLM calls")
        _clients[model] = AsyncInferenceClient(
            model=model,
            token=HF_TOKEN,
            provider=LLM_PROVIDER_ROUTE,  # "auto" → cheapest healthy
            timeout=DEFAULT_TIMEOUT_S,
        )
    return _clients[model]


# ----------------------------------------------------------------------
# Call counter (persisted in config table so Config tab can render it)
# ----------------------------------------------------------------------


async def _bump_counter(module: str, model: str) -> None:
    """Increment today's call counter for (module, model). Best-effort."""
    today = datetime.now(timezone.utc).date().isoformat()
    key = f"llm_calls.{today}.{module}.{model.split('/')[-1]}"
    try:
        async with connect() as db:
            async with db.execute("SELECT value FROM config WHERE key = ?", (key,)) as cur:
                row = await cur.fetchone()
            current = int(row["value"]) if row else 0
            await db.execute(
                "INSERT INTO config(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(current + 1)),
            )
            await db.commit()
    except Exception as e:  # noqa: BLE001 — counter must never break a call
        log.warning("llm.counter_failed", error=str(e), key=key)


# ----------------------------------------------------------------------
# Public call surface
# ----------------------------------------------------------------------


async def chat(
    messages: list[dict],
    *,
    module: str,
    model: str | None = None,
    max_tokens: int = 2048,
    temperature: float = 0.3,
    json_mode: bool = False,
    trade_id: str | None = None,
) -> LLMResult:
    """Chat completion with automatic fallback + optional JSON repair.

    Args:
        messages: OpenAI-style [{"role": "system"|"user"|"assistant", "content": "..."}]
        module: calling module name for the call-counter + log context.
        model: override model. Defaults to DeepSeek V3.
        max_tokens: cap on output length.
        temperature: 0.3 is a deterministic-leaning default suitable for
            analyzer synthesis. Callers can bump it for creative tasks.
        json_mode: if True, attempt to parse+repair the output as JSON and
            surface it on `LLMResult.json`. Returns `None` if repair fails.
        trade_id: optional — binds into log context so LLM calls show up
            linked to the detected trade that triggered them.
    """
    primary = model or LLM_DEFAULT_MODEL
    fallback = LLM_FALLBACK_MODEL if primary != LLM_FALLBACK_MODEL else LLM_DEFAULT_MODEL

    tried: list[tuple[str, str]] = []
    last_exc: Exception | None = None

    for attempt_model, timeout in [(primary, DEFAULT_TIMEOUT_S), (fallback, FALLBACK_TIMEOUT_S)]:
        client = _client_for(attempt_model)
        t0 = datetime.now(timezone.utc)
        try:
            log.info(
                "llm.call.start",
                module=module,
                model=attempt_model,
                trade_id=trade_id,
                max_tokens=max_tokens,
                messages_chars=sum(len(m.get("content", "")) for m in messages),
            )
            # AsyncInferenceClient.chat_completion returns a ChatCompletionOutput;
            # .choices[0].message.content is the assistant text.
            resp = await asyncio.wait_for(
                client.chat_completion(
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                ),
                timeout=timeout,
            )
            text = (resp.choices[0].message.content or "").strip()
            provider = getattr(resp, "provider", None) or None
            elapsed = (datetime.now(timezone.utc) - t0).total_seconds()

            await _bump_counter(module, attempt_model)

            result = LLMResult(
                text=text,
                model=attempt_model,
                provider=provider,
                duration_s=round(elapsed, 2),
            )
            if json_mode:
                result.json = _parse_json_maybe(text)

            log.info(
                "llm.call.ok",
                module=module,
                model=attempt_model,
                provider=provider,
                duration_s=result.duration_s,
                output_chars=len(text),
                json_parsed=result.json is not None if json_mode else None,
                trade_id=trade_id,
            )
            return result
        except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001 — catch broadly and fall through
            tried.append((attempt_model, type(e).__name__))
            last_exc = e
            log.warning(
                "llm.call.failed",
                module=module,
                model=attempt_model,
                error=str(e)[:400],
                will_retry=attempt_model != fallback,
                trade_id=trade_id,
            )
            continue

    raise RuntimeError(
        f"LLM call failed for module={module}. Tried: {tried}. Last error: {last_exc}"
    ) from last_exc


def _parse_json_maybe(text: str) -> dict | list | None:
    """Strip markdown fences, then try to parse; repair on failure."""
    cleaned = text.strip()
    # Strip common ```json fences
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[: -3]
        cleaned = cleaned.strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        try:
            repaired = repair_json(cleaned, return_objects=True)
            if isinstance(repaired, (dict, list)):
                return repaired
        except Exception:  # noqa: BLE001
            pass
    return None


# ----------------------------------------------------------------------
# Convenience: one-shot completions
# ----------------------------------------------------------------------


async def complete(
    system: str,
    user: str,
    *,
    module: str,
    model: str | None = None,
    max_tokens: int = 2048,
    temperature: float = 0.3,
    json_mode: bool = False,
    trade_id: str | None = None,
) -> LLMResult:
    """System+user → completion. Thin convenience wrapper over chat()."""
    return await chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        module=module,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        json_mode=json_mode,
        trade_id=trade_id,
    )


# Re-export for callers that want to be explicit about which model they want
MODEL_DEFAULT = LLM_DEFAULT_MODEL
MODEL_FALLBACK = LLM_FALLBACK_MODEL


# ----------------------------------------------------------------------
# Call-counter read helper (used by Config tab later)
# ----------------------------------------------------------------------


async def counts_for(date_iso: str | None = None) -> dict[str, int]:
    """Return {module_model: n} for the given date (default: today)."""
    date_iso = date_iso or datetime.now(timezone.utc).date().isoformat()
    prefix = f"llm_calls.{date_iso}."
    out: dict[str, int] = {}
    async with connect() as db:
        async with db.execute(
            "SELECT key, value FROM config WHERE key LIKE ?",
            (prefix + "%",),
        ) as cur:
            rows = await cur.fetchall()
    for r in rows:
        out[r["key"][len(prefix):]] = int(r["value"])
    return out

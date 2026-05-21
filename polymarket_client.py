"""Polymarket API client — data-api + gamma-api.

Endpoints verified 2026-04-19:
- GET data-api.polymarket.com/trades?user={addr}&limit=1000&offset=N
- GET data-api.polymarket.com/positions?user={addr}
- GET gamma-api.polymarket.com/markets?condition_ids=X[&condition_ids=Y...]

Rate-limited to 60 req/min, max 10 concurrent.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

import httpx

from config import (
    POLYMARKET_DATA_API,
    POLYMARKET_GAMMA_API,
    POLYMARKET_MAX_CONCURRENT,
    POLYMARKET_RATE_LIMIT_PER_MIN,
    POLYMARKET_TIMEOUT_SECONDS,
)
from logging_setup import get_logger

log = get_logger("polymarket_client")


class RateLimiter:
    """Sliding-window rate limiter + concurrency cap."""

    def __init__(self, max_per_min: int, max_concurrent: int):
        self.max_per_min = max_per_min
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            # Drop timestamps older than 60s
            while self._timestamps and now - self._timestamps[0] > 60:
                self._timestamps.popleft()
            if len(self._timestamps) >= self.max_per_min:
                wait = 60 - (now - self._timestamps[0]) + 0.05
                log.info("rate_limit.wait", seconds=round(wait, 2))
                await asyncio.sleep(wait)
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] > 60:
                    self._timestamps.popleft()
            self._timestamps.append(now)


class PolymarketClient:
    """Async client with rate limiting, retry, and structured logging.

    Use as async context manager or call .aclose() when done.
    """

    def __init__(self) -> None:
        self._limiter = RateLimiter(
            POLYMARKET_RATE_LIMIT_PER_MIN, POLYMARKET_MAX_CONCURRENT
        )
        self._client = httpx.AsyncClient(
            timeout=POLYMARKET_TIMEOUT_SECONDS,
            headers={"User-Agent": "polymarket-weather-btc-local/0.2"},
            follow_redirects=True,
        )

    async def __aenter__(self) -> "PolymarketClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, params: dict | None = None, *, retries: int = 3) -> Any:
        await self._limiter.acquire()
        async with self._limiter.semaphore:
            last_exc: Exception | None = None
            for attempt in range(retries):
                try:
                    r = await self._client.get(url, params=params)
                    if r.status_code == 429:
                        wait = min(2 ** attempt, 30)
                        log.warning("polymarket.rate_limited", url=url, wait=wait)
                        await asyncio.sleep(wait)
                        continue
                    # 4xx (non-429) are permanent — don't retry. Polymarket returns
                    # 400 on /trades at offsets past its pagination ceiling; treat
                    # that as "no more data" for paginated endpoints.
                    if 400 <= r.status_code < 500:
                        raise httpx.HTTPStatusError(
                            f"{r.status_code} {r.reason_phrase}", request=r.request, response=r
                        )
                    r.raise_for_status()
                    return r.json()
                except httpx.HTTPStatusError as e:
                    sc = e.response.status_code
                    if 400 <= sc < 500 and sc != 429:
                        raise
                    last_exc = e
                    wait = min(2 ** attempt, 10)
                    log.warning(
                        "polymarket.request_failed",
                        url=url, attempt=attempt + 1, error=str(e), wait=wait,
                    )
                    await asyncio.sleep(wait)
                except (httpx.HTTPError, httpx.TimeoutException) as e:
                    last_exc = e
                    wait = min(2 ** attempt, 10)
                    log.warning(
                        "polymarket.request_failed",
                        url=url, attempt=attempt + 1, error=str(e), wait=wait,
                    )
                    await asyncio.sleep(wait)
            raise RuntimeError(f"Polymarket request failed after {retries} retries: {url}") from last_exc

    # ------------------------------------------------------------------
    # Per-wallet
    # ------------------------------------------------------------------

    async def get_trades(
        self,
        user_addr: str,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[dict]:
        """One trades page. limit max 1000 server-side."""
        data = await self._get(
            f"{POLYMARKET_DATA_API}/trades",
            params={"user": user_addr, "limit": limit, "offset": offset},
        )
        return data if isinstance(data, list) else []

    async def get_all_trades(
        self,
        user_addr: str,
        max_pages: int = 10,
        page_size: int = 1000,
    ) -> list[dict]:
        """Paginate trades. Returns newest-first by default from the API.

        Polymarket caps offset at roughly 5000 and returns 400 past that — we
        treat a 4xx mid-pagination as "end of results", not an error.
        """
        results: list[dict] = []
        for page_idx in range(max_pages):
            try:
                page = await self.get_trades(
                    user_addr, limit=page_size, offset=page_idx * page_size
                )
            except httpx.HTTPStatusError as e:
                if 400 <= e.response.status_code < 500:
                    log.info(
                        "trades.pagination_ceiling",
                        user=user_addr,
                        offset=page_idx * page_size,
                        status=e.response.status_code,
                    )
                    break
                raise
            if not page:
                break
            results.extend(page)
            if len(page) < page_size:
                break
        log.info(
            "trades.fetched",
            user=user_addr,
            count=len(results),
            capped=len(results) >= max_pages * page_size,
        )
        return results

    async def get_positions(self, user_addr: str) -> list[dict]:
        """Current open positions with `redeemable` flag (zombie indicator)."""
        data = await self._get(
            f"{POLYMARKET_DATA_API}/positions",
            params={"user": user_addr, "limit": 500},
        )
        return data if isinstance(data, list) else []

    # ------------------------------------------------------------------
    # Markets (Gamma)
    # ------------------------------------------------------------------

    async def get_markets_by_condition_ids(
        self, condition_ids: list[str]
    ) -> list[dict]:
        """Batch market lookup by conditionId. Gamma supports multi condition_ids param."""
        if not condition_ids:
            return []
        # Gamma uses repeated query param: ?condition_ids=X&condition_ids=Y
        # httpx handles this when params is a list of tuples.
        params = [("condition_ids", cid) for cid in condition_ids]
        data = await self._get(f"{POLYMARKET_GAMMA_API}/markets", params=params)
        return data if isinstance(data, list) else []

    async def get_market_by_condition_id(self, condition_id: str) -> dict | None:
        results = await self.get_markets_by_condition_ids([condition_id])
        return results[0] if results else None

    async def get_market_by_slug(self, slug: str) -> dict | None:
        """Gamma market lookup by `slug` (the last segment of a polymarket.com/event/... URL).

        Polymarket URLs look like:
          https://polymarket.com/event/highest-temperature-in-tokyo-on-april-16
        `slug` is the trailing segment. Gamma accepts `?slug=X` on /markets.
        """
        if not slug:
            return None
        data = await self._get(
            f"{POLYMARKET_GAMMA_API}/markets",
            params={"slug": slug},
        )
        if isinstance(data, list) and data:
            return data[0]
        return None

    async def get_event_by_slug(self, slug: str) -> dict | None:
        """Gamma /events lookup by slug — /event/ URLs often resolve to an event,
        not a single market. Events hold a `markets` array."""
        if not slug:
            return None
        data = await self._get(
            f"{POLYMARKET_GAMMA_API}/events",
            params={"slug": slug},
        )
        if isinstance(data, list) and data:
            return data[0]
        return None

    async def get_markets_query(
        self,
        *,
        closed: bool | None = None,
        active: bool | None = None,
        limit: int = 500,
        offset: int = 0,
        min_volume: float | None = None,
    ) -> list[dict]:
        """Gamma markets list with filters."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if closed is not None:
            params["closed"] = str(closed).lower()
        if active is not None:
            params["active"] = str(active).lower()
        if min_volume is not None:
            params["volume_num_min"] = min_volume
        data = await self._get(f"{POLYMARKET_GAMMA_API}/markets", params=params)
        return data if isinstance(data, list) else []


# ----------------------------------------------------------------------
# Helpers for market metadata
# ----------------------------------------------------------------------


def parse_polymarket_url(url: str) -> str | None:
    """Extract the slug from a polymarket.com URL.

    Accepts forms:
      https://polymarket.com/event/some-slug
      https://polymarket.com/event/some-slug?tid=...
      https://polymarket.com/market/some-slug
      polymarket.com/event/some-slug
      some-slug  (bare slug — returned as-is)
    """
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    # Strip scheme and query
    for prefix in ("https://", "http://"):
        if url.startswith(prefix):
            url = url[len(prefix):]
    if "?" in url:
        url = url.split("?", 1)[0]
    if "#" in url:
        url = url.split("#", 1)[0]
    url = url.rstrip("/")
    # If it looks like a bare slug (no slashes), return as-is
    if "/" not in url:
        return url
    parts = url.split("/")
    # polymarket.com / event|market / slug [ / ... ]
    for i, seg in enumerate(parts):
        if seg in ("event", "market", "markets") and i + 1 < len(parts):
            return parts[i + 1]
    # Fallback: last non-empty segment
    return parts[-1] if parts[-1] else None


def is_binary_market(market: dict) -> bool:
    """Binary Yes/No market. Scalar markets have >2 outcomes."""
    outcomes = market.get("outcomes")
    if isinstance(outcomes, str):
        import json

        try:
            outcomes = json.loads(outcomes)
        except Exception:
            return False
    return isinstance(outcomes, list) and len(outcomes) == 2


def market_is_resolved(market: dict) -> bool:
    """Closed and resolved."""
    return bool(market.get("closed"))


def resolution_outcome_value(market: dict, outcome_index: int) -> float | None:
    """After resolution, outcomePrices holds final values (1.0 / 0.0 for binary).

    Returns the final settled value for the given outcome index, or None if unresolved.
    """
    if not market.get("closed"):
        return None
    prices = market.get("outcomePrices")
    if isinstance(prices, str):
        import json

        try:
            prices = json.loads(prices)
        except Exception:
            return None
    if not isinstance(prices, list) or outcome_index >= len(prices):
        return None
    try:
        return float(prices[outcome_index])
    except (TypeError, ValueError):
        return None


def parse_timestamp(ts: int | str | None) -> datetime | None:
    """Polymarket timestamps are unix seconds (int). Also handles ISO strings."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(ts, str):
        # Try ISO first, then unix string
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            try:
                return datetime.fromtimestamp(int(ts), tz=timezone.utc)
            except (ValueError, OverflowError):
                return None
    return None

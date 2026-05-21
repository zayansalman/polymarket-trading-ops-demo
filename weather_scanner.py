"""Weather scanner — discovers live weather markets on polymarket.com/weather,
analyzes each, ranks mispricings, and returns actionable recommendations.

Why scrape vs. Gamma API: Polymarket's official weather tag ("record-temperatures",
id 426) has only 2 historical entries. The real live weather market catalog is
surfaced only at polymarket.com/weather (rendered from their UI), which lists
every active "highest-temperature-in-{city}-on-{date}" event. We pull the slugs
from that page and resolve each via Gamma /events.

Event shape:
  Each "Highest temperature in Tokyo on April 21?" event contains ~11 binary
  sub-markets forming a distribution over integer °C buckets:
    - "16°C or below" (≤16)
    - "17°C"          (==17)
    - "18°C"
    - ...
    - "26°C or higher"(≥26)
  YES prices across all sub-markets should sum to ~1.0.

What we compute for each bucket:
  - market_yes   — the current YES price
  - hist_prob    — empirical probability from ERA5 reanalysis, 30y ±3d window
  - ens_prob     — probability from multi-model ensemble members on target date
  - synth_prob   — horizon-weighted blend (≤3d → 70% ensemble, 3-10d → 50/50,
                   >10d or no ensemble → historical only)
  - gap          — synth_prob − market_yes (positive = market underpricing YES,
                   negative = overpricing YES; magnitude = edge)
  - confidence   — HIGH/MEDIUM/LOW based on horizon + ensemble spread + sample

Output: ranked list of (event, bucket, gap, synth, market, confidence, recommend).
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from calibrator import calibrate
from llm_sanity_check import sanity_check_rec
from logging_setup import get_logger
from polymarket_client import PolymarketClient
from recommendations import log_recommendation

log = get_logger("weather_scanner")


# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------

WEATHER_PAGE_URL = "https://polymarket.com/weather"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

HISTORICAL_YEARS = 30
DAY_WINDOW = 3
ENSEMBLE_MODELS = "icon_seamless,gfs_seamless,ecmwf_ifs025"

# Concurrency caps — Open-Meteo free limits: 10k/day, be polite
EVENT_CONCURRENCY = 4

# Event-slug pattern on /weather
EVENT_SLUG_RE = re.compile(
    r"highest-temperature-in-([a-z0-9-]+)-on-([a-z]+)-(\d{1,2})-(\d{4})"
)

# Sub-market question parsing
BUCKET_BELOW_RE = re.compile(r"be\s+(-?\d+)°?C?\s+or\s+below", re.I)
BUCKET_ABOVE_RE = re.compile(r"be\s+(-?\d+)°?C?\s+or\s+higher", re.I)
BUCKET_EXACT_RE = re.compile(r"be\s+(-?\d+)°?C\s+on", re.I)


# ----------------------------------------------------------------------
# Data shapes
# ----------------------------------------------------------------------


@dataclass
class Bucket:
    market_id: str                 # conditionId
    question: str
    kind: str                      # "below" | "exact" | "above"
    temp_c: int
    market_yes: float              # current YES price
    slug: str                      # sub-market slug (if any)


@dataclass
class EventAnalysis:
    slug: str
    title: str
    location: str
    location_resolved: str
    lat: float
    lon: float
    target_date: str
    days_out: int
    # Data
    hist_values: list[float] = field(default_factory=list)
    hist_years: int = 0
    ens_values: list[float] = field(default_factory=list)
    ens_mean: float | None = None
    ens_p10: float | None = None
    ens_p90: float | None = None
    buckets: list[dict] = field(default_factory=list)    # per-bucket computed rows
    # Intraday (populated only when days_out == 0)
    realized_max: float | None = None
    remaining_max_forecast: float | None = None
    hours_elapsed: int | None = None
    hours_total: int | None = None
    local_hour: int | None = None
    peak_passed: bool = False
    # Event-level summary
    total_yes_sum: float = 0.0
    event_url: str = ""
    error: str | None = None


@dataclass
class Recommendation:
    """One mispriced bucket."""
    event_slug: str
    event_title: str
    event_url: str
    location: str
    target_date: str
    days_out: int
    bucket_question: str
    bucket_temp: int
    bucket_kind: str
    bucket_market_id: str      # conditionId — for rec-vs-position matching
    bucket_slug: str           # sub-market slug
    market_yes: float
    synth_prob: float
    hist_prob: float
    ens_prob: float
    gap: float                     # synth - market
    abs_gap: float
    confidence: str                # HIGH | MEDIUM | LOW
    side: str                      # "YES" if gap > 0 else "NO"
    ens_spread: float | None = None
    # Day-of intraday context (None if not a 0d market)
    realized_max: float | None = None
    hours_elapsed: int | None = None
    hours_total: int | None = None
    local_hour: int | None = None
    peak_passed: bool = False


# ----------------------------------------------------------------------
# Step 1: discover event slugs from /weather
# ----------------------------------------------------------------------


async def discover_event_slugs(client: httpx.AsyncClient) -> list[str]:
    """Scrape polymarket.com/weather for temperature-event slugs.

    The page is statically-rendered (SSR Next.js) — slugs appear in href
    attributes. We dedupe + preserve first-seen order.
    """
    r = await client.get(
        WEATHER_PAGE_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; polymarket-scanner)"},
    )
    r.raise_for_status()
    text = r.text
    slugs: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"/event/([a-z0-9-]+)", text):
        slug = m.group(1)
        if slug in seen:
            continue
        if not EVENT_SLUG_RE.match(slug):
            # Only care about "highest-temperature-in-..." pattern — skip
            # unrelated slugs that Polymarket's footer/feed might surface.
            continue
        seen.add(slug)
        slugs.append(slug)
    log.info("scanner.discovered", count=len(slugs))
    return slugs


# ----------------------------------------------------------------------
# Step 2: parse event + sub-markets
# ----------------------------------------------------------------------


def _parse_bucket(question: str) -> tuple[str, int] | None:
    """Return (kind, temp_c) where kind ∈ {'below','exact','above'}."""
    m = BUCKET_BELOW_RE.search(question)
    if m:
        return "below", int(m.group(1))
    m = BUCKET_ABOVE_RE.search(question)
    if m:
        return "above", int(m.group(1))
    m = BUCKET_EXACT_RE.search(question)
    if m:
        return "exact", int(m.group(1))
    return None


def _parse_yes_price(market: dict) -> float | None:
    """YES is outcomes[0]. outcomePrices may be a JSON-string list."""
    import json as _json

    prices = market.get("outcomePrices")
    if isinstance(prices, str):
        try:
            prices = _json.loads(prices)
        except Exception:  # noqa: BLE001
            return None
    if not isinstance(prices, list) or not prices:
        return None
    try:
        return float(prices[0])
    except (TypeError, ValueError):
        return None


def _extract_buckets(event: dict) -> list[Bucket]:
    buckets: list[Bucket] = []
    for m in event.get("markets") or []:
        if m.get("closed") or not m.get("active", True):
            continue
        q = m.get("question") or ""
        parsed = _parse_bucket(q)
        if not parsed:
            continue
        kind, temp = parsed
        yes = _parse_yes_price(m)
        if yes is None:
            continue
        buckets.append(Bucket(
            market_id=m.get("conditionId") or m.get("id") or "",
            question=q,
            kind=kind,
            temp_c=temp,
            market_yes=yes,
            slug=m.get("slug") or "",
        ))
    # Sort by temp ascending
    buckets.sort(key=lambda b: (b.temp_c, 0 if b.kind == "exact" else (1 if b.kind == "above" else -1)))
    return buckets


# ----------------------------------------------------------------------
# Step 3: Open-Meteo calls (shared with analyzer_weather for efficiency)
# ----------------------------------------------------------------------


async def _geocode(client: httpx.AsyncClient, location: str) -> tuple[float, float, str]:
    r = await client.get(GEOCODE_URL, params={"name": location, "count": 1, "language": "en"})
    r.raise_for_status()
    data = r.json()
    results = data.get("results") or []
    if not results:
        raise ValueError(f"Could not geocode: {location}")
    top = results[0]
    resolved = ", ".join(filter(None, [top.get("name"), top.get("admin1"), top.get("country")]))
    return float(top["latitude"]), float(top["longitude"]), resolved


async def _fetch_historical_max_temp(
    client: httpx.AsyncClient, lat: float, lon: float, target: date,
) -> tuple[list[float], int]:
    current_year = target.year
    end_year = current_year - 1
    start_year = end_year - HISTORICAL_YEARS + 1
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": f"{start_year}-01-01",
        "end_date": f"{end_year}-12-31",
        "daily": "temperature_2m_max",
        "timezone": "auto",
    }
    r = await client.get(ARCHIVE_URL, params=params)
    r.raise_for_status()
    data = r.json()
    daily = data.get("daily") or {}
    dates = daily.get("time") or []
    values = daily.get("temperature_2m_max") or []
    target_md = (target.month, target.day)
    windowed: list[float] = []
    years_covered: set[int] = set()
    for d_str, v in zip(dates, values):
        if v is None:
            continue
        try:
            d = date.fromisoformat(d_str)
        except ValueError:
            continue
        delta = abs((d - date(d.year, target_md[0], target_md[1])).days)
        if delta <= DAY_WINDOW:
            windowed.append(float(v))
            years_covered.add(d.year)
    return windowed, len(years_covered)


async def _fetch_realized_today(
    client: httpx.AsyncClient, lat: float, lon: float, target: date,
) -> dict | None:
    """For day-of markets, fetch hourly temps and split into realized vs. remaining.

    Timezone: `timezone=auto` on Open-Meteo resolves the local timezone for
    the lat/lon — so local hour = index into the hourly array (00..23).

    Returns None if target is not today. Otherwise:
      {
        "realized_max": float | None,     # max of past hours in local time
        "remaining_hourly": list[float],  # forecast temps for remaining hours
        "hours_elapsed": int,             # count of past hours (== local hour)
        "hours_total": int,
        "tz_offset_seconds": int,
        "local_hour": int,                # 0..23 in the location's local time
      }
    """
    today_utc = datetime.now(timezone.utc).date()
    if target != today_utc:
        return None
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m",
        "timezone": "auto",
        "start_date": target.isoformat(),
        "end_date": target.isoformat(),
    }
    r = await client.get(FORECAST_URL, params=params)
    r.raise_for_status()
    data = r.json()
    tz_offset = int(data.get("utc_offset_seconds") or 0)
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    if not times or not temps:
        return None
    now_local_naive = (datetime.now(timezone.utc) + timedelta(seconds=tz_offset)).replace(tzinfo=None)
    realized: list[float] = []
    remaining: list[float] = []
    for t_str, v in zip(times, temps):
        if v is None:
            continue
        try:
            t = datetime.fromisoformat(t_str)
        except ValueError:
            continue
        if t <= now_local_naive:
            realized.append(float(v))
        else:
            remaining.append(float(v))
    if not realized and not remaining:
        return None
    return {
        "realized_max": max(realized) if realized else None,
        "remaining_hourly": remaining,
        "hours_elapsed": len(realized),
        "hours_total": len(realized) + len(remaining),
        "tz_offset_seconds": tz_offset,
        "local_hour": now_local_naive.hour,
    }


def _day_of_max_samples(
    realized_max: float | None,
    remaining_hourly: list[float],
    local_hour: int,
    n: int = 400,
    seed: int = 0xC0FFEE,
) -> tuple[list[float], bool]:
    """Monte Carlo sample of today's final daily max, using local-hour awareness.

    For each remaining hour, perturb the deterministic forecast with Gaussian
    noise. σ collapses to ~0.4°C once we're past the local diurnal peak
    (default 15:00 — 3pm local), reflecting that after the peak temps usually
    only fall, so residual uncertainty is tiny.

    Returns (samples, peak_passed).
    """
    import random

    # Diurnal peak: assume 15:00 local. By 3pm in most temperate latitudes
    # the day's max is typically reached — from here temps only fall or
    # plateau briefly, so we treat 15:00 as peak-reached (not "still climbing").
    PEAK_HOUR_LOCAL = 15
    peak_passed = local_hour >= PEAK_HOUR_LOCAL  # 15:00 local or later

    # Per-hour forecast noise. At or past peak, temps drop monotonically so
    # the max collapses toward realized_max — use tight σ. Pre-peak, give it
    # more spread to reflect actual short-range hourly uncertainty.
    if peak_passed:
        sigma = 0.4
    elif local_hour >= PEAK_HOUR_LOCAL - 2:  # within 2h of peak (13-14 local)
        sigma = 0.7
    else:
        sigma = 1.1

    rng = random.Random(seed)
    samples: list[float] = []

    if not remaining_hourly and realized_max is not None:
        # Day is over — the max is set, just return n copies with a hair of noise.
        return [realized_max for _ in range(n)], True

    if realized_max is None:
        realized_max = -1000.0  # won't bind

    for _ in range(n):
        max_remaining = max(
            (v + rng.gauss(0, sigma) for v in remaining_hourly),
            default=-1000.0,
        )
        samples.append(max(realized_max, max_remaining))
    return samples, peak_passed


async def _fetch_ensemble_max_temp(
    client: httpx.AsyncClient, lat: float, lon: float, target: date,
) -> tuple[list[float], int]:
    today = datetime.now(timezone.utc).date()
    days_out = (target - today).days
    if days_out < 0 or days_out > 15:
        return [], days_out
    params = {
        "latitude": lat,
        "longitude": lon,
        "models": ENSEMBLE_MODELS,
        "daily": "temperature_2m_max",
        "timezone": "auto",
        "start_date": target.isoformat(),
        "end_date": target.isoformat(),
    }
    r = await client.get(ENSEMBLE_URL, params=params)
    r.raise_for_status()
    data = r.json()
    daily = data.get("daily") or {}
    members: list[float] = []
    for key, series in daily.items():
        if key == "time" or not isinstance(series, list):
            continue
        for v in series:
            if v is not None:
                members.append(float(v))
    return members, days_out


# ----------------------------------------------------------------------
# Step 4: compute per-bucket probabilities + confidence
# ----------------------------------------------------------------------


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def _bucket_match(value: float, kind: str, temp: int) -> bool:
    """Round to nearest integer °C — Polymarket's convention for "be 24°C"."""
    rounded = round(value)
    if kind == "exact":
        return rounded == temp
    if kind == "below":
        return rounded <= temp
    if kind == "above":
        return rounded >= temp
    return False


def _prob_of_bucket(values: list[float], kind: str, temp: int) -> float:
    if not values:
        return 0.0
    hits = sum(1 for v in values if _bucket_match(v, kind, temp))
    return hits / len(values)


def _synth_prob(hist: float, ens: float, days_out: int, have_ens: bool) -> float:
    """Horizon-weighted blend.

    No ensemble data → pure historical.
    Day-of (0d)  → 100% ensemble (intraday-floored — realized max is known).
    1-3 days out → 75% ensemble, 25% historical (forecasts dominate short-range).
    4-10 days    → 50/50.
    11-15 days   → 35% ensemble, 65% historical.
    """
    if not have_ens:
        return hist
    if days_out <= 0:
        return ens
    if days_out <= 3:
        w = 0.75
    elif days_out <= 10:
        w = 0.50
    else:
        w = 0.35
    return w * ens + (1 - w) * hist


def _confidence(
    days_out: int,
    have_ens: bool,
    spread: float | None,
    n_hist: int,
    intraday_known: bool = False,
) -> str:
    if not have_ens and not intraday_known:
        return "LOW"
    # Day-of with known realized max dominates uncertainty — anchor HIGH.
    if days_out <= 0 and intraday_known:
        return "HIGH"
    if n_hist < 20:
        return "LOW"
    if days_out <= 5 and spread is not None and spread < 4.0:
        return "HIGH"
    if days_out <= 10 and spread is not None and spread < 6.0:
        return "MEDIUM"
    return "LOW"


# ----------------------------------------------------------------------
# Step 5: per-event analysis
# ----------------------------------------------------------------------


def _parse_event_meta(slug: str, title: str) -> tuple[str, str]:
    """Return (location_human, iso_date) from slug or title.

    Slug form: highest-temperature-in-{city-slug}-on-{month}-{day}-{year}
    Title form: 'Highest temperature in Tokyo on April 21?'
    """
    m = EVENT_SLUG_RE.match(slug)
    if m:
        city_slug = m.group(1).replace("-", " ").strip()
        month_name = m.group(2)
        day = int(m.group(3))
        year = int(m.group(4))
        try:
            dt = datetime.strptime(f"{month_name} {day} {year}", "%B %d %Y")
            iso = dt.date().isoformat()
        except ValueError:
            iso = f"{year}-01-01"
        # Prefer the title's formatting if available (Tokyo vs tokyo)
        title_loc = None
        title_m = re.search(r"Highest temperature in (.+?) on ", title, re.I)
        if title_m:
            title_loc = title_m.group(1).strip()
        return (title_loc or city_slug.title()), iso
    return title, datetime.now(timezone.utc).date().isoformat()


async def analyze_event(
    client: httpx.AsyncClient,
    pm: PolymarketClient,
    slug: str,
) -> EventAnalysis:
    event = await pm.get_event_by_slug(slug)
    if not event:
        return EventAnalysis(
            slug=slug, title="", location="", location_resolved="",
            lat=0.0, lon=0.0, target_date="", days_out=0,
            event_url=f"https://polymarket.com/event/{slug}",
            error="event not found",
        )
    title = event.get("title") or ""
    location, target_iso = _parse_event_meta(slug, title)
    try:
        target = date.fromisoformat(target_iso)
    except ValueError:
        return EventAnalysis(
            slug=slug, title=title, location=location, location_resolved="",
            lat=0.0, lon=0.0, target_date=target_iso, days_out=0,
            event_url=f"https://polymarket.com/event/{slug}",
            error=f"bad target_date: {target_iso}",
        )

    try:
        lat, lon, resolved = await _geocode(client, location)
    except Exception as e:  # noqa: BLE001
        return EventAnalysis(
            slug=slug, title=title, location=location, location_resolved="",
            lat=0.0, lon=0.0, target_date=target_iso, days_out=0,
            event_url=f"https://polymarket.com/event/{slug}",
            error=f"geocode failed: {e}",
        )

    hist_task = _fetch_historical_max_temp(client, lat, lon, target)
    ens_task = _fetch_ensemble_max_temp(client, lat, lon, target)
    intraday_task = _fetch_realized_today(client, lat, lon, target)
    (hist_values, hist_years), (ens_values, days_out), intraday = await asyncio.gather(
        hist_task, ens_task, intraday_task
    )

    # Day-of markets: replace day-ahead ensemble with local-time-aware Monte
    # Carlo samples of the final daily max. We *know* realized hours, and the
    # remaining hourly forecast has much less uncertainty than a ensemble
    # generated 24h ago. Post-peak local time, variance collapses further.
    realized_max = None
    remaining_max_forecast = None
    hours_elapsed = None
    hours_total = None
    local_hour = None
    peak_passed = False
    if intraday:
        realized_max = intraday["realized_max"]
        remaining_hourly = intraday["remaining_hourly"]
        remaining_max_forecast = max(remaining_hourly) if remaining_hourly else None
        hours_elapsed = intraday["hours_elapsed"]
        hours_total = intraday["hours_total"]
        local_hour = intraday["local_hour"]
        ens_values, peak_passed = _day_of_max_samples(
            realized_max, remaining_hourly, local_hour,
        )

    buckets = _extract_buckets(event)
    bucket_rows: list[dict] = []
    total_yes = 0.0
    for b in buckets:
        total_yes += b.market_yes
        h_prob = _prob_of_bucket(hist_values, b.kind, b.temp_c)
        e_prob = _prob_of_bucket(ens_values, b.kind, b.temp_c) if ens_values else 0.0
        s_prob = _synth_prob(h_prob, e_prob, days_out, bool(ens_values))
        bucket_rows.append({
            "bucket": asdict(b),
            "hist_prob": h_prob,
            "ens_prob": e_prob,
            "synth_prob": s_prob,
            "gap": s_prob - b.market_yes,
        })

    ens_mean = sum(ens_values) / len(ens_values) if ens_values else None
    ens_p10 = _percentile(ens_values, 0.1)
    ens_p90 = _percentile(ens_values, 0.9)

    return EventAnalysis(
        slug=slug,
        title=title,
        location=location,
        location_resolved=resolved,
        lat=lat, lon=lon,
        target_date=target_iso,
        days_out=days_out,
        hist_values=hist_values,
        hist_years=hist_years,
        ens_values=ens_values,
        ens_mean=ens_mean,
        ens_p10=ens_p10,
        ens_p90=ens_p90,
        buckets=bucket_rows,
        realized_max=realized_max,
        remaining_max_forecast=remaining_max_forecast,
        hours_elapsed=hours_elapsed,
        hours_total=hours_total,
        local_hour=local_hour,
        peak_passed=peak_passed,
        total_yes_sum=round(total_yes, 4),
        event_url=f"https://polymarket.com/event/{slug}",
    )


# ----------------------------------------------------------------------
# Step 6: scan entry point + ranking
# ----------------------------------------------------------------------


async def scan_weather_section(
    *,
    max_events: int | None = None,
    min_abs_gap: float = 0.05,
) -> dict:
    """Discover + analyze all weather events, rank mispricings.

    Returns:
      {
        "events": [EventAnalysis-dict, ...],
        "recommendations": [Recommendation-dict, ...],   # sorted by abs_gap desc
        "summary": {"events_scanned": N, "events_with_errors": M,
                    "recommendations_count": K, "scanned_at": ISO}
      }
    """
    t0 = datetime.now(timezone.utc)
    async with httpx.AsyncClient(
        timeout=45.0,
        headers={"User-Agent": "Mozilla/5.0 (compatible; polymarket-scanner)"},
    ) as client, PolymarketClient() as pm:
        slugs = await discover_event_slugs(client)
        if max_events is not None:
            slugs = slugs[:max_events]

        sem = asyncio.Semaphore(EVENT_CONCURRENCY)

        async def _bounded(slug: str) -> EventAnalysis:
            async with sem:
                try:
                    return await analyze_event(client, pm, slug)
                except Exception as e:  # noqa: BLE001
                    log.warning("scanner.event_failed", slug=slug, error=str(e))
                    return EventAnalysis(
                        slug=slug, title="", location="", location_resolved="",
                        lat=0.0, lon=0.0, target_date="", days_out=0,
                        event_url=f"https://polymarket.com/event/{slug}",
                        error=str(e),
                    )

        analyses = await asyncio.gather(*(_bounded(s) for s in slugs))

    # Build recommendations
    recs: list[Recommendation] = []
    for ea in analyses:
        if ea.error or not ea.buckets:
            continue
        spread = (
            ea.ens_p90 - ea.ens_p10
            if ea.ens_p90 is not None and ea.ens_p10 is not None else None
        )
        conf = _confidence(
            ea.days_out,
            bool(ea.ens_values),
            spread,
            len(ea.hist_values),
            intraday_known=ea.realized_max is not None,
        )
        for row in ea.buckets:
            b = row["bucket"]
            gap = row["gap"]
            if abs(gap) < min_abs_gap:
                continue
            # Suppress 0.00 / 1.00 market edges where bookie is already certain —
            # synth is almost always noisy against those.
            if b["market_yes"] < 0.005 or b["market_yes"] > 0.995:
                continue
            recs.append(Recommendation(
                event_slug=ea.slug,
                event_title=ea.title,
                event_url=ea.event_url,
                location=ea.location_resolved or ea.location,
                target_date=ea.target_date,
                days_out=ea.days_out,
                bucket_question=b["question"],
                bucket_temp=b["temp_c"],
                bucket_kind=b["kind"],
                bucket_market_id=b.get("market_id", ""),
                bucket_slug=b.get("slug", ""),
                market_yes=b["market_yes"],
                synth_prob=row["synth_prob"],
                hist_prob=row["hist_prob"],
                ens_prob=row["ens_prob"],
                gap=gap,
                abs_gap=abs(gap),
                confidence=conf,
                side="YES" if gap > 0 else "NO",
                ens_spread=spread,
                realized_max=ea.realized_max,
                hours_elapsed=ea.hours_elapsed,
                hours_total=ea.hours_total,
                local_hour=ea.local_hour,
                peak_passed=ea.peak_passed,
            ))

    recs.sort(key=lambda r: (r.confidence != "HIGH", r.confidence != "MEDIUM", -r.abs_gap))

    # Persist every HIGH/MEDIUM rec so the My Positions tab can later compare
    # "what Claude recommended" vs. "what the user actually did". LOW-confidence
    # rows stay on screen but aren't treated as actionable recommendations.
    await _log_weather_recs(recs, analyses)

    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    summary = {
        "events_scanned": len(analyses),
        "events_with_errors": sum(1 for e in analyses if e.error),
        "recommendations_count": len(recs),
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "elapsed_s": round(elapsed, 1),
    }
    log.info("scanner.done", **summary)

    return {
        "events": [_ea_to_dict(e) for e in analyses],
        "recommendations": [asdict(r) for r in recs],
        "summary": summary,
    }


def _ea_to_dict(e: EventAnalysis) -> dict[str, Any]:
    d = asdict(e)
    # Drop large arrays — UI doesn't need them
    d.pop("hist_values", None)
    d.pop("ens_values", None)
    return d


async def _log_weather_recs(
    recs: list[Recommendation], analyses: list[EventAnalysis]
) -> None:
    """Persist HIGH/MEDIUM recs to the recommendations table for rec-vs-actual.

    For each actionable rec we also:
      - apply the learned calibrator (post-processed model probability)
      - call the DeepSeek-V3 sanity reviewer on HIGH-confidence recs with
        ≥10pp edge (the set most likely to turn into actual bets)
    """
    # Index analyses by slug for intraday context on sanity-check
    ea_by_slug = {ea.slug: ea for ea in analyses}
    logged = 0
    for r in recs:
        if r.confidence == "LOW":
            continue
        # Outcome index follows Polymarket convention: 0 = Yes, 1 = No
        outcome = "Yes" if r.side == "YES" else "No"
        outcome_index = 0 if r.side == "YES" else 1
        # Price context: if BET YES, user enters at market_yes; if BET NO,
        # user enters at (1 - market_yes) on the NO token.
        entry_price = r.market_yes if r.side == "YES" else (1.0 - r.market_yes)

        # Side-specific probabilities — calibrate the prob we're actually
        # betting on (i.e. 1-synth for NO side).
        prob_side = r.synth_prob if r.side == "YES" else 1.0 - r.synth_prob
        try:
            calibrated = await calibrate(prob_side)
        except Exception as e:  # noqa: BLE001
            log.warning("weather.calibrate_failed", slug=r.bucket_slug, error=str(e))
            calibrated = prob_side

        # LLM sanity-check the tightest recs — HIGH confidence + ≥10pp edge.
        # Fail-open: error is logged but doesn't block the rec.
        llm_verdict: str | None = None
        llm_reasoning: str | None = None
        if r.confidence == "HIGH" and abs(r.gap) >= 0.10:
            ea = ea_by_slug.get(r.event_slug)
            try:
                sc = await sanity_check_rec(
                    question=r.event_title or r.bucket_question,
                    location=r.location,
                    days_out=r.days_out,
                    side=r.side,
                    market_yes=r.market_yes,
                    synth_prob=r.synth_prob,
                    hist_prob=r.hist_prob,
                    ens_prob=r.ens_prob,
                    ens_spread=r.ens_spread,
                    realized_max=r.realized_max,
                    peak_passed=r.peak_passed,
                )
                llm_verdict = sc.verdict
                llm_reasoning = sc.reasoning
            except Exception as e:  # noqa: BLE001
                log.warning("weather.sanity_check_failed", slug=r.bucket_slug, error=str(e))

        notes = (
            f"market {r.market_yes*100:.1f}%, synth {r.synth_prob*100:.1f}% "
            f"(calib {calibrated*100:.1f}%) → {r.gap*100:+.1f}pp gap · {r.bucket_question}"
        )
        try:
            inserted = await log_recommendation(
                source="weather",
                outcome=outcome,
                outcome_index=outcome_index,
                rec_price=entry_price,
                market_slug=r.bucket_slug or None,
                event_slug=r.event_slug,
                market_question=r.bucket_question,
                condition_id=r.bucket_market_id or None,
                edge_pp=r.gap * 100,
                confidence=r.confidence,
                notes=notes,
                source_ref=None,
                synth_prob=r.synth_prob,
                calibrated_prob=calibrated,
                location=r.location,
                days_out=r.days_out,
                llm_verdict=llm_verdict,
                llm_reasoning=llm_reasoning,
            )
            if inserted:
                logged += 1
        except Exception as e:  # noqa: BLE001 — logging should never crash the scan
            log.warning("weather.rec_log_failed", slug=r.bucket_slug, error=str(e))
    if logged:
        log.info("weather.recs_logged", count=logged, total=len(recs))

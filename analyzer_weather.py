"""Weather-market analyzer (PRD step 7).

Takes a binary Polymarket weather market and produces a probability-gap report.
Purely informational — doesn't recommend a position.

Pipeline:
  1. LLM extraction — parse the market question into a structured spec:
       {location, target_date, metric, threshold, threshold_op}
  2. Geocode the location via Open-Meteo free geocoder.
  3. Historical base rate — Open-Meteo ERA5 archive (free, no key), 30 years
     of the same calendar day ±3 days. Compute empirical probability of the
     threshold being met.
  4. Current forecast — Open-Meteo ensemble forecast (multi-model mean + spread).
     Compute probability of threshold from ensemble member outcomes.
  5. Anomaly flags — is the forecast sitting in the tail of the historical
     distribution? Is recent-year trend different from long-run?
  6. LLM synthesis — DeepSeek V3 takes all signals + produces the PRD-shaped
     markdown output the Analyze tab renders.
  7. Persist to `analysis_requests` with mode='weather'.

No keys needed. Open-Meteo is free forever for non-commercial use. ERA5 archive
covers 1940-present. Ensemble forecasts extend ~16 days out.
"""
from __future__ import annotations

import asyncio
import json
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from llm import MODEL_DEFAULT, complete
from logging_setup import get_logger

log = get_logger("analyzer_weather")


# ----------------------------------------------------------------------
# Open-Meteo endpoints (all free, no key)
# ----------------------------------------------------------------------

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# Historical lookback
HISTORICAL_YEARS = 30
DAY_WINDOW = 3  # ± days around the target calendar day to densify the sample

# Which Open-Meteo daily variable corresponds to each metric we support
METRIC_TO_VAR = {
    "temperature_max": "temperature_2m_max",
    "temperature_min": "temperature_2m_min",
    "temperature_mean": "temperature_2m_mean",
    "precipitation": "precipitation_sum",
    "snowfall": "snowfall_sum",
    "wind_max": "wind_speed_10m_max",
}

# Ensemble models we average across
ENSEMBLE_MODELS = "icon_seamless,gfs_seamless,ecmwf_ifs025"


# ----------------------------------------------------------------------
# Data shapes
# ----------------------------------------------------------------------


@dataclass
class WeatherSpec:
    """Structured form of a weather market question."""
    location: str
    target_date: str              # ISO YYYY-MM-DD
    metric: str                   # one of METRIC_TO_VAR keys
    threshold: float              # value the market compares against
    threshold_op: str             # ">=", ">", "<=", "<", "=="
    units: str                    # "C", "F", "mm", "inches", "kph", "mph"
    raw_question: str


@dataclass
class WeatherBundle:
    """Everything the synthesizer gets to see."""
    spec: WeatherSpec
    lat: float
    lon: float
    location_resolved: str
    # historical
    historical_values: list[float] = field(default_factory=list)
    historical_years: int = 0
    historical_prob: float = 0.0     # raw empirical
    historical_prob_adj: float = 0.0 # trend-adjusted (simple linear warming)
    historical_trend_c_per_decade: float | None = None
    # forecast
    ensemble_values: list[float] = field(default_factory=list)
    ensemble_mean: float | None = None
    ensemble_p10: float | None = None
    ensemble_p90: float | None = None
    ensemble_prob: float = 0.0
    ensemble_days_out: int | None = None
    # market
    market_price: float | None = None


@dataclass
class WeatherReport:
    markdown: str                 # the PRD-shaped markdown
    synthesized_prob: float       # model's final probability estimate
    market_prob: float | None     # from market_price if provided
    gap_points: float | None
    confidence: str               # LOW | MEDIUM | HIGH
    bundle: WeatherBundle
    llm_calls: int
    duration_s: float


# ----------------------------------------------------------------------
# Step 1: LLM extraction
# ----------------------------------------------------------------------


EXTRACTOR_SYSTEM = """You extract structured weather-market specs from Polymarket questions.

Return ONLY a JSON object with these exact keys:
  location (string — city/region, e.g. "Tokyo, Japan")
  target_date (ISO YYYY-MM-DD)
  metric (one of: temperature_max, temperature_min, temperature_mean, precipitation, snowfall, wind_max)
  threshold (number in the units field)
  threshold_op (one of: ">=", ">", "<=", "<", "==")
  units (one of: C, F, mm, inches, kph, mph)

Examples of mapping:
- "Highest temperature in Tokyo on April 16, 2026" with resolution "≥24°C" → temperature_max, >=, 24, C
- "Will NYC get rain on May 3, 2026?" (>0mm) → precipitation, >, 0, mm
- "Coldest low in Chicago Jan 15" with "below -10°C" → temperature_min, <, -10, C

If the question asks only "the highest temperature" without a threshold, infer the threshold from the resolution rules if provided; else return threshold_op="==" and threshold=0 and flag by setting units to empty string (caller handles).
"""


async def _extract_spec(market_question: str, resolution_rules: str | None) -> WeatherSpec:
    user_msg = f"MARKET QUESTION:\n{market_question}"
    if resolution_rules:
        user_msg += f"\n\nRESOLUTION RULES:\n{resolution_rules[:2000]}"
    res = await complete(
        EXTRACTOR_SYSTEM,
        user_msg,
        module="analyzer_weather.extract",
        max_tokens=400,
        temperature=0.0,
        json_mode=True,
    )
    data = res.json or {}
    if not isinstance(data, dict):
        raise ValueError(f"Extractor returned non-dict: {res.text[:200]}")
    try:
        spec = WeatherSpec(
            location=str(data["location"]),
            target_date=str(data["target_date"]),
            metric=str(data["metric"]),
            threshold=float(data["threshold"]),
            threshold_op=str(data["threshold_op"]),
            units=str(data.get("units", "")),
            raw_question=market_question,
        )
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"Extractor output missing keys: {data} ({e})") from e
    if spec.metric not in METRIC_TO_VAR:
        raise ValueError(f"Unsupported metric: {spec.metric}")
    return spec


# ----------------------------------------------------------------------
# Step 2: Geocoding
# ----------------------------------------------------------------------


async def _geocode(client: httpx.AsyncClient, location: str) -> tuple[float, float, str]:
    r = await client.get(GEOCODE_URL, params={"name": location, "count": 1, "language": "en"})
    r.raise_for_status()
    data = r.json()
    results = data.get("results") or []
    if not results:
        raise ValueError(f"Could not geocode: {location}")
    top = results[0]
    resolved = ", ".join(
        filter(None, [top.get("name"), top.get("admin1"), top.get("country")])
    )
    return float(top["latitude"]), float(top["longitude"]), resolved


# ----------------------------------------------------------------------
# Step 3: Historical (ERA5 archive)
# ----------------------------------------------------------------------


async def _fetch_historical(
    client: httpx.AsyncClient,
    lat: float,
    lon: float,
    target: date,
    var: str,
) -> tuple[list[float], list[tuple[int, float]]]:
    """Fetch a pile of ±DAY_WINDOW-day windows per year for HISTORICAL_YEARS.

    Returns (values, (year, annual_value) pairs — annual_value is the per-year
    metric value from the target calendar day only, used for trend estimation).
    """
    # ERA5 publishes with ~5-day lag. Stop at year-1 to be safe.
    current_year = target.year
    end_year = current_year - 1
    start_year = end_year - HISTORICAL_YEARS + 1

    # Build an archive range that covers every target ±DAY_WINDOW day across years.
    # Open-Meteo accepts multi-year ranges. We'll pull start_year-01-01 to
    # end_year-12-31 once (cheap) and filter locally.
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": f"{start_year}-01-01",
        "end_date": f"{end_year}-12-31",
        "daily": var,
        "timezone": "auto",
    }
    r = await client.get(ARCHIVE_URL, params=params)
    r.raise_for_status()
    data = r.json()
    daily = data.get("daily") or {}
    dates = daily.get("time") or []
    values = daily.get(var) or []
    if not dates or not values:
        return [], []

    target_md = (target.month, target.day)
    windowed: list[float] = []
    by_year: dict[int, list[float]] = {}
    for d_str, v in zip(dates, values):
        if v is None:
            continue
        try:
            d = date.fromisoformat(d_str)
        except ValueError:
            continue
        # ±DAY_WINDOW days around the target calendar day
        delta = abs((date(d.year, d.month, d.day) - date(d.year, target_md[0], target_md[1])).days)
        if delta <= DAY_WINDOW:
            windowed.append(float(v))
            # For trend, only take the exact target day
            if (d.month, d.day) == target_md:
                by_year.setdefault(d.year, []).append(float(v))

    annual = [(yr, sum(vals) / len(vals)) for yr, vals in sorted(by_year.items())]
    return windowed, annual


def _apply_op(value: float, op: str, threshold: float) -> bool:
    if op == ">=":
        return value >= threshold
    if op == ">":
        return value > threshold
    if op == "<=":
        return value <= threshold
    if op == "<":
        return value < threshold
    if op == "==":
        return abs(value - threshold) < 1e-6
    return False


def _linear_trend(points: list[tuple[int, float]]) -> float | None:
    """Plain OLS slope (value per year). Returns None if <5 points."""
    if len(points) < 5:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return num / den


# ----------------------------------------------------------------------
# Step 4: Ensemble forecast
# ----------------------------------------------------------------------


async def _fetch_ensemble(
    client: httpx.AsyncClient,
    lat: float,
    lon: float,
    target: date,
    var: str,
) -> tuple[list[float], int | None]:
    """Return (per-member target-day values, days_out).

    Open-Meteo ensemble returns hourly by default. We request the closest daily
    aggregate on the `temperature_2m_max`-style var. For precipitation/wind
    we request the hourly and aggregate locally.
    """
    today = datetime.now(timezone.utc).date()
    days_out = (target - today).days
    if days_out < 0:
        return [], days_out
    if days_out > 15:
        # beyond ensemble horizon — signal with empty values
        return [], days_out

    # Ensemble API exposes these as daily when requested
    supports_daily = var in {
        "temperature_2m_max",
        "temperature_2m_min",
        "temperature_2m_mean",
        "precipitation_sum",
        "snowfall_sum",
        "wind_speed_10m_max",
    }
    params: dict[str, Any] = {
        "latitude": lat,
        "longitude": lon,
        "models": ENSEMBLE_MODELS,
        "timezone": "auto",
        "start_date": target.isoformat(),
        "end_date": target.isoformat(),
    }
    if supports_daily:
        params["daily"] = var
    else:
        params["hourly"] = var
    r = await client.get(ENSEMBLE_URL, params=params)
    r.raise_for_status()
    data = r.json()

    members: list[float] = []
    if supports_daily:
        daily = data.get("daily") or {}
        # Every member key looks like `{var}_member01` etc., plus `{var}` is the control
        for key, series in daily.items():
            if key == "time":
                continue
            if not isinstance(series, list):
                continue
            for v in series:
                if v is None:
                    continue
                members.append(float(v))
    else:
        hourly = data.get("hourly") or {}
        # Aggregate hourly per-member to daily
        # Keys like "{var}_member01" are parallel lists
        per_member: dict[str, list[float]] = {}
        for key, series in hourly.items():
            if key == "time" or not isinstance(series, list):
                continue
            per_member[key] = [v for v in series if v is not None]
        for vals in per_member.values():
            if vals:
                members.append(sum(vals))  # sum for precip, sum for snowfall

    return members, days_out


# ----------------------------------------------------------------------
# Step 5: Compute probabilities
# ----------------------------------------------------------------------


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def _build_bundle(
    spec: WeatherSpec,
    lat: float,
    lon: float,
    resolved: str,
    hist_values: list[float],
    hist_annual: list[tuple[int, float]],
    ensemble_values: list[float],
    days_out: int | None,
    market_price: float | None,
) -> WeatherBundle:
    # historical raw
    if hist_values:
        passes = sum(1 for v in hist_values if _apply_op(v, spec.threshold_op, spec.threshold))
        hist_prob = passes / len(hist_values)
    else:
        hist_prob = 0.0

    # Trend — only meaningful for temperature metrics
    trend = _linear_trend(hist_annual) if spec.metric.startswith("temperature") else None
    trend_per_decade = trend * 10 if trend is not None else None

    # Adjusted: shift every historical value up by (years_ahead × trend)
    hist_prob_adj = hist_prob
    if trend is not None and hist_annual:
        latest_year = hist_annual[-1][0]
        years_ahead = max(0, int(spec.target_date[:4]) - latest_year)
        if years_ahead > 0:
            shifted = [v + trend * years_ahead for v in hist_values]
            passes_adj = sum(
                1 for v in shifted if _apply_op(v, spec.threshold_op, spec.threshold)
            )
            hist_prob_adj = passes_adj / len(shifted)

    # Ensemble
    if ensemble_values:
        passes = sum(
            1 for v in ensemble_values if _apply_op(v, spec.threshold_op, spec.threshold)
        )
        e_prob = passes / len(ensemble_values)
        e_mean = statistics.fmean(ensemble_values)
        e_p10 = _percentile(ensemble_values, 0.1)
        e_p90 = _percentile(ensemble_values, 0.9)
    else:
        e_prob = 0.0
        e_mean = e_p10 = e_p90 = None

    return WeatherBundle(
        spec=spec,
        lat=lat,
        lon=lon,
        location_resolved=resolved,
        historical_values=hist_values,
        historical_years=len({int(d) for d, _ in hist_annual}) if hist_annual else 0,
        historical_prob=hist_prob,
        historical_prob_adj=hist_prob_adj,
        historical_trend_c_per_decade=trend_per_decade,
        ensemble_values=ensemble_values,
        ensemble_mean=e_mean,
        ensemble_p10=e_p10,
        ensemble_p90=e_p90,
        ensemble_prob=e_prob,
        ensemble_days_out=days_out,
        market_price=market_price,
    )


# ----------------------------------------------------------------------
# Step 6: LLM synthesis
# ----------------------------------------------------------------------


SYNTHESIZER_SYSTEM = """You are a weather-market analyst. Given:

- A binary market question with an implied probability from market price
- An empirical base rate from 30y of ERA5 reanalysis (same calendar day ±3d)
- A trend-adjusted base rate (if warming trend detected)
- An ensemble forecast probability (ICON + GFS + ECMWF, days out)

Produce a markdown report in EXACTLY this format:

WEATHER ANALYSIS: {market_question}

Market price: {price} (implies {implied}%)

Historical base rate (ERA5, ±3d same calendar day, {years}y):
- Raw: {raw_prob}%
- Climate-adjusted: {adj_prob}%
- Trend: {trend}

Current forecast (Open-Meteo ensemble, {days_out}d out):
- Mean: {mean}{units}
- 10th-90th percentile: {p10}-{p90}{units}
- Probability of threshold: {forecast_prob}%

Probability synthesis: {final_prob}% (confidence: LOW | MEDIUM | HIGH)
Market vs synthesized: {gap} points

One paragraph reasoning (explicitly caveat uncertainty and acknowledge weather forecast horizon).

Final line: JSON {"synthesized_prob": <0-1>, "confidence": "LOW|MEDIUM|HIGH"}

Rules:
- If days_out > 15, downweight ensemble heavily; lean on historical.
- If days_out < 3, weight ensemble ~70%, historical ~30%.
- If ensemble values empty (out of horizon), synthesized_prob ≈ adj historical.
- Confidence HIGH only if days_out ≤ 7 AND ensemble spread (p90-p10) is narrow.
- Do NOT recommend a position. Inform judgment only.
"""


def _synth_user_payload(bundle: WeatherBundle) -> str:
    s = bundle.spec
    market_pct = round(bundle.market_price * 100, 1) if bundle.market_price is not None else None
    trend_str = (
        f"{bundle.historical_trend_c_per_decade:+.2f}{s.units}/decade"
        if bundle.historical_trend_c_per_decade is not None
        else "n/a (non-temperature metric)"
    )
    payload = {
        "market_question": s.raw_question,
        "threshold": f"{s.threshold_op} {s.threshold}{s.units}",
        "target_date": s.target_date,
        "location": bundle.location_resolved,
        "market_price": bundle.market_price,
        "market_implied_pct": market_pct,
        "historical": {
            "years": bundle.historical_years,
            "n_samples": len(bundle.historical_values),
            "raw_prob_pct": round(bundle.historical_prob * 100, 1),
            "adjusted_prob_pct": round(bundle.historical_prob_adj * 100, 1),
            "trend": trend_str,
        },
        "ensemble": {
            "days_out": bundle.ensemble_days_out,
            "n_members": len(bundle.ensemble_values),
            "mean": round(bundle.ensemble_mean, 2) if bundle.ensemble_mean is not None else None,
            "p10": round(bundle.ensemble_p10, 2) if bundle.ensemble_p10 is not None else None,
            "p90": round(bundle.ensemble_p90, 2) if bundle.ensemble_p90 is not None else None,
            "prob_threshold_pct": round(bundle.ensemble_prob * 100, 1),
        },
        "units": s.units,
    }
    return json.dumps(payload, indent=2)


async def _synthesize(bundle: WeatherBundle) -> tuple[str, float, str]:
    payload = _synth_user_payload(bundle)
    res = await complete(
        SYNTHESIZER_SYSTEM,
        payload,
        module="analyzer_weather.synth",
        max_tokens=1200,
        temperature=0.2,
    )
    text = res.text

    # Extract final-line JSON for structured fields. Models sometimes wrap the
    # whole report in ```markdown fences, so we scan bottom-up for any line
    # containing a JSON object with our two keys.
    synthesized_prob = bundle.historical_prob_adj or bundle.historical_prob
    confidence = "LOW"
    for line in reversed(text.strip().splitlines()):
        line = line.strip().lstrip("`").rstrip("`").strip()
        if not line:
            continue
        # Allow lines like `JSON {"...": ...}` — strip leading label
        if line.lower().startswith("json"):
            line = line[4:].strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                parsed = json.loads(line)
                if "synthesized_prob" in parsed:
                    synthesized_prob = float(parsed["synthesized_prob"])
                if "confidence" in parsed:
                    confidence = str(parsed["confidence"]).upper()
                break
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
    return text, synthesized_prob, confidence


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------


async def analyze(
    market_question: str,
    *,
    market_price: float | None = None,
    resolution_rules: str | None = None,
    focus_areas: str | None = None,  # accepted for interface parity with other analyzers
) -> WeatherReport:
    """Full pipeline. Raises on any upstream failure — caller handles."""
    t0 = datetime.now(timezone.utc)
    llm_calls = 0

    spec = await _extract_spec(market_question, resolution_rules)
    llm_calls += 1
    log.info("weather.extract", spec=spec.__dict__)

    target = date.fromisoformat(spec.target_date)
    var = METRIC_TO_VAR[spec.metric]

    async with httpx.AsyncClient(timeout=30.0) as client:
        lat, lon, resolved = await _geocode(client, spec.location)
        log.info("weather.geocode", lat=lat, lon=lon, resolved=resolved)

        hist_task = _fetch_historical(client, lat, lon, target, var)
        ens_task = _fetch_ensemble(client, lat, lon, target, var)
        (hist_values, hist_annual), (ens_values, days_out) = await asyncio.gather(
            hist_task, ens_task
        )

    log.info(
        "weather.data",
        hist_samples=len(hist_values),
        hist_years=len({d for d, _ in hist_annual}),
        ens_members=len(ens_values),
        days_out=days_out,
    )

    bundle = _build_bundle(
        spec, lat, lon, resolved,
        hist_values, hist_annual,
        ens_values, days_out,
        market_price,
    )

    text, synth_prob, confidence = await _synthesize(bundle)
    llm_calls += 1

    market_prob = bundle.market_price if bundle.market_price is not None else None
    gap = None
    if market_prob is not None:
        gap = round((synth_prob - market_prob) * 100, 1)

    return WeatherReport(
        markdown=text,
        synthesized_prob=synth_prob,
        market_prob=market_prob,
        gap_points=gap,
        confidence=confidence,
        bundle=bundle,
        llm_calls=llm_calls,
        duration_s=(datetime.now(timezone.utc) - t0).total_seconds(),
    )


# ----------------------------------------------------------------------
# Persistence helper
# ----------------------------------------------------------------------


async def persist_request(
    polymarket_url: str,
    market_id: str | None,
    market_question: str,
    market_price: float | None,
    report: WeatherReport,
    focus_areas: str | None,
) -> int:
    """Write into analysis_requests table. Returns request_id."""
    from db import connect

    full = {
        "synthesized_prob": report.synthesized_prob,
        "market_prob": report.market_prob,
        "gap_points": report.gap_points,
        "confidence": report.confidence,
        "spec": report.bundle.spec.__dict__,
        "resolved_location": report.bundle.location_resolved,
        "historical_years": report.bundle.historical_years,
        "historical_prob": report.bundle.historical_prob,
        "historical_prob_adj": report.bundle.historical_prob_adj,
        "ensemble_days_out": report.bundle.ensemble_days_out,
        "ensemble_mean": report.bundle.ensemble_mean,
        "ensemble_prob": report.bundle.ensemble_prob,
        "markdown": report.markdown,
    }
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    async with connect() as db:
        cur = await db.execute(
            "INSERT INTO analysis_requests ("
            "  mode, requested_at, polymarket_url, market_id, market_question, "
            "  market_price, user_focus_areas, verdict, full_response_json, "
            "  completed_at, duration_seconds, llm_calls"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "weather",
                now,
                polymarket_url,
                market_id,
                market_question,
                market_price,
                focus_areas,
                report.confidence,
                json.dumps(full),
                now,
                int(report.duration_s),
                report.llm_calls,
            ),
        )
        await db.commit()
        return cur.lastrowid or 0

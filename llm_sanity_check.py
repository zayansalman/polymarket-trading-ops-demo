"""DeepSeek-V3 (via HF Inference) sanity-check on each HIGH/MEDIUM weather rec.

The numeric scanner is precise but narrow — it sees historical frequency and
ensemble spread, but not *why* a forecast might be wrong (synoptic regime,
late-breaking anomaly, sensor issues, market information asymmetry, etc.).
This module asks a reasoning model to sanity-check each rec:

  "Given market question Q, YES price P_m, our model prob P_s, historical
   rate H, ensemble mean E (max °C), days_out D — is this a reasonable bet?"

Output: {verdict: 'CONFIRM'|'CAUTION'|'REJECT', reasoning: <≤1 sentence>}

We do NOT let the LLM *create* recs. It only reviews what the scanner
produced. Fail-open on errors — we don't want HF flakiness to hide real
edges.
"""

from __future__ import annotations

from dataclasses import dataclass

from llm import chat
from logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class SanityCheck:
    verdict: str  # 'CONFIRM' | 'CAUTION' | 'REJECT' | 'ERROR'
    reasoning: str


_SYSTEM = (
    "You are a weather-market sanity reviewer. You see one bucket at a time "
    "from a Polymarket weather event. Your job: given the numeric inputs, "
    "decide whether the bet the system proposes is reasonable. "
    "You do NOT invent new bets. You do not suggest sizing. "
    "Answer ONLY in JSON with keys `verdict` (CONFIRM/CAUTION/REJECT) and "
    "`reasoning` (one short sentence, ≤160 chars). "
    "CONFIRM = numeric edge looks solid and no obvious red flag. "
    "CAUTION = numeric edge is real but one input disagrees strongly (e.g. "
    "ensemble spread is wide, or the bucket is at a tail). "
    "REJECT = edge looks like an artifact (tail of a multi-bucket distribution, "
    "already-settled-ish day, or numeric values look pathological)."
)


def _build_user_prompt(
    question: str,
    location: str,
    days_out: int,
    side: str,
    market_yes: float,
    synth_prob: float,
    hist_prob: float,
    ens_prob: float,
    ens_spread: float | None,
    realized_max: float | None,
    peak_passed: bool,
) -> str:
    lines = [
        f"Market: {question}",
        f"Location: {location}",
        f"Days out: {days_out}",
        f"Proposed side: {side}",
        f"Market YES price: {market_yes:.3f} ({market_yes*100:.1f}%)",
        f"Our synthetic probability: {synth_prob:.3f} ({synth_prob*100:.1f}%)",
        f"Historical 30y probability: {hist_prob:.3f}",
        f"Ensemble probability: {ens_prob:.3f}",
    ]
    if ens_spread is not None:
        lines.append(f"Ensemble spread (P90-P10, °C): {ens_spread:.2f}")
    if realized_max is not None:
        lines.append(
            f"Realized max so far today: {realized_max:.1f}°C "
            f"({'past peak' if peak_passed else 'pre/near peak'})"
        )
    lines.append(
        "Answer JSON like: "
        '{"verdict":"CONFIRM","reasoning":"Ensemble cleanly below bucket + 30y base rate 2%"}'
    )
    return "\n".join(lines)


async def sanity_check_rec(
    *,
    question: str,
    location: str,
    days_out: int,
    side: str,
    market_yes: float,
    synth_prob: float,
    hist_prob: float,
    ens_prob: float,
    ens_spread: float | None = None,
    realized_max: float | None = None,
    peak_passed: bool = False,
) -> SanityCheck:
    """One LLM call, ≤400 tokens out, json-repair on. Fail-open."""
    messages = [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": _build_user_prompt(
                question=question,
                location=location,
                days_out=days_out,
                side=side,
                market_yes=market_yes,
                synth_prob=synth_prob,
                hist_prob=hist_prob,
                ens_prob=ens_prob,
                ens_spread=ens_spread,
                realized_max=realized_max,
                peak_passed=peak_passed,
            ),
        },
    ]
    try:
        r = await chat(
            messages=messages,
            module="llm_sanity_check",
            max_tokens=300,
            temperature=0.1,
            json_mode=True,
        )
    except Exception as e:  # noqa: BLE001 — never block a scan on LLM flakiness
        log.warning("sanity_check.llm_failed", error=str(e)[:200])
        return SanityCheck(verdict="ERROR", reasoning=f"LLM unavailable: {type(e).__name__}")

    data = r.json if isinstance(r.json, dict) else {}
    verdict = str(data.get("verdict", "")).upper().strip()
    if verdict not in ("CONFIRM", "CAUTION", "REJECT"):
        verdict = "CAUTION"
    reasoning = str(data.get("reasoning", "") or r.text[:160]).strip()[:300]
    return SanityCheck(verdict=verdict, reasoning=reasoning)

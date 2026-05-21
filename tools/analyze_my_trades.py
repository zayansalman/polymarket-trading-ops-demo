"""One-shot: pull all my Polymarket trades, classify weather vs non-weather, compute realized PnL.

Output: console summary + writes /tmp/my_trades_analysis.json with the breakdown so we can
audit which methodology is actually winning before stripping anything out.
"""
from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from pathlib import Path

from config import MY_POLYMARKET_PROXY_ADDRESS
from polymarket_client import PolymarketClient

WEATHER_RE = re.compile(
    r"(highest|lowest|max|min)[-\s]?temperature|temperature[-\s]?in[-\s]|"
    r"rain|snow|hottest|coldest|degrees?[-\s]?in[-\s]",
    re.IGNORECASE,
)


def is_weather(slug: str | None, question: str | None) -> bool:
    text = f"{slug or ''} {question or ''}"
    return bool(WEATHER_RE.search(text))


async def main() -> None:
    addr = MY_POLYMARKET_PROXY_ADDRESS
    if not addr:
        raise SystemExit("MY_POLYMARKET_PROXY_ADDRESS not set")

    print(f"Pulling trades + positions for {addr}...")
    async with PolymarketClient() as c:
        trades = await c.get_all_trades(addr, max_pages=10)
        positions = await c.get_positions(addr)

    print(f"  trades:    {len(trades)}")
    print(f"  positions: {len(positions)}")

    # Group trades by conditionId — each market is one settled (or pending) bet
    by_market: dict[str, dict] = defaultdict(
        lambda: {"buys": [], "sells": [], "slug": None, "question": None, "outcome": None}
    )
    for t in trades:
        cid = t.get("conditionId") or t.get("condition_id")
        if not cid:
            continue
        m = by_market[cid]
        m["slug"] = m["slug"] or t.get("slug") or t.get("eventSlug")
        m["question"] = m["question"] or t.get("title") or t.get("question")
        m["outcome"] = m["outcome"] or t.get("outcome")
        side = (t.get("side") or "").upper()
        size = float(t.get("size", 0) or 0)
        price = float(t.get("price", 0) or 0)
        if side == "BUY":
            m["buys"].append((size, price))
        elif side == "SELL":
            m["sells"].append((size, price))

    # Build a position lookup: conditionId -> redeemable + cur value
    pos_by_cid: dict[str, dict] = {}
    for p in positions:
        cid = p.get("conditionId") or p.get("condition_id")
        if cid:
            pos_by_cid[cid] = p

    # Resolve each market via Gamma to get final outcome
    cids = list(by_market.keys())
    print(f"Resolving {len(cids)} markets via Gamma...")
    resolved: dict[str, dict] = {}
    async with PolymarketClient() as c:
        BATCH = 20
        for i in range(0, len(cids), BATCH):
            batch = cids[i : i + BATCH]
            ms = await c.get_markets_by_condition_ids(batch)
            for m in ms:
                rcid = m.get("conditionId") or m.get("condition_id")
                if rcid:
                    resolved[rcid] = m

    # Compute realized PnL per market
    rows = []
    for cid, agg in by_market.items():
        market = resolved.get(cid, {})
        slug = agg["slug"] or market.get("slug")
        question = agg["question"] or market.get("question")
        outcome = agg["outcome"]
        outcome_idx = 0 if (outcome or "").lower() == "yes" else 1
        prices = market.get("outcomePrices")
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except Exception:
                prices = None
        closed = bool(market.get("closed"))
        final_value = None
        if closed and isinstance(prices, list) and outcome_idx < len(prices):
            try:
                final_value = float(prices[outcome_idx])
            except Exception:
                final_value = None

        cost = sum(s * p for s, p in agg["buys"]) - sum(s * p for s, p in agg["sells"])
        shares = sum(s for s, _ in agg["buys"]) - sum(s for s, _ in agg["sells"])

        # Realized PnL only if settled
        realized_pnl = None
        if closed and final_value is not None and shares > 0:
            realized_pnl = (shares * final_value) - cost

        # Position from /positions for unsettled
        pos = pos_by_cid.get(cid, {})
        cur_value = float(pos.get("currentValue") or 0)
        redeemable = bool(pos.get("redeemable"))
        zombie = closed and shares > 0 and not redeemable and final_value == 0

        rows.append({
            "cid": cid,
            "slug": slug,
            "question": (question or "")[:120],
            "is_weather": is_weather(slug, question),
            "side": outcome,
            "shares": round(shares, 4),
            "cost_usd": round(cost, 4),
            "closed": closed,
            "final_value": final_value,
            "realized_pnl_usd": round(realized_pnl, 4) if realized_pnl is not None else None,
            "current_value_usd": round(cur_value, 4),
            "redeemable": redeemable,
            "zombie": zombie,
        })

    # Slice
    weather_rows = [r for r in rows if r["is_weather"]]
    other_rows = [r for r in rows if not r["is_weather"]]

    def summarize(label: str, rs: list[dict]) -> dict:
        settled = [r for r in rs if r["realized_pnl_usd"] is not None]
        wins = [r for r in settled if r["realized_pnl_usd"] > 0]
        losses = [r for r in settled if r["realized_pnl_usd"] <= 0]
        unsettled = [r for r in rs if r["realized_pnl_usd"] is None]
        total_pnl = sum(r["realized_pnl_usd"] for r in settled)
        unsettled_value = sum(r["current_value_usd"] for r in unsettled)
        unsettled_cost = sum(r["cost_usd"] for r in unsettled)
        s = {
            "label": label,
            "total_markets": len(rs),
            "settled": len(settled),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(settled), 3) if settled else None,
            "realized_pnl_usd": round(total_pnl, 2),
            "avg_pnl_per_settled": round(total_pnl / len(settled), 3) if settled else None,
            "unsettled_markets": len(unsettled),
            "unsettled_cost_usd": round(unsettled_cost, 2),
            "unsettled_current_value_usd": round(unsettled_value, 2),
        }
        return s

    summary = {
        "wallet": addr,
        "total_trades_executed": len(trades),
        "total_unique_markets": len(rows),
        "all": summarize("all", rows),
        "weather": summarize("weather", weather_rows),
        "other": summarize("other", other_rows),
    }

    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))

    print("\n=== TOP WEATHER WINS ===")
    weather_settled = sorted(
        [r for r in weather_rows if r["realized_pnl_usd"] is not None],
        key=lambda r: r["realized_pnl_usd"] or 0,
        reverse=True,
    )
    for r in weather_settled[:10]:
        print(f"  +${r['realized_pnl_usd']:>6.2f}  [{r['side']:>3}]  {r['question']}")

    print("\n=== TOP WEATHER LOSSES ===")
    for r in weather_settled[-10:]:
        print(f"  ${r['realized_pnl_usd']:>7.2f}  [{r['side']:>3}]  {r['question']}")

    print("\n=== UNSETTLED WEATHER ===")
    for r in [r for r in weather_rows if r["realized_pnl_usd"] is None][:10]:
        print(f"  cost ${r['cost_usd']:>5.2f} → cur ${r['current_value_usd']:>5.2f}  "
              f"[{r['side']:>3}]  {r['question']}")

    Path("/tmp/my_trades_analysis.json").write_text(
        json.dumps({"summary": summary, "rows": rows}, indent=2)
    )
    print("\nFull breakdown → /tmp/my_trades_analysis.json")


if __name__ == "__main__":
    asyncio.run(main())

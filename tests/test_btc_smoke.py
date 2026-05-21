from pathlib import Path

from btc_bot.history import load_btc_history_stats
from btc_bot.paper import _notional_from_confidence
from config import BTC_PAPER_MAX_TRADE_USD, BTC_PAPER_MIN_CONFIDENCE, BTC_PAPER_MIN_TRADE_USD


def test_confidence_sizing_stays_in_paper_bounds() -> None:
    assert _notional_from_confidence(BTC_PAPER_MIN_CONFIDENCE) >= BTC_PAPER_MIN_TRADE_USD
    assert _notional_from_confidence(0.99) <= BTC_PAPER_MAX_TRADE_USD


def test_history_csv_is_optional(tmp_path: Path) -> None:
    stats = load_btc_history_stats(tmp_path / "missing.csv")
    assert stats.found is False
    assert stats.btc_rows == 0

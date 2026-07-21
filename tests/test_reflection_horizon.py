"""Reflection must not overclaim beyond the window it actually measured.

The outcome window is a handful of trading days; the decisions it grades state
horizons of months. These tests pin the three things that keep the resulting
lesson honest: the window length reaches the model, the rating reaches it so the
long-only return sign is readable, and a window that hasn't closed yet is not
graded early (resolution is one-way — an early verdict is permanent).
"""

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd

from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.graph.reflection import Reflector
from tradingagents.graph.trading_graph import TradingAgentsGraph

DECISION_SELL = (
    "Rating: Sell\nExit position immediately.\n\n**Time Horizon**: 3-6 months"
)


def _price_df(prices):
    return pd.DataFrame({"Close": prices})


def _reflect(**kwargs):
    """Run a reflection against a mock LLM, return the human message text."""
    mock_llm = MagicMock()
    mock_llm.invoke.return_value.content = "ok"
    reflector = Reflector(mock_llm)
    reflector.reflect_on_final_decision(
        final_decision=DECISION_SELL, raw_return=-0.08, alpha_return=-0.05, **kwargs
    )
    messages = mock_llm.invoke.call_args[0][0]
    return dict(messages)


class TestPromptScopesToWindow:
    """The system prompt must ask a question the window can answer."""

    def test_forbids_scoring_the_long_thesis(self):
        system = _reflect()["system"].lower()
        assert "open" in system and "not being scored" in system

    def test_asks_about_entry_not_direction(self):
        """The old prompt demanded a verdict on the directional call outright."""
        system = _reflect()["system"]
        assert "Was the directional call correct?" not in system
        assert "timing" in system.lower()

    def test_states_long_only_sign_convention(self):
        """Without this a correct bearish call reads as a loss."""
        system = _reflect()["system"].lower()
        assert "long-only" in system
        assert "sell" in system and "right" in system


class TestOutcomeContext:
    def test_window_length_reaches_the_model(self):
        assert "Observation window: 5 trading days" in _reflect(holding_days=5)["human"]

    def test_single_day_window_is_singular(self):
        assert "Observation window: 1 trading day\n" in _reflect(holding_days=1)["human"]

    def test_rating_reaches_the_model(self):
        assert "Rating under review: Sell" in _reflect(rating="Sell")["human"]

    def test_returns_still_present(self):
        human = _reflect(holding_days=5, rating="Sell")["human"]
        assert "-8.0%" in human and "-5.0%" in human
        assert "Exit position immediately." in human

    def test_omitted_context_adds_no_lines(self):
        """Optional so existing callers keep working, not silently emitting 'None'."""
        human = _reflect()["human"]
        assert "Observation window" not in human
        assert "Rating under review" not in human


class TestPartialWindowNotGradedEarly:
    """A window that can still fill must stay pending rather than lock in."""

    @staticmethod
    def _fetch(trade_date, stock_prices, bench_prices):
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        with patch("yfinance.Ticker") as mock_ticker_cls:
            mock_ticker_cls.side_effect = lambda sym: MagicMock(
                **{
                    "history.return_value": _price_df(
                        bench_prices if sym == "SPY" else stock_prices
                    )
                }
            )
            return TradingAgentsGraph._fetch_returns(
                mock_graph, "NVDA", trade_date, benchmark="SPY"
            )

    def test_recent_partial_window_defers(self):
        recent = (date.today() - timedelta(days=2)).strftime("%Y-%m-%d")
        raw, alpha, days = self._fetch(recent, [100.0, 102.0], [400.0, 401.0])
        assert (raw, alpha, days) == (None, None, None)

    def test_recent_full_window_resolves(self):
        """Deferral is about the window, not about the date being recent."""
        recent = (date.today() - timedelta(days=2)).strftime("%Y-%m-%d")
        raw, alpha, days = self._fetch(
            recent,
            [100.0, 102.0, 104.0, 103.0, 105.0, 106.0],
            [400.0, 402.0, 404.0, 403.0, 405.0, 406.0],
        )
        assert days == 5
        assert raw is not None and alpha is not None

    def test_elapsed_partial_window_resolves(self):
        """A halt or delisting must not pin the entry as pending forever."""
        old = (date.today() - timedelta(days=90)).strftime("%Y-%m-%d")
        raw, alpha, days = self._fetch(old, [100.0, 102.0, 104.0], [400.0, 401.0, 402.0])
        assert days == 2
        assert raw is not None and alpha is not None

    def test_boundary_uses_the_buffered_end_date(self):
        """The cutoff is holding_days + 7 calendar days, matching the fetch range."""
        just_inside = (date.today() - timedelta(days=11)).strftime("%Y-%m-%d")
        assert datetime.strptime(just_inside, "%Y-%m-%d") + timedelta(
            days=12
        ) > datetime.now()
        raw, _, _ = self._fetch(just_inside, [100.0, 102.0], [400.0, 401.0])
        assert raw is None


class TestResolvePassesContextThrough:
    def test_holding_days_and_rating_forwarded(self, tmp_path):
        log = TradingMemoryLog({"memory_log_path": str(tmp_path / "m.md")})
        log.store_decision("NVDA", "2026-01-05", DECISION_SELL)

        mock_graph = MagicMock(spec=TradingAgentsGraph)
        mock_graph.memory_log = log
        mock_graph._resolve_benchmark = MagicMock(return_value="SPY")
        mock_graph._fetch_returns = MagicMock(return_value=(-0.08, -0.05, 3))
        mock_graph.reflector = MagicMock()
        mock_graph.reflector.reflect_on_final_decision.return_value = "Setup confirmed."

        TradingAgentsGraph._resolve_pending_entries(mock_graph, "NVDA")

        kwargs = mock_graph.reflector.reflect_on_final_decision.call_args.kwargs
        assert kwargs["holding_days"] == 3
        assert kwargs["rating"] == "Sell"

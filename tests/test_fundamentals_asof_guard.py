"""Guard against look-ahead bias in live fundamentals snapshots.

Vendor overview endpoints return only present-day figures. Serving them for a
historical trade date reports the future as the past, which silently invalidates
any backtest. These tests pin both directions: the guard fires on a stale date
and stays silent on a recent one.
"""

from datetime import date, timedelta

import pytest

from tradingagents.dataflows import config as dataflows_config
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.utils import guard_fundamentals_asof


@pytest.fixture(autouse=True)
def _reset_config():
    """Each test drives the guard through the real config accessor."""
    original = dataflows_config._config
    dataflows_config._config = None
    yield
    dataflows_config._config = original


def _set_threshold(value):
    dataflows_config.initialize_config()
    dataflows_config._config["fundamentals_max_staleness_days"] = value


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).strftime("%Y-%m-%d")


class TestGuardFires:
    def test_raises_on_historical_date(self):
        _set_threshold(7)
        with pytest.raises(NoMarketDataError):
            guard_fundamentals_asof("NVDA", "NVDA", "2024-05-10")

    def test_raises_just_past_threshold(self):
        _set_threshold(7)
        with pytest.raises(NoMarketDataError):
            guard_fundamentals_asof("NVDA", "NVDA", _days_ago(8))

    def test_detail_explains_look_ahead(self):
        """The router surfaces ``detail`` verbatim, so it must name the cause."""
        _set_threshold(7)
        with pytest.raises(NoMarketDataError) as exc:
            guard_fundamentals_asof("NVDA", "NVDA", "2024-05-10")
        detail = exc.value.detail
        assert "2024-05-10" in detail
        assert "live-only" in detail

    def test_carries_symbols_for_router_message(self):
        _set_threshold(7)
        with pytest.raises(NoMarketDataError) as exc:
            guard_fundamentals_asof("XAUUSD", "GC=F", "2024-05-10")
        assert exc.value.symbol == "XAUUSD"
        assert exc.value.canonical == "GC=F"


class TestGuardSilent:
    """The live-trading path must not be disturbed."""

    def test_today_passes(self):
        _set_threshold(7)
        guard_fundamentals_asof("NVDA", "NVDA", _days_ago(0))

    def test_within_threshold_passes(self):
        _set_threshold(7)
        guard_fundamentals_asof("NVDA", "NVDA", _days_ago(7))

    def test_future_date_passes(self):
        """Negative age is not staleness; date validation is a separate concern."""
        _set_threshold(7)
        guard_fundamentals_asof("NVDA", "NVDA", _days_ago(-3))

    def test_none_date_passes(self):
        _set_threshold(7)
        guard_fundamentals_asof("NVDA", "NVDA", None)

    def test_malformed_date_passes(self):
        """Not a staleness signal — the caller's own validation owns this."""
        _set_threshold(7)
        guard_fundamentals_asof("NVDA", "NVDA", "not-a-date")

    def test_none_threshold_disables_guard(self):
        _set_threshold(None)
        guard_fundamentals_asof("NVDA", "NVDA", "2024-05-10")


class TestVendorsWired:
    """Both vendors must refuse: fixing only one leaves the leak reachable."""

    def test_yfinance_refuses_before_network_call(self, monkeypatch):
        from tradingagents.dataflows import y_finance

        _set_threshold(7)

        def _boom(*_a, **_k):
            raise AssertionError("vendor was called despite a stale date")

        monkeypatch.setattr(y_finance.yf, "Ticker", _boom)
        with pytest.raises(NoMarketDataError):
            y_finance.get_fundamentals("NVDA", "2024-05-10")

    def test_alpha_vantage_refuses_before_api_call(self, monkeypatch):
        from tradingagents.dataflows import alpha_vantage_fundamentals as av

        _set_threshold(7)

        def _boom(*_a, **_k):
            raise AssertionError("API budget spent despite a stale date")

        monkeypatch.setattr(av, "_make_api_request", _boom)
        with pytest.raises(NoMarketDataError):
            av.get_fundamentals("NVDA", "2024-05-10")


class TestRouterBehavior:
    def test_router_returns_no_data_sentinel(self, monkeypatch):
        """A refusal must reach the agent as NO_DATA_AVAILABLE, not a raw traceback.

        This is what makes the guard "loud" in this codebase: the agent is told
        explicitly not to estimate, rather than narrating around a stale number.
        """
        from tradingagents.dataflows import interface

        _set_threshold(7)
        monkeypatch.setitem(
            interface.VENDOR_METHODS["get_fundamentals"],
            "yfinance",
            lambda ticker, curr_date=None: guard_fundamentals_asof(
                ticker, ticker, curr_date
            ),
        )
        result = interface.route_to_vendor("get_fundamentals", "NVDA", "2024-05-10")
        assert "NO_DATA_AVAILABLE" in result


class TestConfigExposure:
    def test_env_override_registered(self):
        from tradingagents.default_config import _ENV_OVERRIDES

        assert (
            _ENV_OVERRIDES["TRADINGAGENTS_FUNDAMENTALS_MAX_STALENESS_DAYS"]
            == "fundamentals_max_staleness_days"
        )

    def test_default_present(self):
        from tradingagents.default_config import DEFAULT_CONFIG

        assert DEFAULT_CONFIG["fundamentals_max_staleness_days"] == 7

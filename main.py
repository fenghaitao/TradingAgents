import os
import sys
from datetime import date, datetime

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

# Ticker and trade date are configurable so this script doesn't have to be
# edited between runs:
#
#     python main.py                      # defaults below
#     python main.py NVDA 2026-07-13      # positional
#     TRADINGAGENTS_TICKER=INTC python main.py
#
# Precedence is argv > env > default, matching the "explicit wins" rule the CLI
# applies in ``_build_run_config``.
DEFAULT_TICKER = "NVDA"
# Today, not a literal: a hardcoded date silently ages past
# ``fundamentals_max_staleness_days`` and starts getting its fundamentals refused.
DEFAULT_TRADE_DATE = date.today().strftime("%Y-%m-%d")


def _resolve(argv_index: int, env_var: str, default: str) -> str:
    if len(sys.argv) > argv_index:
        return sys.argv[argv_index]
    return os.getenv(env_var) or default


ticker = _resolve(1, "TRADINGAGENTS_TICKER", DEFAULT_TICKER).upper()
trade_date = _resolve(2, "TRADINGAGENTS_TRADE_DATE", DEFAULT_TRADE_DATE)

# Validate here so a typo fails immediately instead of surfacing as an obscure
# vendor error several minutes and several LLM calls into the run.
try:
    datetime.strptime(trade_date, "%Y-%m-%d")
except ValueError:
    raise SystemExit(
        f"Invalid trade date {trade_date!r} — expected YYYY-MM-DD."
    ) from None

# DEFAULT_CONFIG already applies TRADINGAGENTS_* env-var overrides
# (llm_provider, deep_think_llm, quick_think_llm, backend_url, etc.),
# so users can switch models or endpoints purely via .env without
# editing this script. Override individual keys here only when you
# want a hard-coded value that should ignore the environment.
config = DEFAULT_CONFIG.copy()

# Initialize with custom config
ta = TradingAgentsGraph(debug=True, config=config)

# forward propagate
_, decision = ta.propagate(ticker, trade_date)
print(decision)

# Reflection is automatic and deferred: this run appends a `pending` entry to
# the memory log, and the next run for the same ticker resolves it — fetching
# realized returns vs the benchmark and appending an LLM reflection. No manual
# call is needed.

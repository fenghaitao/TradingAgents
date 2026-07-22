#!/usr/bin/env python3
"""Batch run TradingAgents analysis for multiple tickers and compare results."""

import os
import sys
from datetime import date, datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

TICKERS = [
    # Semiconductor chain
    "NVDA",   # Nvidia - AI chip leader
    "TSM",    # TSMC - foundry behind all chips
    "QCOM",   # Qualcomm - mobile/auto semis
    # Chinese tech
    "JD",     # JD.com - e-commerce peer to BABA
    # US mega-cap / momentum
    "META",   # Meta - social media, AI
    "TSLA",   # Tesla - EV, AI
]

TRADE_DATE = date.today().strftime("%Y-%m-%d")

config = DEFAULT_CONFIG.copy()

results = {}

for i, ticker in enumerate(TICKERS, 1):
    print(f"\n{'=' * 70}")
    print(f"[{i}/{len(TICKERS)}] Running analysis for {ticker} on {TRADE_DATE}")
    print(f"{'=' * 70}\n")

    try:
        ta = TradingAgentsGraph(debug=True, config=config)
        _, decision = ta.propagate(ticker, TRADE_DATE)
        results[ticker] = decision
        print(f"\n--- {ticker} Decision ---")
        print(decision)
    except Exception as e:
        results[ticker] = f"ERROR: {e}"
        print(f"\n--- {ticker} FAILED: {e} ---")

# Summary
print(f"\n{'=' * 70}")
print("BATCH SUMMARY")
print(f"{'=' * 70}")
for ticker, decision in results.items():
    print(f"\n### {ticker} ###")
    # Print first 500 chars of decision for quick comparison
    print(str(decision)[:500])

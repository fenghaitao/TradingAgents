"""Tests for milestone persistence in TradingMemoryLog.

The section-parsing tests here are the regression gate for the entry format:
inserting MILESTONES / THESIS_REFLECTION between the existing sections is
exactly the change that makes one section's regex swallow another, leaking
milestone text into ``entry["decision"]`` — which feeds every agent prompt.
"""

from unittest.mock import MagicMock

from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.agents.schemas import (
    Milestone,
    PortfolioDecision,
    PortfolioRating,
    render_pm_decision,
)
from tradingagents.agents.utils.memory import TradingMemoryLog

_SEP = TradingMemoryLog._SEPARATOR

M1 = "- [2026-10-24 | quant | pending] Foundry revenue turns YoY-positive"
M2 = "- [2026-12-31 | qual | pending] 18A ships to an external customer"


def make_log(tmp_path, **cfg):
    return TradingMemoryLog({"memory_log_path": str(tmp_path / "mem.md"), **cfg})


def pm_decision(**overrides):
    base = {
        "rating": PortfolioRating.BUY,
        "executive_summary": "Enter on weakness, 5% cap.",
        "investment_thesis": "Foundry turnaround is underpriced by the market.",
        "milestones": [
            Milestone(
                claim="Foundry revenue turns YoY-positive",
                due_date="2026-10-24",
                kind="quant",
            ),
            Milestone(
                claim="18A ships to an external customer",
                due_date="2026-12-31",
                kind="qual",
            ),
        ],
    }
    return render_pm_decision(PortfolioDecision(**{**base, **overrides}))


# ---------------------------------------------------------------------------
# Store → parse round trip
# ---------------------------------------------------------------------------

def test_store_extracts_milestones_and_keeps_decision_verbatim(tmp_path):
    log = make_log(tmp_path)
    rendered = pm_decision()
    log.store_decision("INTC", "2026-07-21", rendered)

    entry = log.load_entries()[0]

    # The MILESTONES: tracker was written and parsed back.
    assert [m["claim"] for m in entry["milestones"]] == [
        "Foundry revenue turns YoY-positive",
        "18A ships to an external customer",
    ]
    assert all(m["status"] == "pending" for m in entry["milestones"])

    # DECISION is the PM's output verbatim — the audit copy, untouched.
    assert entry["decision"] == rendered
    # ...and the tracker block did not leak into it.
    assert "MILESTONES:" not in entry["decision"]


def test_entry_without_milestones_keeps_the_legacy_shape(tmp_path):
    """No milestones ⇒ no MILESTONES: section, so old readers are unaffected."""
    log = make_log(tmp_path)
    log.store_decision("BABA", "2026-07-21", pm_decision(milestones=[]))

    raw = (tmp_path / "mem.md").read_text(encoding="utf-8")
    assert "MILESTONES:" not in raw

    entry = log.load_entries()[0]
    assert entry["milestones"] == []
    assert entry["thesis_reflection"] == ""


def test_legacy_entry_written_before_the_feature_still_parses(tmp_path):
    log = make_log(tmp_path)
    (tmp_path / "mem.md").write_text(
        "[2026-06-22 | INTC | Hold | -6.5% | -6.1% | 5d]\n\n"
        "DECISION:\nRating: Hold. Wait for the Q2 print.\n\n"
        "REFLECTION:\nEntry was early; the catalyst had not landed." + _SEP,
        encoding="utf-8",
    )
    entry = log.load_entries()[0]
    assert entry["decision"] == "Rating: Hold. Wait for the Q2 print."
    assert entry["reflection"] == "Entry was early; the catalyst had not landed."
    assert entry["milestones"] == []
    assert entry["thesis_reflection"] == ""


# ---------------------------------------------------------------------------
# Section isolation — the swallow regression
# ---------------------------------------------------------------------------

def _four_section_entry(order=("DECISION", "MILESTONES", "REFLECTION", "THESIS_REFLECTION")):
    bodies = {
        "DECISION": "**Rating**: Buy\n\n**Investment Thesis**: Foundry turnaround.",
        "MILESTONES": f"{M1}\n{M2}",
        "REFLECTION": "Five-day window was noise; entry timing was acceptable.",
        "THESIS_REFLECTION": "Both milestones hit; the foundry thesis held.",
    }
    sections = "\n\n".join(f"{name}:\n{bodies[name]}" for name in order)
    return "[2026-07-21 | INTC | Buy | +2.0% | +1.1% | 5d]\n\n" + sections + _SEP


def test_all_four_sections_parse_without_leaking(tmp_path):
    log = make_log(tmp_path)
    (tmp_path / "mem.md").write_text(_four_section_entry(), encoding="utf-8")

    entry = log.load_entries()[0]
    assert entry["decision"] == "**Rating**: Buy\n\n**Investment Thesis**: Foundry turnaround."
    assert entry["reflection"] == "Five-day window was noise; entry timing was acceptable."
    assert entry["thesis_reflection"] == "Both milestones hit; the foundry thesis held."
    assert len(entry["milestones"]) == 2

    # No section absorbed a later header.
    for field in ("decision", "reflection", "thesis_reflection"):
        for header in ("MILESTONES:", "REFLECTION:", "THESIS_REFLECTION:"):
            assert header not in entry[field], f"{field} swallowed {header}"


def test_reflection_does_not_match_inside_thesis_reflection(tmp_path):
    """An entry with only THESIS_REFLECTION must leave `reflection` empty."""
    log = make_log(tmp_path)
    (tmp_path / "mem.md").write_text(
        "[2026-07-21 | INTC | Buy | pending]\n\n"
        "DECISION:\nRating: Buy.\n\n"
        "THESIS_REFLECTION:\nThe thesis held." + _SEP,
        encoding="utf-8",
    )
    entry = log.load_entries()[0]
    assert entry["reflection"] == ""
    assert entry["thesis_reflection"] == "The thesis held."


def test_parsing_is_independent_of_section_order(tmp_path):
    """Sections appended out of order must still parse correctly."""
    log = make_log(tmp_path)
    (tmp_path / "mem.md").write_text(
        _four_section_entry(
            order=("DECISION", "MILESTONES", "THESIS_REFLECTION", "REFLECTION")
        ),
        encoding="utf-8",
    )
    entry = log.load_entries()[0]
    assert entry["reflection"] == "Five-day window was noise; entry timing was acceptable."
    assert entry["thesis_reflection"] == "Both milestones hit; the foundry thesis held."


def test_five_day_reflection_appends_after_milestones(tmp_path):
    """The existing Phase-B writer must not disturb the milestone tracker."""
    log = make_log(tmp_path)
    log.store_decision("INTC", "2026-07-21", pm_decision())
    log.update_with_outcome("INTC", "2026-07-21", -0.065, -0.061, 5, "Entry was early.")

    entry = log.load_entries()[0]
    assert entry["pending"] is False
    assert entry["reflection"] == "Entry was early."
    assert len(entry["milestones"]) == 2
    assert "REFLECTION:" not in entry["decision"]


# ---------------------------------------------------------------------------
# update_milestone_statuses
# ---------------------------------------------------------------------------

def test_grading_rewrites_only_the_tracker(tmp_path):
    log = make_log(tmp_path)
    rendered = pm_decision()
    log.store_decision("INTC", "2026-07-21", rendered)

    assert log.update_milestone_statuses(
        "INTC", "2026-07-21", {"Foundry revenue turns YoY-positive": "hit"}
    )

    entry = log.load_entries()[0]
    assert [(m["claim"], m["status"]) for m in entry["milestones"]] == [
        ("Foundry revenue turns YoY-positive", "hit"),
        ("18A ships to an external customer", "pending"),
    ]
    # The frozen audit copy inside DECISION still reads "pending".
    assert entry["decision"] == rendered


def test_grading_is_idempotent(tmp_path):
    log = make_log(tmp_path)
    log.store_decision("INTC", "2026-07-21", pm_decision())
    statuses = {"Foundry revenue turns YoY-positive": "hit"}

    assert log.update_milestone_statuses("INTC", "2026-07-21", statuses) is True
    after_first = (tmp_path / "mem.md").read_text(encoding="utf-8")

    assert log.update_milestone_statuses("INTC", "2026-07-21", statuses) is False
    assert (tmp_path / "mem.md").read_text(encoding="utf-8") == after_first


def test_thesis_reflection_is_appended_once(tmp_path):
    log = make_log(tmp_path)
    log.store_decision("INTC", "2026-07-21", pm_decision())

    log.update_milestone_statuses(
        "INTC",
        "2026-07-21",
        {"Foundry revenue turns YoY-positive": "hit", "18A ships to an external customer": "miss"},
        thesis_reflection="Revenue inflected but the customer win never landed.",
    )
    entry = log.load_entries()[0]
    assert entry["thesis_reflection"] == "Revenue inflected but the customer win never landed."

    # A second call with a different reflection must not append a duplicate.
    log.update_milestone_statuses(
        "INTC", "2026-07-21", {}, thesis_reflection="A different lesson."
    )
    raw = (tmp_path / "mem.md").read_text(encoding="utf-8")
    assert raw.count("THESIS_REFLECTION:") == 1
    assert "A different lesson." not in raw


def test_grading_ignores_unrelated_entries(tmp_path):
    log = make_log(tmp_path)
    log.store_decision("INTC", "2026-07-21", pm_decision())
    log.store_decision("BABA", "2026-07-21", pm_decision())

    log.update_milestone_statuses(
        "INTC", "2026-07-21", {"Foundry revenue turns YoY-positive": "hit"}
    )

    intc, baba = log.load_entries()
    assert intc["milestones"][0]["status"] == "hit"
    assert baba["milestones"][0]["status"] == "pending"


def test_grading_a_missing_entry_is_a_no_op(tmp_path):
    log = make_log(tmp_path)
    log.store_decision("INTC", "2026-07-21", pm_decision())
    assert log.update_milestone_statuses("NVDA", "2026-07-21", {"x": "hit"}) is False


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------

def test_rotation_keeps_entries_with_pending_milestones(tmp_path):
    """A resolved tag whose thesis is still ungraded must survive eviction."""
    log = make_log(tmp_path, memory_log_max_entries=1)

    # Oldest: resolved 5d tag, milestones still pending -> must be kept.
    log.store_decision("INTC", "2026-07-01", pm_decision())
    log.update_with_outcome("INTC", "2026-07-01", 0.01, 0.005, 5, "r1")
    # Two fully closed entries with no milestones -> eligible for eviction.
    for date in ("2026-07-02", "2026-07-03"):
        log.store_decision("BABA", date, pm_decision(milestones=[]))
        log.update_with_outcome("BABA", date, 0.01, 0.005, 5, "r")

    dates = [(e["date"], e["ticker"]) for e in log.load_entries()]
    assert ("2026-07-01", "INTC") in dates, "entry with a pending milestone was evicted"


def test_rotation_evicts_once_all_milestones_close(tmp_path):
    log = make_log(tmp_path, memory_log_max_entries=1)
    log.store_decision("INTC", "2026-07-01", pm_decision())
    log.update_with_outcome("INTC", "2026-07-01", 0.01, 0.005, 5, "r1")
    log.store_decision("BABA", "2026-07-02", pm_decision(milestones=[]))
    log.update_with_outcome("BABA", "2026-07-02", 0.01, 0.005, 5, "r2")

    assert len(log.load_entries()) == 2  # INTC held back by its milestones

    log.update_milestone_statuses(
        "INTC",
        "2026-07-01",
        {
            "Foundry revenue turns YoY-positive": "hit",
            "18A ships to an external customer": "expired",
        },
    )
    remaining = [(e["date"], e["ticker"]) for e in log.load_entries()]
    assert remaining == [("2026-07-02", "BABA")]


# ---------------------------------------------------------------------------
# Prompt injection
# ---------------------------------------------------------------------------

def test_cross_ticker_lesson_prefers_the_thesis_reflection(tmp_path):
    log = make_log(tmp_path)
    log.store_decision("INTC", "2026-07-01", pm_decision())
    log.update_with_outcome("INTC", "2026-07-01", 0.01, 0.005, 5, "Five-day entry lesson.")
    log.update_milestone_statuses(
        "INTC",
        "2026-07-01",
        {
            "Foundry revenue turns YoY-positive": "hit",
            "18A ships to an external customer": "hit",
        },
        thesis_reflection="Graded thesis lesson.",
    )

    context = log.get_past_context("NVDA")
    assert "Graded thesis lesson." in context
    assert "Five-day entry lesson." not in context


def test_pm_node_to_log_capture_chain(tmp_path):
    """End-to-end capture: PM structured output -> rendered markdown -> log tracker.

    Covers the seam the unit tests above each only see one side of — the PM node
    renders, and the log extracts from that rendering.
    """
    decision = PortfolioDecision(
        rating=PortfolioRating.OVERWEIGHT,
        executive_summary="Build gradually over two weeks.",
        investment_thesis="AI capex cycle remains intact.",
        milestones=[
            Milestone(
                claim="Data-center revenue exceeds $30B",
                due_date="2026-11-19",
                kind="quant",
            )
        ],
    )
    structured = MagicMock()
    structured.invoke.return_value = decision
    llm = MagicMock()
    llm.with_structured_output.return_value = structured

    state = {
        "company_of_interest": "NVDA",
        "past_context": "",
        "risk_debate_state": {
            "history": "Risk debate history.",
            "aggressive_history": "",
            "conservative_history": "",
            "neutral_history": "",
            "judge_decision": "",
            "current_aggressive_response": "",
            "current_conservative_response": "",
            "current_neutral_response": "",
            "count": 1,
        },
        "market_report": "Market report.",
        "sentiment_report": "Sentiment report.",
        "news_report": "News report.",
        "fundamentals_report": "Fundamentals report.",
        "investment_plan": "Research plan.",
        "trader_investment_plan": "Trader plan.",
    }
    result = create_portfolio_manager(llm)(state)

    log = make_log(tmp_path)
    log.store_decision("NVDA", "2026-07-21", result["final_trade_decision"])

    entry = log.load_entries()[0]
    assert entry["rating"] == "Overweight"
    assert entry["milestones"] == [
        {
            "claim": "Data-center revenue exceeds $30B",
            "due_date": "2026-11-19",
            "kind": "quant",
            "status": "pending",
        }
    ]


def test_same_ticker_context_includes_both_reflections(tmp_path):
    log = make_log(tmp_path)
    log.store_decision("INTC", "2026-07-01", pm_decision())
    log.update_with_outcome("INTC", "2026-07-01", 0.01, 0.005, 5, "Five-day entry lesson.")
    log.update_milestone_statuses(
        "INTC", "2026-07-01", {}, thesis_reflection="Graded thesis lesson."
    )

    context = log.get_past_context("INTC")
    assert "Five-day entry lesson." in context
    assert "Graded thesis lesson." in context

"""Tests for the milestone resolver on TradingAgentsGraph.

The evaluator itself is a stub in this PR, so these drive the resolver with a
mock evaluator. What is being tested is the scaffold around it: the due-date
scan, the defer/expire rule, the gate, and the exactly-once thesis reflection.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.graph.trading_graph import TradingAgentsGraph

_SEP = TradingMemoryLog._SEPARATOR

CLAIM_A = "Foundry revenue turns YoY-positive"
CLAIM_B = "18A ships to an external customer"


def days_out(n: int) -> str:
    """A YYYY-MM-DD date n days from now (negative = in the past)."""
    return (datetime.now() + timedelta(days=n)).strftime("%Y-%m-%d")


def write_entry(path, ticker, date, milestones, tag_tail="| pending]", extra=""):
    """Append one entry with the given (due_date, kind, status, claim) milestones."""
    lines = "\n".join(
        f"- [{due} | {kind} | {status}] {claim}" for due, kind, status, claim in milestones
    )
    text = (
        f"[{date} | {ticker} | Buy {tag_tail}\n\n"
        f"DECISION:\n**Rating**: Buy\n\n**Investment Thesis**: Foundry turnaround.\n\n"
        f"MILESTONES:\n{lines}"
    )
    if extra:
        text += f"\n\n{extra}"
    with open(path, "a", encoding="utf-8") as f:
        f.write(text + _SEP)


def make_graph(log, evaluator=None, enabled=True, grace_days=30):
    graph = MagicMock(spec=TradingAgentsGraph)
    graph.config = {
        "milestone_grading_enabled": enabled,
        "milestone_grace_days": grace_days,
    }
    graph.memory_log = log
    graph.reflector = MagicMock()
    graph.reflector.reflect_on_thesis.return_value = "The thesis held on revenue."
    graph._evaluate_milestone = evaluator or (lambda entry, m, as_of: None)
    # Bind the real helper so the mock delegates to the code under test.
    graph._resolve_entry_milestones = lambda *a: TradingAgentsGraph._resolve_entry_milestones(
        graph, *a
    )
    return graph


def resolve(graph, ticker="INTC"):
    TradingAgentsGraph._resolve_milestones(graph, ticker)


@pytest.fixture()
def log(tmp_path):
    return TradingMemoryLog({"memory_log_path": str(tmp_path / "mem.md")})


@pytest.fixture()
def log_path(tmp_path):
    return tmp_path / "mem.md"


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def test_resolver_is_a_no_op_when_disabled(log, log_path):
    write_entry(log_path, "INTC", "2026-01-05", [(days_out(-90), "quant", "pending", CLAIM_A)])
    before = log_path.read_text(encoding="utf-8")

    graph = make_graph(log, evaluator=lambda *a: "hit", enabled=False)
    resolve(graph)

    assert log_path.read_text(encoding="utf-8") == before


def test_disabled_resolver_never_calls_the_evaluator(log, log_path):
    write_entry(log_path, "INTC", "2026-01-05", [(days_out(-90), "quant", "pending", CLAIM_A)])
    evaluator = MagicMock(return_value="hit")

    resolve(make_graph(log, evaluator=evaluator, enabled=False))

    evaluator.assert_not_called()


# ---------------------------------------------------------------------------
# The due-date scan
# ---------------------------------------------------------------------------

def test_milestone_not_yet_due_is_left_alone(log, log_path):
    write_entry(log_path, "INTC", "2026-01-05", [(days_out(60), "quant", "pending", CLAIM_A)])
    evaluator = MagicMock(return_value="hit")

    resolve(make_graph(log, evaluator=evaluator))

    evaluator.assert_not_called()
    assert log.load_entries()[0]["milestones"][0]["status"] == "pending"


def test_due_milestone_is_graded(log, log_path):
    write_entry(
        log_path,
        "INTC",
        "2026-01-05",
        [(days_out(-1), "quant", "pending", CLAIM_A), (days_out(60), "qual", "pending", CLAIM_B)],
    )
    resolve(make_graph(log, evaluator=lambda entry, m, as_of: "hit"))

    statuses = {m["claim"]: m["status"] for m in log.load_entries()[0]["milestones"]}
    assert statuses == {CLAIM_A: "hit", CLAIM_B: "pending"}


def test_evaluator_is_bounded_to_the_due_date_not_today(log, log_path):
    """The as-of passed to the evaluator must be the due date — no look-ahead."""
    due = days_out(-45)
    write_entry(log_path, "INTC", "2026-01-05", [(due, "quant", "pending", CLAIM_A)])
    evaluator = MagicMock(return_value="hit")

    resolve(make_graph(log, evaluator=evaluator))

    _entry, milestone, as_of = evaluator.call_args[0]
    assert as_of == due
    assert as_of != datetime.now().strftime("%Y-%m-%d")
    assert milestone["claim"] == CLAIM_A


# ---------------------------------------------------------------------------
# Defer vs expire
# ---------------------------------------------------------------------------

def test_undetermined_within_grace_stays_pending(log, log_path):
    """The stub evaluator must never close a milestone that is still in grace."""
    write_entry(log_path, "INTC", "2026-01-05", [(days_out(-10), "quant", "pending", CLAIM_A)])

    resolve(make_graph(log, grace_days=30))  # default stub -> None

    assert log.load_entries()[0]["milestones"][0]["status"] == "pending"


def test_undetermined_past_grace_expires(log, log_path):
    write_entry(log_path, "INTC", "2026-01-05", [(days_out(-45), "quant", "pending", CLAIM_A)])

    resolve(make_graph(log, grace_days=30))

    assert log.load_entries()[0]["milestones"][0]["status"] == "expired"


def test_expiry_boundary_is_exclusive_of_the_grace_day(log, log_path):
    write_entry(log_path, "INTC", "2026-01-05", [(days_out(-30), "quant", "pending", CLAIM_A)])

    resolve(make_graph(log, grace_days=30))

    assert log.load_entries()[0]["milestones"][0]["status"] == "pending"


# ---------------------------------------------------------------------------
# Milestones outlive the pending tag
# ---------------------------------------------------------------------------

def test_entries_with_resolved_tags_are_still_scanned(log, log_path):
    """The whole point: a milestone comes due long after the 5-day tag resolved."""
    write_entry(
        log_path,
        "INTC",
        "2026-01-05",
        [(days_out(-1), "quant", "pending", CLAIM_A)],
        tag_tail="| -6.5% | -6.1% | 5d]",
        extra="REFLECTION:\nEntry was early.",
    )
    resolve(make_graph(log, evaluator=lambda *a: "miss"))

    entry = log.load_entries()[0]
    assert entry["pending"] is False
    assert entry["milestones"][0]["status"] == "miss"
    assert entry["reflection"] == "Entry was early."


def test_other_tickers_are_untouched(log, log_path):
    write_entry(log_path, "INTC", "2026-01-05", [(days_out(-1), "quant", "pending", CLAIM_A)])
    write_entry(log_path, "BABA", "2026-01-05", [(days_out(-1), "quant", "pending", CLAIM_A)])

    resolve(make_graph(log, evaluator=lambda *a: "hit"), ticker="INTC")

    intc, baba = log.load_entries()
    assert intc["milestones"][0]["status"] == "hit"
    assert baba["milestones"][0]["status"] == "pending"


# ---------------------------------------------------------------------------
# Thesis reflection
# ---------------------------------------------------------------------------

def test_no_thesis_reflection_while_a_milestone_remains_open(log, log_path):
    write_entry(
        log_path,
        "INTC",
        "2026-01-05",
        [(days_out(-1), "quant", "pending", CLAIM_A), (days_out(60), "qual", "pending", CLAIM_B)],
    )
    graph = make_graph(log, evaluator=lambda *a: "hit")
    resolve(graph)

    graph.reflector.reflect_on_thesis.assert_not_called()
    assert log.load_entries()[0]["thesis_reflection"] == ""


def test_thesis_reflection_fires_when_the_last_milestone_closes(log, log_path):
    write_entry(
        log_path,
        "INTC",
        "2026-01-05",
        [(days_out(-1), "quant", "hit", CLAIM_A), (days_out(-1), "qual", "pending", CLAIM_B)],
    )
    graph = make_graph(log, evaluator=lambda *a: "miss")
    resolve(graph)

    graph.reflector.reflect_on_thesis.assert_called_once()
    graded = graph.reflector.reflect_on_thesis.call_args.kwargs["milestone_results"]
    assert {m["claim"]: m["status"] for m in graded} == {CLAIM_A: "hit", CLAIM_B: "miss"}
    assert log.load_entries()[0]["thesis_reflection"] == "The thesis held on revenue."


def test_resolver_is_idempotent(log, log_path):
    write_entry(
        log_path,
        "INTC",
        "2026-01-05",
        [(days_out(-1), "quant", "pending", CLAIM_A)],
    )
    graph = make_graph(log, evaluator=lambda *a: "hit")

    resolve(graph)
    after_first = log_path.read_text(encoding="utf-8")
    resolve(graph)

    assert log_path.read_text(encoding="utf-8") == after_first
    graph.reflector.reflect_on_thesis.assert_called_once()
    assert after_first.count("THESIS_REFLECTION:") == 1


# ---------------------------------------------------------------------------
# Failure containment
# ---------------------------------------------------------------------------

def test_evaluator_failure_does_not_break_the_run(log, log_path):
    def boom(entry, milestone, as_of):
        raise RuntimeError("evidence fetch failed")

    write_entry(log_path, "INTC", "2026-01-05", [(days_out(-1), "quant", "pending", CLAIM_A)])
    before = log_path.read_text(encoding="utf-8")

    resolve(make_graph(log, evaluator=boom))  # must not raise

    assert log_path.read_text(encoding="utf-8") == before
    assert log.load_entries()[0]["milestones"][0]["status"] == "pending"


def test_one_bad_entry_does_not_block_the_others(log, log_path):
    write_entry(log_path, "INTC", "2026-01-05", [(days_out(-1), "quant", "pending", CLAIM_A)])
    write_entry(log_path, "INTC", "2026-02-05", [(days_out(-1), "quant", "pending", CLAIM_B)])

    def selective(entry, milestone, as_of):
        if entry["date"] == "2026-01-05":
            raise RuntimeError("boom")
        return "hit"

    resolve(make_graph(log, evaluator=selective))

    first, second = log.load_entries()
    assert first["milestones"][0]["status"] == "pending"
    assert second["milestones"][0]["status"] == "hit"


# ---------------------------------------------------------------------------
# The stub evaluator itself
# ---------------------------------------------------------------------------

def test_stub_evaluator_returns_undetermined():
    graph = MagicMock(spec=TradingAgentsGraph)
    verdict = TradingAgentsGraph._evaluate_milestone(
        graph, {"decision": "d"}, {"claim": CLAIM_A, "due_date": "2026-01-05"}, "2026-01-05"
    )
    assert verdict is None

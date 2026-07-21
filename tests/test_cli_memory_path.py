"""The CLI must run through propagate(), not around it.

Everything that makes a run durable — resolving pending entries, injecting past
context, logging state, storing the decision, checkpoint lifecycle — lives in
``propagate``/``_run_graph``. The CLI used to drive ``graph.graph.stream``
itself, so it produced reports and no memory at all, and its ``--checkpoint``
flag set a config key nothing on that path ever read. These tests pin the
observer plumbing that let the CLI join the normal path, and guard the bypass
from coming back.
"""

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.trading_graph import TradingAgentsGraph

CLI_SOURCE = Path(__file__).resolve().parents[1] / "cli" / "main.py"

DECISION = "Rating: Hold\nMaintain exposure.\n\n**Time Horizon**: 3-6 months"

CHUNKS = [
    {"messages": [], "market_report": "tech"},
    {"messages": [], "investment_debate_state": {"judge_decision": "d"}},
    # No messages key at all — the shape that used to be dropped from the merge.
    {"final_trade_decision": DECISION},
]


def _graph(tmp_path, **overrides):
    """A TradingAgentsGraph stand-in wired for _run_graph with real memory."""
    g = MagicMock(spec=TradingAgentsGraph)
    g.debug = False
    g.config = {"checkpoint_enabled": False}
    g.memory_log = TradingMemoryLog({"memory_log_path": str(tmp_path / "m.md")})
    g.propagator = Propagator()
    g.resolve_instrument_context = MagicMock(return_value="ctx")
    g.graph = MagicMock()
    g.graph.stream = MagicMock(return_value=iter(CHUNKS))
    g.process_signal = MagicMock(return_value="Hold")
    g._log_state = MagicMock()
    for k, v in overrides.items():
        setattr(g, k, v)
    return g


class TestObserverPlumbing:
    def test_on_chunk_receives_every_chunk(self, tmp_path):
        seen = []
        g = _graph(tmp_path)
        TradingAgentsGraph._run_graph(g, "INTC", "2026-07-21", on_chunk=seen.append)
        assert seen == CHUNKS

    def test_on_chunk_streams_even_when_not_debug(self, tmp_path):
        """An observer implies streaming; invoke() would starve the live UI."""
        g = _graph(tmp_path)
        TradingAgentsGraph._run_graph(g, "INTC", "2026-07-21", on_chunk=lambda c: None)
        g.graph.stream.assert_called_once()
        g.graph.invoke.assert_not_called()

    def test_no_observer_still_invokes(self, tmp_path):
        g = _graph(tmp_path)
        g.graph.invoke = MagicMock(return_value={"final_trade_decision": DECISION})
        TradingAgentsGraph._run_graph(g, "INTC", "2026-07-21")
        g.graph.invoke.assert_called_once()
        g.graph.stream.assert_not_called()

    def test_merged_state_keeps_chunks_without_messages(self, tmp_path):
        """The final decision arrives on a chunk that carries no messages key."""
        g = _graph(tmp_path)
        final_state, _ = TradingAgentsGraph._run_graph(
            g, "INTC", "2026-07-21", on_chunk=lambda c: None
        )
        assert final_state["market_report"] == "tech"
        assert final_state["final_trade_decision"] == DECISION

    def test_debug_printing_yields_to_the_observer(self, tmp_path):
        """The CLI builds this graph with debug=True and owns the terminal.

        pretty_print() writes to stdout; the CLI renders into a Rich Live
        display. Both firing at once corrupts the frame — and the old CLI never
        reached this code, so nothing else guards it.
        """
        msg = MagicMock()
        msg.content = "hi"
        g = _graph(tmp_path, debug=True)
        g.graph.stream = MagicMock(
            return_value=iter([{"messages": [msg], "final_trade_decision": DECISION}])
        )
        TradingAgentsGraph._run_graph(
            g, "INTC", "2026-07-21", on_chunk=lambda c: None
        )
        msg.pretty_print.assert_not_called()

    def test_debug_still_prints_without_an_observer(self, tmp_path):
        """main.py relies on this; the gate must not disable debug outright."""
        msg = MagicMock()
        msg.content = "hi"
        g = _graph(tmp_path, debug=True)
        g.graph.stream = MagicMock(
            return_value=iter([{"messages": [msg], "final_trade_decision": DECISION}])
        )
        TradingAgentsGraph._run_graph(g, "INTC", "2026-07-21")
        msg.pretty_print.assert_called_once()

    def test_callbacks_reach_graph_args(self, tmp_path):
        handler = object()
        g = _graph(tmp_path)
        TradingAgentsGraph._run_graph(
            g, "INTC", "2026-07-21", on_chunk=lambda c: None, callbacks=[handler]
        )
        passed_config = g.graph.stream.call_args.kwargs["config"]
        assert passed_config["callbacks"] == [handler]


class TestObservedRunIsDurable:
    """The whole point: an observer-driven run must still write memory."""

    def test_decision_is_stored(self, tmp_path):
        g = _graph(tmp_path)
        TradingAgentsGraph._run_graph(
            g, "INTC", "2026-07-21", on_chunk=lambda c: None
        )
        pending = g.memory_log.get_pending_entries()
        assert [(e["ticker"], e["date"]) for e in pending] == [("INTC", "2026-07-21")]

    def test_past_context_is_injected(self, tmp_path):
        g = _graph(tmp_path)
        g.memory_log.store_decision("INTC", "2026-06-22", DECISION)
        g.memory_log.update_with_outcome(
            "INTC", "2026-06-22", -0.065, -0.061, 5, "Entry was extended."
        )
        TradingAgentsGraph._run_graph(
            g, "INTC", "2026-07-21", on_chunk=lambda c: None
        )
        state = g.graph.stream.call_args[0][0]
        assert "Entry was extended." in state["past_context"]

    def test_state_is_logged(self, tmp_path):
        g = _graph(tmp_path)
        TradingAgentsGraph._run_graph(
            g, "INTC", "2026-07-21", on_chunk=lambda c: None
        )
        g._log_state.assert_called_once()


class TestCheckpointReachesObservedRuns:
    def test_thread_id_injected_when_enabled(self, tmp_path):
        """--checkpoint was inert on the CLI path: no checkpointer, no thread_id."""
        g = _graph(tmp_path)
        g.config = {"checkpoint_enabled": True, "data_cache_dir": str(tmp_path)}
        g._run_signature = MagicMock(return_value="sig")
        TradingAgentsGraph._run_graph(
            g, "INTC", "2026-07-21", on_chunk=lambda c: None
        )
        configurable = g.graph.stream.call_args.kwargs["config"]["configurable"]
        assert configurable["thread_id"]


class TestBypassDoesNotReturn:
    """Structural guard — the bug was invisible to every behavioural test."""

    @staticmethod
    def _calls(tree):
        return [
            n.func for n in ast.walk(tree) if isinstance(n, ast.Call)
        ]

    @pytest.fixture(scope="class")
    def cli_tree(self):
        return ast.parse(CLI_SOURCE.read_text(encoding="utf-8"))

    def test_cli_does_not_stream_the_graph_itself(self, cli_tree):
        for func in self._calls(cli_tree):
            if isinstance(func, ast.Attribute) and func.attr == "stream":
                owner = func.value
                # graph.graph.stream(...) — the bypass.
                assert not (
                    isinstance(owner, ast.Attribute) and owner.attr == "graph"
                ), "cli/main.py must run the graph via propagate(), not stream it directly"

    def test_cli_calls_propagate(self, cli_tree):
        assert any(
            isinstance(f, ast.Attribute) and f.attr == "propagate"
            for f in self._calls(cli_tree)
        ), "cli/main.py must drive the run through graph.propagate()"

    def test_cli_does_not_build_initial_state(self, cli_tree):
        """State setup belongs to _run_graph; a second copy drifts out of sync."""
        assert not any(
            isinstance(f, ast.Attribute) and f.attr == "create_initial_state"
            for f in self._calls(cli_tree)
        )

# TradingAgents/graph/reflection.py

from typing import Any


class Reflector:
    """Handles reflection on trading decisions."""

    def __init__(self, quick_thinking_llm: Any):
        """Initialize the reflector with an LLM."""
        self.quick_thinking_llm = quick_thinking_llm
        self.log_reflection_prompt = self._get_log_reflection_prompt()
        self.thesis_reflection_prompt = self._get_thesis_reflection_prompt()

    def _get_log_reflection_prompt(self) -> str:
        """Concise prompt for reflect_on_final_decision (Phase B log entries).

        Produces 2-4 sentences of plain prose — compact enough to be re-injected
        into future agent prompts without bloating the context window.

        Deliberately scoped to the *entry*, not the thesis. The outcome window is
        a handful of trading days while the decision's stated ``time_horizon`` is
        usually months, so asking "was the directional call correct?" demands a
        verdict the data cannot support. The prompt instead asks what a few days
        genuinely show — timing and the immediate catalyst — and forbids scoring
        the multi-month thesis, which is still open.

        It also states the long-only sign convention: ``_fetch_returns`` computes
        a long return regardless of rating, so a bearish call that worked shows up
        as a negative figure and would otherwise read as a failure.
        """
        return (
            "You are a trading analyst reviewing the near-term outcome of your own past decision.\n"
            "The observation window is short — days — while the decision's stated time horizon is "
            "usually far longer. Judge only what this window can actually support.\n\n"
            "Write exactly 2-4 sentences of plain prose (no bullets, no headers, no markdown).\n\n"
            "Cover in order:\n"
            "1. Over this window, did the near-term setup confirm or contradict the entry — timing, "
            "the immediate catalyst, the technical read? Cite the alpha figure.\n"
            "2. State plainly that the longer-horizon thesis remains open and is not being scored here.\n"
            "3. One concrete lesson about entry timing or setup to apply to the next similar analysis.\n\n"
            "Sign convention: returns are measured long-only, whatever the rating. A negative return "
            "under a bearish rating (Sell, Underweight) means the near-term call was RIGHT, not wrong. "
            "Read the sign against the rating before judging.\n\n"
            "Be specific and terse. Your output will be stored verbatim in a decision log "
            "and re-read by future analysts, so every word must earn its place."
        )

    def _get_thesis_reflection_prompt(self) -> str:
        """Prompt for reflect_on_thesis — the counterpart to the entry reflection.

        Scoped in the exact opposite direction to ``_get_log_reflection_prompt``.
        That one refuses to judge the multi-month thesis because five trading
        days cannot support a verdict; this one *does* judge it, because the
        concrete claims the thesis rested on have now come due and been graded.

        The ``expired`` status carries a distinction the model gets wrong if
        unstated: a milestone that never resolved within its horizon is not the
        same as one that resolved against the thesis. The prompt says so.
        """
        return (
            "You are a trading analyst grading the investment thesis behind one of your own "
            "past decisions. Its milestones — the concrete claims the thesis depended on — "
            "have now come due and been graded.\n\n"
            "Write exactly 2-4 sentences of plain prose (no bullets, no headers, no markdown).\n\n"
            "Cover in order:\n"
            "1. Did the thesis hold? Judge it against the milestone results, naming the "
            "specific claims that carried or broke it.\n"
            "2. If it failed, say whether the reasoning was wrong or the timing was — these "
            "call for different corrections.\n"
            "3. One concrete, transferable lesson about building a thesis to apply to the "
            "next analysis.\n\n"
            "Milestone statuses: 'hit' means the claim came true; 'miss' means it did not; "
            "'partial' means it came true in part; 'expired' means it was never confirmed "
            "within its horizon — which may mean the thesis was too slow rather than wrong. "
            "Do not treat 'expired' as a miss.\n\n"
            "Be specific and terse. Your output will be stored verbatim in a decision log "
            "and re-read by future analysts, so every word must earn its place."
        )

    def reflect_on_thesis(
        self,
        final_decision: str,
        milestone_results: list[dict],
    ) -> str:
        """Reflect on the multi-month thesis once its milestones have resolved.

        ``milestone_results`` is the entry's graded milestone list — dicts with
        ``claim``, ``due_date``, ``kind`` and ``status`` as parsed from the log's
        ``MILESTONES:`` block.

        This is a second, independent reflection: ``reflect_on_final_decision``
        grades the entry over a handful of trading days, while this grades the
        thesis over the months its milestones actually took to resolve.
        """
        lines = [
            f"- [{m.get('status', 'pending')}] due {m.get('due_date', 'n/a')}: {m.get('claim', '')}"
            for m in milestone_results
        ]
        outcome = "Milestone results:\n" + ("\n".join(lines) if lines else "- (none recorded)")

        messages = [
            ("system", self.thesis_reflection_prompt),
            ("human", f"{outcome}\n\nOriginal Decision:\n{final_decision}"),
        ]
        return self.quick_thinking_llm.invoke(messages).content

    def reflect_on_final_decision(
        self,
        final_decision: str,
        raw_return: float,
        alpha_return: float,
        benchmark_name: str = "SPY",
        holding_days: int | None = None,
        rating: str | None = None,
    ) -> str:
        """Single reflection call on the final trade decision with outcome context.

        Used by Phase B deferred reflection. The final_trade_decision already
        synthesises all analyst insights, so no separate market context is needed.
        ``benchmark_name`` is the label used for the alpha line (e.g. ``"SPY"``
        for US tickers, ``"^N225"`` for ``.T`` listings); defaults to SPY for
        callers that haven't been updated to thread the benchmark through.

        ``holding_days`` and ``rating`` are what let the model scope its claims:
        without the window length it cannot tell a 1-day blip from a full week,
        and without the rating it cannot read the long-only sign convention. Both
        are optional so existing callers keep working, but omitting them means the
        model reasons about a window it can't see.
        """
        outcome_lines = []
        if rating:
            outcome_lines.append(f"Rating under review: {rating}")
        if holding_days is not None:
            unit = "trading day" if holding_days == 1 else "trading days"
            outcome_lines.append(f"Observation window: {holding_days} {unit}")
        outcome_lines.extend([
            f"Raw return: {raw_return:+.1%}",
            f"Alpha vs {benchmark_name}: {alpha_return:+.1%}",
        ])

        messages = [
            ("system", self.log_reflection_prompt),
            (
                "human",
                "\n".join(outcome_lines) + f"\n\nFinal Decision:\n{final_decision}",
            ),
        ]
        return self.quick_thinking_llm.invoke(messages).content

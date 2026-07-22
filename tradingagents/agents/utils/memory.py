"""Append-only markdown decision log for TradingAgents."""

import re
from pathlib import Path

from tradingagents.agents.utils.milestones import (
    extract_milestones,
    format_milestone_line,
    has_pending,
    parse_milestone_line,
    render_milestone_lines,
)
from tradingagents.agents.utils.rating import parse_rating

# Entry section headers, in the order they appear in an entry body.
_SECTIONS = ("DECISION", "MILESTONES", "REFLECTION", "THESIS_REFLECTION")


def _section_pattern(name: str) -> re.Pattern:
    """Build the capture pattern for one entry section.

    Two properties matter, and both are bugs when missed:

    - A section must stop at *every other* header. Otherwise one section
      swallows another and, for ``DECISION``, the swallowed text leaks into
      ``entry["decision"]`` — which feeds ``get_past_context`` and the
      reflection prompts. Stopping at every other header rather than only at
      later ones also makes parsing independent of the order sections were
      appended in, so a writer that appends out of order cannot corrupt reads.
    - The header is anchored to a line start, so ``REFLECTION:`` cannot match
      inside ``THESIS_REFLECTION:``.

    Generating the family from ``_SECTIONS`` means adding a section can never
    leave an older pattern's stop-list stale.
    """
    stops = "".join(rf"\n{h}:|" for h in _SECTIONS if h != name)
    return re.compile(rf"(?:^|\n){name}:\n(.*?)(?={stops}\Z)", re.DOTALL)


class TradingMemoryLog:
    """Append-only markdown log of trading decisions and reflections."""

    # HTML comment: cannot appear in LLM prose output, safe as a hard delimiter
    _SEPARATOR = "\n\n<!-- ENTRY_END -->\n\n"
    # Precompiled patterns — avoids re-compilation on every load_entries() call
    _DECISION_RE = _section_pattern("DECISION")
    _MILESTONES_RE = _section_pattern("MILESTONES")
    _REFLECTION_RE = _section_pattern("REFLECTION")
    _THESIS_REFLECTION_RE = _section_pattern("THESIS_REFLECTION")
    _HEADER_LINES = frozenset(f"{s}:" for s in _SECTIONS)

    def __init__(self, config: dict = None):
        cfg = config or {}
        self._log_path = None
        path = cfg.get("memory_log_path")
        if path:
            self._log_path = Path(path).expanduser()
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
        # Optional cap on resolved entries. None disables rotation.
        self._max_entries = cfg.get("memory_log_max_entries")

    # --- Write path (Phase A) ---

    def store_decision(
        self,
        ticker: str,
        trade_date: str,
        final_trade_decision: str,
    ) -> None:
        """Append pending entry at end of propagate(). No LLM call."""
        if not self._log_path:
            return
        # Idempotency guard: fast raw-text scan instead of full parse
        if self._log_path.exists():
            raw = self._log_path.read_text(encoding="utf-8")
            for line in raw.splitlines():
                if line.startswith(f"[{trade_date} | {ticker} |") and line.endswith("| pending]"):
                    return
        rating = parse_rating(final_trade_decision)
        tag = f"[{trade_date} | {ticker} | {rating} | pending]"
        entry = f"{tag}\n\nDECISION:\n{final_trade_decision}"

        # Milestones are stored twice on purpose: the copy inside DECISION is a
        # frozen audit record of what the PM claimed, while this extracted block
        # is the mutable tracker the resolver rewrites. The section is omitted
        # entirely when there are none, so entries without milestones stay
        # byte-identical to the pre-milestone format.
        milestones = extract_milestones(final_trade_decision)
        if milestones:
            block = "\n".join(render_milestone_lines(milestones))
            entry += f"\n\nMILESTONES:\n{block}"

        with open(self._log_path, "a", encoding="utf-8") as f:
            f.write(entry + self._SEPARATOR)

    # --- Read path (Phase A) ---

    def load_entries(self) -> list[dict]:
        """Parse all entries from log. Returns list of dicts."""
        if not self._log_path or not self._log_path.exists():
            return []
        text = self._log_path.read_text(encoding="utf-8")
        raw_entries = [e.strip() for e in text.split(self._SEPARATOR) if e.strip()]
        entries = []
        for raw in raw_entries:
            parsed = self._parse_entry(raw)
            if parsed:
                entries.append(parsed)
        return entries

    def get_pending_entries(self) -> list[dict]:
        """Return entries with outcome:pending (for Phase B)."""
        return [e for e in self.load_entries() if e.get("pending")]

    def get_past_context(self, ticker: str, n_same: int = 5, n_cross: int = 3) -> str:
        """Return formatted past context string for agent prompt injection."""
        entries = [e for e in self.load_entries() if not e.get("pending")]
        if not entries:
            return ""

        same, cross = [], []
        for e in reversed(entries):
            if len(same) >= n_same and len(cross) >= n_cross:
                break
            if e["ticker"] == ticker and len(same) < n_same:
                same.append(e)
            elif e["ticker"] != ticker and len(cross) < n_cross:
                cross.append(e)

        if not same and not cross:
            return ""

        parts = []
        if same:
            parts.append(f"Past analyses of {ticker} (most recent first):")
            parts.extend(self._format_full(e) for e in same)
        if cross:
            parts.append("Recent cross-ticker lessons:")
            parts.extend(self._format_reflection_only(e) for e in cross)
        return "\n\n".join(parts)

    # --- Update path (Phase B) ---

    def update_with_outcome(
        self,
        ticker: str,
        trade_date: str,
        raw_return: float,
        alpha_return: float,
        holding_days: int,
        reflection: str,
    ) -> None:
        """Replace pending tag and append REFLECTION section using atomic write.

        Finds the first pending entry matching (trade_date, ticker), updates
        its tag with return figures, and appends a REFLECTION section.  Uses
        a temp-file + os.replace() so a crash mid-write never corrupts the log.
        """
        if not self._log_path or not self._log_path.exists():
            return

        text = self._log_path.read_text(encoding="utf-8")
        blocks = text.split(self._SEPARATOR)

        pending_prefix = f"[{trade_date} | {ticker} |"
        raw_pct = f"{raw_return:+.1%}"
        alpha_pct = f"{alpha_return:+.1%}"

        updated = False
        new_blocks = []
        for block in blocks:
            stripped = block.strip()
            if not stripped:
                new_blocks.append(block)
                continue

            lines = stripped.splitlines()
            tag_line = lines[0].strip()

            if (
                not updated
                and tag_line.startswith(pending_prefix)
                and tag_line.endswith("| pending]")
            ):
                # Parse rating from the existing pending tag
                fields = [f.strip() for f in tag_line[1:-1].split("|")]
                rating = fields[2]
                new_tag = (
                    f"[{trade_date} | {ticker} | {rating}"
                    f" | {raw_pct} | {alpha_pct} | {holding_days}d]"
                )
                rest = "\n".join(lines[1:])
                new_blocks.append(
                    f"{new_tag}\n\n{rest.lstrip()}\n\nREFLECTION:\n{reflection}"
                )
                updated = True
            else:
                new_blocks.append(block)

        if not updated:
            return

        new_blocks = self._apply_rotation(new_blocks)
        new_text = self._SEPARATOR.join(new_blocks)
        tmp_path = self._log_path.with_suffix(".tmp")
        tmp_path.write_text(new_text, encoding="utf-8")
        tmp_path.replace(self._log_path)

    def batch_update_with_outcomes(self, updates: list[dict]) -> None:
        """Apply multiple outcome updates in a single read + atomic write.

        Each element of updates must have keys: ticker, trade_date,
        raw_return, alpha_return, holding_days, reflection.
        """
        if not self._log_path or not self._log_path.exists() or not updates:
            return

        text = self._log_path.read_text(encoding="utf-8")
        blocks = text.split(self._SEPARATOR)

        # Build lookup keyed by (trade_date, ticker) for O(1) dispatch
        update_map = {(u["trade_date"], u["ticker"]): u for u in updates}

        new_blocks = []
        for block in blocks:
            stripped = block.strip()
            if not stripped:
                new_blocks.append(block)
                continue

            lines = stripped.splitlines()
            tag_line = lines[0].strip()

            matched = False
            for (trade_date, ticker), upd in list(update_map.items()):
                pending_prefix = f"[{trade_date} | {ticker} |"
                if tag_line.startswith(pending_prefix) and tag_line.endswith("| pending]"):
                    fields = [f.strip() for f in tag_line[1:-1].split("|")]
                    rating = fields[2]
                    raw_pct = f"{upd['raw_return']:+.1%}"
                    alpha_pct = f"{upd['alpha_return']:+.1%}"
                    new_tag = (
                        f"[{trade_date} | {ticker} | {rating}"
                        f" | {raw_pct} | {alpha_pct} | {upd['holding_days']}d]"
                    )
                    rest = "\n".join(lines[1:])
                    new_blocks.append(
                        f"{new_tag}\n\n{rest.lstrip()}\n\nREFLECTION:\n{upd['reflection']}"
                    )
                    del update_map[(trade_date, ticker)]
                    matched = True
                    break

            if not matched:
                new_blocks.append(block)

        new_blocks = self._apply_rotation(new_blocks)
        new_text = self._SEPARATOR.join(new_blocks)
        tmp_path = self._log_path.with_suffix(".tmp")
        tmp_path.write_text(new_text, encoding="utf-8")
        tmp_path.replace(self._log_path)

    def update_milestone_statuses(
        self,
        ticker: str,
        trade_date: str,
        status_by_claim: dict[str, str],
        thesis_reflection: str | None = None,
    ) -> bool:
        """Grade milestones on an existing entry; optionally append its thesis reflection.

        Milestones come due long after the 5-day tag has resolved, so this
        deliberately matches on ``(trade_date, ticker)`` alone rather than on a
        pending tag. Only the ``MILESTONES:`` tracker is rewritten — the copy
        inside ``DECISION`` is a frozen audit record and is never touched.

        Idempotent: re-applying statuses that are already in place changes
        nothing and leaves the file untouched, and ``THESIS_REFLECTION`` is only
        appended when the entry does not already have one. Returns whether the
        log was rewritten.
        """
        if not self._log_path or not self._log_path.exists():
            return False
        if not status_by_claim and not thesis_reflection:
            return False

        text = self._log_path.read_text(encoding="utf-8")
        blocks = text.split(self._SEPARATOR)
        prefix = f"[{trade_date} | {ticker} |"

        changed = False
        new_blocks = []
        for block in blocks:
            stripped = block.strip()
            if not stripped or not stripped.splitlines()[0].strip().startswith(prefix):
                new_blocks.append(block)
                continue
            rewritten, block_changed = self._rewrite_milestones(
                stripped, status_by_claim, thesis_reflection
            )
            changed = changed or block_changed
            new_blocks.append(rewritten)

        if not changed:
            return False

        new_blocks = self._apply_rotation(new_blocks)
        new_text = self._SEPARATOR.join(new_blocks)
        tmp_path = self._log_path.with_suffix(".tmp")
        tmp_path.write_text(new_text, encoding="utf-8")
        tmp_path.replace(self._log_path)
        return True

    # --- Helpers ---

    def _rewrite_milestones(
        self,
        block: str,
        status_by_claim: dict[str, str],
        thesis_reflection: str | None,
    ) -> tuple[str, bool]:
        """Return (rewritten block, changed?) for one entry."""
        out: list[str] = []
        in_milestones = False
        changed = False

        for line in block.splitlines():
            bare = line.strip()
            if bare in self._HEADER_LINES:
                in_milestones = bare == "MILESTONES:"
                out.append(line)
                continue
            if in_milestones and (parsed := parse_milestone_line(bare)):
                new_status = status_by_claim.get(parsed["claim"])
                if new_status and new_status != parsed["status"]:
                    out.append(
                        format_milestone_line(
                            parsed["claim"],
                            parsed["due_date"],
                            parsed["kind"],
                            new_status,
                        )
                    )
                    changed = True
                    continue
            out.append(line)

        rebuilt = "\n".join(out)
        if thesis_reflection and "\nTHESIS_REFLECTION:" not in f"\n{rebuilt}":
            rebuilt += f"\n\nTHESIS_REFLECTION:\n{thesis_reflection.strip()}"
            changed = True
        return rebuilt, changed

    def _block_has_pending_milestone(self, block: str) -> bool:
        """True when the block still carries an ungraded milestone.

        Milestones outlive the 5-day tag: they come due months after the entry
        resolves. Without this check, rotation would evict a resolved entry
        whose thesis has never been graded.
        """
        match = self._MILESTONES_RE.search(block)
        if not match:
            return False
        return has_pending(
            [
                parsed
                for line in match.group(1).splitlines()
                if (parsed := parse_milestone_line(line))
            ]
        )

    def _apply_rotation(self, blocks: list[str]) -> list[str]:
        """Drop oldest resolved blocks when their count exceeds max_entries.

        Pending blocks are always kept (they represent unprocessed work), as are
        blocks with a still-pending milestone (unfinished thesis grading).
        Returns ``blocks`` unchanged when rotation is disabled or under cap.
        """
        if not self._max_entries or self._max_entries <= 0:
            return blocks

        # Tag each block with (kept, is_resolved) by parsing tag-line markers.
        decisions = []
        for block in blocks:
            stripped = block.strip()
            if not stripped:
                decisions.append((block, False))
                continue
            tag_line = stripped.splitlines()[0].strip()
            is_resolved = (
                tag_line.startswith("[")
                and tag_line.endswith("]")
                and not tag_line.endswith("| pending]")
                and not self._block_has_pending_milestone(block)
            )
            decisions.append((block, is_resolved))

        resolved_count = sum(1 for _, r in decisions if r)
        if resolved_count <= self._max_entries:
            return blocks

        to_drop = resolved_count - self._max_entries
        kept: list[str] = []
        for block, is_resolved in decisions:
            if is_resolved and to_drop > 0:
                to_drop -= 1
                continue
            kept.append(block)
        return kept

    def _parse_entry(self, raw: str) -> dict | None:
        lines = raw.strip().splitlines()
        if not lines:
            return None
        tag_line = lines[0].strip()
        if not (tag_line.startswith("[") and tag_line.endswith("]")):
            return None
        fields = [f.strip() for f in tag_line[1:-1].split("|")]
        if len(fields) < 4:
            return None
        entry = {
            "date": fields[0],
            "ticker": fields[1],
            "rating": fields[2],
            "pending": fields[3] == "pending",
            "raw": fields[3] if fields[3] != "pending" else None,
            "alpha": fields[4] if len(fields) > 4 else None,
            "holding": fields[5] if len(fields) > 5 else None,
        }
        body = "\n".join(lines[1:]).strip()

        def section(pattern) -> str:
            match = pattern.search(body)
            return match.group(1).strip() if match else ""

        entry["decision"] = section(self._DECISION_RE)
        entry["reflection"] = section(self._REFLECTION_RE)
        entry["thesis_reflection"] = section(self._THESIS_REFLECTION_RE)
        entry["milestones"] = [
            parsed
            for line in section(self._MILESTONES_RE).splitlines()
            if (parsed := parse_milestone_line(line))
        ]
        return entry

    def _format_full(self, e: dict) -> str:
        raw = e["raw"] or "n/a"
        alpha = e["alpha"] or "n/a"
        holding = e["holding"] or "n/a"
        tag = f"[{e['date']} | {e['ticker']} | {e['rating']} | {raw} | {alpha} | {holding}]"
        parts = [tag, f"DECISION:\n{e['decision']}"]
        if e["reflection"]:
            parts.append(f"REFLECTION:\n{e['reflection']}")
        if e.get("thesis_reflection"):
            parts.append(f"THESIS_REFLECTION:\n{e['thesis_reflection']}")
        return "\n\n".join(parts)

    def _format_reflection_only(self, e: dict) -> str:
        tag = f"[{e['date']} | {e['ticker']} | {e['rating']} | {e['raw'] or 'n/a'}]"
        # A graded thesis is the more durable lesson: it says whether the
        # multi-month call was right, where the 5-day reflection can only speak
        # to entry timing. Prefer it when the thesis has actually been graded.
        lesson = e.get("thesis_reflection") or e["reflection"]
        if lesson:
            return f"{tag}\n{lesson}"
        text = e["decision"][:300]
        suffix = "..." if len(e["decision"]) > 300 else ""
        return f"{tag}\n{text}{suffix}"

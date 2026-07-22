"""Canonical grammar for thesis milestones.

Single source of truth shared by the producer (the Portfolio Manager's
rendered decision, see ``agents/schemas.py``) and the consumer (the memory
log, see ``agents/utils/memory.py``).  Both sides format and parse through
this module, so the on-disk form cannot drift between them.

Line format::

    - [{due_date} | {kind} | {status}] {claim}
    - [2026-10-24 | quant | pending] Foundry revenue turns YoY-positive in Q3

The claim sits *outside* the bracket deliberately.  Only the three bracketed
fields are ``|``-split, so a claim containing ``|`` or non-ASCII text round
trips unharmed.  Newlines inside a claim are collapsed at format time, which
keeps every milestone on exactly one line — the property the memory log's
line-oriented rewriter depends on.
"""

from __future__ import annotations

from datetime import datetime

# Section header the Portfolio Manager's rendered decision uses for its
# audit copy of the milestones. Deliberately distinct from the memory log's
# bare ``MILESTONES:`` header so the log's section regexes cannot match it.
MILESTONE_SECTION_HEADER = "**Milestones**:"

MILESTONE_KINDS = ("quant", "qual")

#: ``pending`` is the only open status; the rest are terminal.
MILESTONE_STATUSES = ("pending", "hit", "miss", "partial", "expired")

_DATE_FORMAT = "%Y-%m-%d"
_LINE_PREFIX = "- ["


def _normalize_claim(claim: str) -> str:
    """Collapse all whitespace runs so the claim survives as a single line."""
    return " ".join(str(claim).split())


def format_milestone_line(
    claim: str,
    due_date: str,
    kind: str,
    status: str = "pending",
) -> str:
    """Render one milestone to its canonical single-line form.

    Raises ``ValueError`` on an unknown ``kind``/``status`` or a ``due_date``
    that is not ``YYYY-MM-DD``: a malformed line written into the log would
    silently drop out of the resolver's due-scan, so it fails at write time
    instead.
    """
    if kind not in MILESTONE_KINDS:
        raise ValueError(f"kind must be one of {MILESTONE_KINDS}, got {kind!r}")
    if status not in MILESTONE_STATUSES:
        raise ValueError(f"status must be one of {MILESTONE_STATUSES}, got {status!r}")
    try:
        datetime.strptime(due_date, _DATE_FORMAT)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"due_date must be YYYY-MM-DD, got {due_date!r}") from exc

    normalized = _normalize_claim(claim)
    if not normalized:
        raise ValueError("claim must not be empty")
    return f"{_LINE_PREFIX}{due_date} | {kind} | {status}] {normalized}"


def parse_milestone_line(line: str) -> dict | None:
    """Parse a canonical milestone line, or return ``None`` if it isn't one.

    Returning ``None`` rather than raising lets callers scan mixed prose and
    keep only the lines that are genuinely milestones.
    """
    if not line:
        return None
    stripped = line.strip()
    if not stripped.startswith(_LINE_PREFIX):
        return None

    # The claim may contain "]", but none of the three bracketed fields can,
    # so the first "]" always closes the bracket.
    close = stripped.find("]")
    if close == -1:
        return None

    fields = [f.strip() for f in stripped[len(_LINE_PREFIX) : close].split("|")]
    if len(fields) != 3:
        return None
    due_date, kind, status = fields

    if kind not in MILESTONE_KINDS or status not in MILESTONE_STATUSES:
        return None
    try:
        datetime.strptime(due_date, _DATE_FORMAT)
    except ValueError:
        return None

    claim = _normalize_claim(stripped[close + 1 :])
    if not claim:
        return None
    return {"claim": claim, "due_date": due_date, "kind": kind, "status": status}


def render_milestone_lines(milestones: list[dict]) -> list[str]:
    """Re-render parsed milestone dicts back to canonical lines."""
    return [
        format_milestone_line(
            m["claim"], m["due_date"], m["kind"], m.get("status", "pending")
        )
        for m in milestones
    ]


def extract_milestones(text: str) -> list[dict]:
    """Pull milestones out of a rendered Portfolio Manager decision.

    Scoped to the ``**Milestones**:`` section rather than scanning the whole
    document: the investment thesis is free prose and may well contain its own
    bracketed bullets, which must not be mistaken for milestones.
    """
    if not text or MILESTONE_SECTION_HEADER not in text:
        return []

    milestones: list[dict] = []
    in_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if not in_section:
            if stripped.startswith(MILESTONE_SECTION_HEADER):
                in_section = True
            continue
        if not stripped:
            continue
        # A new bold header ends the section.
        if stripped.startswith("**"):
            break
        parsed = parse_milestone_line(stripped)
        if parsed:
            milestones.append(parsed)
    return milestones


def has_pending(milestones: list[dict]) -> bool:
    """True when at least one milestone is still open."""
    return any(m.get("status") == "pending" for m in milestones)


def is_due(milestone: dict, as_of: str) -> bool:
    """True when ``milestone`` is pending and its due date has arrived."""
    if milestone.get("status") != "pending":
        return False
    return milestone.get("due_date", "") <= as_of

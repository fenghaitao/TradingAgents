"""Tests for the milestone grammar and its capture in the Portfolio Manager schema."""

import pytest

from tradingagents.agents.schemas import (
    Milestone,
    MilestoneKind,
    PortfolioDecision,
    PortfolioRating,
    render_pm_decision,
)
from tradingagents.agents.utils.milestones import (
    MILESTONE_SECTION_HEADER,
    extract_milestones,
    format_milestone_line,
    has_pending,
    is_due,
    parse_milestone_line,
)
from tradingagents.agents.utils.rating import parse_rating
from tradingagents.graph.signal_processing import SignalProcessor

# ---------------------------------------------------------------------------
# Grammar round-trip
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "claim",
    [
        "Foundry revenue turns YoY-positive in the Q3 report",
        "Gross margin | ex-charges | exceeds 40%",          # pipes in the claim
        "台积电 3nm 产能扩张按期完成",                          # non-ASCII
        "Ships 18A silicon; no further delay announced",
    ],
)
def test_format_parse_round_trip(claim):
    line = format_milestone_line(claim, "2026-10-24", "quant", "pending")
    parsed = parse_milestone_line(line)
    assert parsed == {
        "claim": claim,
        "due_date": "2026-10-24",
        "kind": "quant",
        "status": "pending",
    }


def test_claim_newlines_collapsed_to_one_line():
    line = format_milestone_line("multi\nline\n\nclaim", "2026-01-05", "qual")
    assert "\n" not in line
    assert parse_milestone_line(line)["claim"] == "multi line claim"


def test_claim_containing_bracket_survives():
    # The first "]" closes the bracket; none of the three fields can contain one.
    line = format_milestone_line("EPS [non-GAAP] beats $2.10", "2026-02-01", "quant")
    assert parse_milestone_line(line)["claim"] == "EPS [non-GAAP] beats $2.10"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"due_date": "2026-13-01", "kind": "quant"},   # impossible month
        {"due_date": "Q3 2026", "kind": "quant"},      # not a date
        {"due_date": "2026-10-24", "kind": "vibes"},   # unknown kind
    ],
)
def test_format_rejects_malformed_fields(kwargs):
    with pytest.raises(ValueError):
        format_milestone_line("some claim", **kwargs)


def test_format_rejects_unknown_status():
    with pytest.raises(ValueError):
        format_milestone_line("c", "2026-10-24", "quant", status="maybe")


@pytest.mark.parametrize(
    "line",
    [
        "",
        "just prose",
        "- not a milestone",
        "- [2026-10-24 | quant] missing the status field",
        "- [2026-10-24 | quant | pending missing bracket",
        "- [2026-10-24 | quant | pending]",            # empty claim
        "- [2026-99-99 | quant | pending] bad date",
        "- [2026-10-24 | quant | wat] bad status",
    ],
)
def test_parse_rejects_non_milestone_lines(line):
    assert parse_milestone_line(line) is None


# ---------------------------------------------------------------------------
# Section extraction
# ---------------------------------------------------------------------------

def test_extract_is_scoped_to_the_milestone_section():
    """Bracketed bullets in the thesis prose must not be read as milestones."""
    text = "\n".join([
        "**Rating**: Buy",
        "",
        "**Investment Thesis**: Key risks are",
        "- [not a milestone] because it lives in the prose",
        "- [2030-01-01 | quant | pending] decoy line outside the section",
        "",
        MILESTONE_SECTION_HEADER,
        "- [2026-10-24 | quant | pending] Foundry revenue turns YoY-positive",
        "- [2026-12-31 | qual | pending] 18A ships to an external customer",
    ])
    claims = [m["claim"] for m in extract_milestones(text)]
    assert claims == [
        "Foundry revenue turns YoY-positive",
        "18A ships to an external customer",
    ]


def test_extract_stops_at_the_next_bold_header():
    text = "\n".join([
        MILESTONE_SECTION_HEADER,
        "- [2026-10-24 | quant | pending] kept",
        "",
        "**Time Horizon**: 6 months",
        "- [2027-01-01 | quant | pending] not kept",
    ])
    assert [m["claim"] for m in extract_milestones(text)] == ["kept"]


def test_extract_returns_empty_without_a_section():
    assert extract_milestones("**Rating**: Hold\n\nno milestones here") == []
    assert extract_milestones("") == []


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------

def test_has_pending_and_is_due():
    pending = {"claim": "c", "due_date": "2026-10-24", "kind": "quant", "status": "pending"}
    hit = {**pending, "status": "hit"}

    assert has_pending([hit, pending])
    assert not has_pending([hit])
    assert not has_pending([])

    assert is_due(pending, "2026-10-24")     # due today
    assert is_due(pending, "2026-11-01")     # overdue
    assert not is_due(pending, "2026-10-23")  # not yet
    assert not is_due(hit, "2026-11-01")      # already closed


# ---------------------------------------------------------------------------
# Capture in the Portfolio Manager schema
# ---------------------------------------------------------------------------

def _decision(**overrides):
    base = {
        "rating": PortfolioRating.BUY,
        "executive_summary": "Enter on weakness.",
        "investment_thesis": "Foundry turnaround is underpriced.",
    }
    return PortfolioDecision(**{**base, **overrides})


def test_render_emits_canonical_milestone_lines():
    decision = _decision(
        milestones=[
            Milestone(
                claim="Foundry revenue turns YoY-positive",
                due_date="2026-10-24",
                kind=MilestoneKind.QUANT,
            ),
            Milestone(
                claim="18A ships to an external customer",
                due_date="2026-12-31",
                kind=MilestoneKind.QUAL,
            ),
        ]
    )
    rendered = render_pm_decision(decision)
    assert MILESTONE_SECTION_HEADER in rendered
    assert "- [2026-10-24 | quant | pending] Foundry revenue turns YoY-positive" in rendered
    assert "- [2026-12-31 | qual | pending] 18A ships to an external customer" in rendered
    # Round-trips through the consumer side unchanged.
    assert [m["claim"] for m in extract_milestones(rendered)] == [
        "Foundry revenue turns YoY-positive",
        "18A ships to an external customer",
    ]


def test_milestones_are_optional_and_absent_from_render():
    """A decision without milestones renders byte-identically to the old format."""
    rendered = render_pm_decision(_decision())
    assert rendered == (
        "**Rating**: Buy\n"
        "\n"
        "**Executive Summary**: Enter on weakness.\n"
        "\n"
        "**Investment Thesis**: Foundry turnaround is underpriced."
    )
    assert "Milestones" not in rendered


def test_malformed_milestone_is_dropped_not_fatal():
    """A bad due_date costs one milestone, never the whole decision."""
    decision = _decision(
        milestones=[
            {"claim": "good one", "due_date": "2026-10-24", "kind": "quant"},
            {"claim": "vague", "due_date": "Q3 2026", "kind": "quant"},
            {"claim": "  ", "due_date": "2026-10-24", "kind": "qual"},
            {"claim": "bad kind", "due_date": "2026-10-24", "kind": "hunch"},
        ]
    )
    assert decision.rating == PortfolioRating.BUY
    assert [m.claim for m in decision.milestones] == ["good one"]


def test_milestone_claim_whitespace_is_normalized():
    m = Milestone(claim="  spaced   out\nclaim ", due_date="2026-10-24", kind="quant")
    assert m.claim == "spaced out claim"


# ---------------------------------------------------------------------------
# Price claims are steered out of the schema, not filtered after the fact
# ---------------------------------------------------------------------------

def test_schema_steers_the_model_off_price_level_claims():
    """A live PM run emitted "reclaims the 117-118 zone" tagged ``quant``.

    That is the drift milestones exist to prevent — price over the horizon is
    already graded by the 5-day window — and it is also a routing hazard: the
    evaluator keys off ``kind``, so a price claim tagged ``quant`` gets sent to
    a fundamentals lookup that cannot answer it. Field descriptions *are* the
    model's instructions, so the exclusion has to survive into the generated
    JSON schema, which is what the provider actually sees.
    """
    schema = PortfolioDecision.model_json_schema()
    milestone_props = schema["$defs"]["Milestone"]["properties"]

    for text in (
        schema["properties"]["milestones"]["description"],
        milestone_props["claim"]["description"],
        milestone_props["kind"]["description"],
    ):
        assert "price" in text.lower()

    # The list-level description is what frames the whole section, so it must
    # also rule out restating entry timing as a milestone.
    assert "timing" in schema["properties"]["milestones"]["description"].lower()
    # 'quant' must read as a company-reported figure, not any number at all.
    assert "reports" in milestone_props["kind"]["description"].lower()


# ---------------------------------------------------------------------------
# Rating extraction must survive milestone claims
# ---------------------------------------------------------------------------

def test_rating_extraction_ignores_keywords_inside_milestone_claims():
    """Milestone capture is ungated, so every decision now carries free-text claims.

    Those claims routinely contain rating words ("Sell-side desks flip to Buy",
    "management holds guidance"). Both the memory-log tag and the user-facing
    signal come from ``parse_rating`` over this same markdown, so a body scan
    that reached the claims would silently invert the reported call.
    """
    decision = _decision(
        rating=PortfolioRating.SELL,
        executive_summary="Exit into strength; the setup has broken down.",
        investment_thesis="Foundry economics do not clear the cost of capital.",
        milestones=[
            Milestone(
                claim="Sell-side desks flip to Buy on the Q3 print",
                due_date="2026-11-19",
                kind="qual",
            ),
            Milestone(
                claim="Management holds full-year guidance at the analyst day",
                due_date="2026-12-04",
                kind="qual",
            ),
        ],
    )
    rendered = render_pm_decision(decision)

    # Both consumers of the rendered decision read the rating the same way.
    assert parse_rating(rendered) == "Sell"
    assert SignalProcessor().process_signal(rendered) == "Sell"

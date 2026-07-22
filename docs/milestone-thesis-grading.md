# Milestone-Based Thesis Grading — Design

Status: **implemented** (capture + resolution scaffold; evaluator stubbed) · see §13 for
what shipped and what is still open.

## 1. Problem

The reflection system today grades only the **entry/timing** of a decision over a
5-trading-day window (`_fetch_returns`, `holding_days=5`), and the entry reflection
prompt *explicitly refuses* to score the multi-month thesis ("the longer-horizon
thesis remains open and is not being scored here").

That refusal is honest — 5 days cannot judge a 3–18 month call — but it leaves a
gap: **the thesis itself is never graded at all.** A Portfolio Manager can be right
on timing and wrong on thesis (or vice versa), and we only ever capture the first.

Milestone-based grading closes the gap by scoring the thesis **not on a calendar
window, but on whether the specific things it predicted actually happened** — each
milestone graded when it comes due.

## 2. Two directions of "window" (why this is separate from the 5d path)

| | Entry reflection (exists) | Thesis grading (this design) |
|---|---|---|
| Trigger | 5 trading days elapse | a milestone's `due_date` is reached |
| Grades | entry timing / immediate catalyst | the multi-month thesis claims |
| Resolves | days after the decision | months after, on later same-ticker runs |
| Lives on | the `pending → resolved` tag | milestones, which **outlive** that tag |

Key consequence: milestones resolve on entries whose 5d tag is **already resolved**.
Milestone tracking is therefore **orthogonal** to the `pending`/resolved tag — the
resolver must scan *all* same-ticker entries that still have a pending milestone,
not just `pending`-tagged ones.

## 3. The fork we chose: A (structured PM field)

Milestones are captured as a **structured field the Portfolio Manager emits**, not
LLM-extracted from prose after the fact.

- **A (chosen):** PM emits milestones as part of its `PortfolioDecision` structured
  output. We grade exactly what the PM claimed. Provenance is trustworthy.
- **B (rejected):** a separate extractor LLM infers milestones from the decision
  text. Cheaper on the PM prompt, but can invent milestones the thesis never made or
  miss the real one — we'd be grading a paraphrase.

The A-vs-B choice was only ever about **capture**. Both forks share the same
evaluator.

## 4. Phasing — capture now, evaluator later (and why)

We ship **capture + the resolution scaffold** first. We do **not** build the
evaluator's evidence-fetch guts yet, for a concrete reason:

> Every milestone the PM writes today is due in the *future* (Q3, FY-end). The
> resolver fires only when `due_date <= today`, so it cannot run against real data
> for months, and there is **no backfill** — existing log entries predate the
> feature. The evaluator is therefore untestable against reality right now; only
> mock-testable. This is the same "deferred until the window arrives" situation as
> the 5d path.

So the evaluator (`_evaluate_milestone`) is a **stub that returns `None`
(undetermined)**. Until it is wired, the resolver never fabricates a verdict — it
only **expires** a milestone that is long past due. The whole resolver is gated
behind a config flag (default off), so the stub can never silently expire real
theses in production.

What we validate in this PR is the **capture half**: run the PM once on a real
ticker and look at the milestones it actually emits.

## 5. Data model

### 5.1 Canonical milestone-line grammar (single source of truth)

New module `tradingagents/agents/utils/milestones.py` owns the grammar. Both the PM
renderer (producer) and the memory layer (consumer) use it — one format, round-trip
tested.

```
- [{due_date} | {kind} | {status}] {claim}
```

- `due_date` — `YYYY-MM-DD`, the earliest date the milestone can be evaluated.
- `kind` — `quant` (verifiable against a numeric fundamentals figure) or `qual`.
- `status` — `pending | hit | miss | partial | expired`.
- `claim` — free text, **outside** the bracket. A `|` or non-ASCII in the claim is
  safe because only the 3 bracketed fields are `|`-split; the claim is everything
  after `] `. Newlines in the claim are collapsed to spaces at format time.

Example:
```
- [2026-10-24 | quant | pending] Foundry revenue turns YoY-positive in the Q3 report
```

### 5.2 Memory entry layout (fixed section order)

```
[2026-07-21 | INTC | Overweight | pending]      ← tag (5d lifecycle)

DECISION:
<full rendered PM markdown, VERBATIM — includes its own **Milestones** section>

MILESTONES:                                      ← mutable tracker (extracted copy)
- [2026-10-24 | quant | pending] ...
- [2026-12-31 | quant | pending] ...

REFLECTION:                                      ← 5d entry reflection (appended later)
<2-4 sentences>

THESIS_REFLECTION:                               ← appended when all milestones close
<2-4 sentences>

<!-- ENTRY_END -->
```

Two intentional design points:

- **`DECISION` stays frozen.** Milestones appear twice: once inside the verbatim
  `DECISION` (audit snapshot) and once in the separate `MILESTONES:` block (the live
  tracker). Only the `MILESTONES:` block is ever mutated. The audit record never
  changes.
- **Fixed section order** so every section regex can stop at *all* later headers.

### 5.3 Parser fix (a real bug to avoid, not a style nit)

The existing `_DECISION_RE` runs to `(?=\nREFLECTION:|\Z)` and `_REFLECTION_RE` runs
to `$`. Dropping a `MILESTONES:` / `THESIS_REFLECTION:` section between them makes
one regex **swallow** the other section, leaking milestone lines into
`entry["decision"]` — which feeds `past_context` and the 5d reflection. Every
section regex must be rewritten to stop at every later header, and the
`REFLECTION` / `THESIS_REFLECTION` patterns must be anchored (`(?:^|\n)`) so
`REFLECTION:` does not match inside `THESIS_REFLECTION:`. Guarded by a
round-trip store→parse unit test with milestones present.

### 5.4 Rotation fix

`_apply_rotation` classifies an entry with a resolved 5d tag as droppable and can
evict it while its milestones are still `pending`. Add "has a pending milestone" as
a keep condition — such an entry is never dropped, regardless of `max_entries`.

## 6. Schema changes (`schemas.py`)

```python
class MilestoneKind(str, Enum):
    QUANT = "quant"
    QUAL  = "qual"

class Milestone(BaseModel):
    claim: str          # a single checkable prediction the thesis depends on
    due_date: str       # YYYY-MM-DD, earliest evaluation date
    kind: MilestoneKind # quant if checkable against a numeric figure, else qual

class PortfolioDecision(BaseModel):
    ...
    milestones: list[Milestone] = Field(default_factory=list)  # 2–4 checkable predictions
```

`render_pm_decision` appends a `**Milestones**:` section emitting each milestone via
`format_milestone_line(...)` with `status="pending"`. Field descriptions double as
the model's output instructions (existing convention).

## 7. Memory API (`memory.py`)

- `store_decision` — extract milestone lines from the rendered decision (re-parse +
  re-format to canonicalize) and write the `MILESTONES:` block; `DECISION` stays
  verbatim.
- `_parse_entry` — parse `milestones` (list of dicts) and `thesis_reflection`.
- `update_milestone_statuses(ticker, trade_date, status_by_claim, thesis_reflection=None)`
  — atomic (temp-file + replace) rewrite of matching milestone lines' status;
  appends `THESIS_REFLECTION` only if provided and not already present. Idempotent.
- `_apply_rotation` — keep entries with any pending milestone (§5.4).
- `get_past_context` — prefer `thesis_reflection` as the cross-ticker lesson when
  present (it is the durable, generalizable learning).

## 8. Graph changes (`trading_graph.py`)

- `_resolve_milestones(ticker)` — called from `propagate()` alongside
  `_resolve_pending_entries`. Gated on `config["milestone_grading_enabled"]`
  (default `False`). For each same-ticker entry with pending milestones whose
  `due_date <= today`:
  - call `_evaluate_milestone(...)` → `hit | miss | partial | None`.
  - `None` and `today > due + GRACE` → `expired`; else leave pending.
  - when the update closes the **last** pending milestone and no
    `THESIS_REFLECTION` exists yet → generate one via `reflector.reflect_on_thesis`.
  - write via `update_milestone_statuses` (atomic, idempotent).
- `_evaluate_milestone(entry, milestone, as_of)` — **STUB, returns `None`.** When
  built: fetch news + fundamentals **bounded to `as_of` (the due date), never
  today** — same look-ahead discipline as the deleted NVDA entry — and LLM-judge
  the claim. `kind` stays in the schema for future routing; do **not** build a
  deterministic quant-metric engine (fragile, large). Before relying on
  `guard_fundamentals_asof`, verify it accepts an arbitrary as-of date.

## 9. Reflection (`reflection.py`)

`reflect_on_thesis(final_decision, milestone_results)` — a **second** reflection,
scoped opposite to the entry one: it *does* score the thesis, because the milestones
it depended on have now resolved. Summarizes hit/miss/partial/expired, judges whether
the multi-month thesis held, extracts one lesson. States that `expired` means
"never confirmed within horizon" — not necessarily wrong. 2–4 sentences.

## 10. Config (`default_config.py`)

```python
"milestone_grading_enabled": False,   # resolver off until the evaluator is wired
```
Add a matching `TRADINGAGENTS_MILESTONE_GRADING_ENABLED` env override.

## 11. Tests

- schema: `render_pm_decision` emits canonical milestone lines.
- grammar: `format_milestone_line`/`parse_milestone_line` round-trip, incl. a `|`
  and non-ASCII in the claim.
- memory: round-trip store→parse with milestones (DECISION not swallowed); a
  `MILESTONES:` / `THESIS_REFLECTION:` section does not leak into `entry["decision"]`.
- rotation: an entry with a pending milestone survives eviction under `max_entries`.
- resolver (flag on, mock evaluator): due-scan; defer before grace; expire past
  grace; thesis reflection fires exactly on the some-pending→all-closed transition;
  idempotent re-run (no duplicate `THESIS_REFLECTION`).
- gate: resolver is a no-op when `milestone_grading_enabled` is `False`.

## 12. Explicitly out of scope (this PR)

- The evaluator's evidence fetch + LLM judge + as-of guard (§8) — stubbed.
- Deterministic quant-metric evaluation — rejected in favor of LLM-judge later.
- Earnings-calendar lookup to auto-fill `due_date` — PM supplies it for now.

## 13. What shipped

| File | Change |
|---|---|
| `agents/utils/milestones.py` | **new** — the grammar: format/parse/extract, `has_pending`, `is_due` |
| `agents/schemas.py` | `MilestoneKind`, `Milestone`, `PortfolioDecision.milestones`, render section |
| `agents/utils/memory.py` | generated section regexes, `MILESTONES:` block, `update_milestone_statuses`, rotation keep-condition, `thesis_reflection` in past context |
| `graph/reflection.py` | `reflect_on_thesis` + its prompt |
| `graph/trading_graph.py` | `_resolve_milestones`, `_resolve_entry_milestones`, `_evaluate_milestone` (stub), `propagate()` call |
| `default_config.py` | `milestone_grading_enabled` (off), `milestone_grace_days` (30) + env override |
| `tests/` | `test_milestones.py`, `test_milestone_memory.py`, `test_milestone_resolver.py` (+1 env-override case) |

Two decisions made during implementation, beyond the design as written:

- **Section regexes stop at *every other* header, not merely every later one.**
  `update_with_outcome` appends `REFLECTION` at the end of the block, so an entry
  whose thesis somehow closed before its 5-day window would have sections in an
  order the design assumed impossible — and the "later headers only" rule would
  have swallowed one. Order-independent patterns remove the assumption instead of
  documenting it.
- **A malformed milestone is dropped, not fatal.** `PortfolioDecision` filters
  invalid entries in a `mode="before"` validator, so a model writing `"Q3 2026"`
  into `due_date` costs that one milestone rather than the whole decision — which
  carries the rating the rest of the system depends on. The `due_date` field
  description tells the model to resolve quarters to concrete dates itself.

Also worth recording: the expiry comparison is done on `date`, not `datetime`.
With `datetime.now()` the grace boundary moved with the *time of day* a run
happened, so a milestone could expire on day 30 or day 31 depending on the clock.

One interaction worth knowing about rather than fixing: closing an entry's last
milestone also makes it evictable, so under a tight `memory_log_max_entries` a
freshly written `THESIS_REFLECTION` could rotate out before any run reads it.
The cap winning is the intended precedence, and rotation is off by default
(`memory_log_max_entries: None`), so this does not bite today.

Rating extraction is pinned by a test: capture is **ungated**, so from the next
run onward every decision carries free-text milestone claims, and those claims
routinely contain rating words ("Sell-side desks flip to Buy"). Both the memory
tag and the user-facing signal come from `parse_rating`, which anchors on the
leading `**Rating**:` line — `test_rating_extraction_ignores_keywords_inside_milestone_claims`
keeps it that way.

### Still open

- **The evaluator.** Until it is built, `milestone_grading_enabled` must stay off:
  an enabled resolver with a stub evaluator does nothing but expire real
  milestones once they pass grace.
- ~~**No live capture run yet.**~~ **Closed.** INTC @ 2026-07-10 on openai/gpt-5.5
  emitted three well-formed milestones with no structured-output fallback, so the
  `$ref`-to-object field is accepted in practice. The same run confirmed the parser
  fix (§5.3 — no section leakage, non-ASCII claim intact), the rotation keep-condition
  (§5.4), and the orthogonality claim itself: the 5d tag resolved to
  `-13.5% | -11.9% | 5d` while all three milestones stayed `pending`.

  Two things the run exposed, both fixed in the schema descriptions rather than by
  filtering after the fact:

  - The PM tagged a *price/technical* claim ("reclaims the 117–118 zone") as `quant`.
    Since the evaluator will route on `kind`, that sends a price claim to a
    fundamentals lookup which cannot answer it. Adding a `price` kind was rejected —
    it would legitimize the very drift milestones exist to prevent, and commit the
    evaluator to duplicating what the 5-day window already grades. The `claim`,
    `kind`, and `milestones` descriptions now rule out share-price and chart levels,
    and `quant` reads as *a number the company reports*. Pinned by
    `test_schema_steers_the_model_off_price_level_claims`, which asserts against the
    generated JSON schema — the text the provider actually receives.
  - That same milestone restated entry timing, which the 5d reflection already
    covers. The list-level description now rules that out too.

- **Entry reflection still manufactures a lesson on a correct call.** Observed in the
  same run: on a call that was right (-11.9% alpha under `Underweight`), the
  reflection nonetheless produced "do not press a bearish call without a clearly
  broken support ... wait for the setup to confirm" — advice for a mistake attached
  to a success, which then enters `past_context`. The prompt demands one concrete
  lesson every time (`reflection.py`); letting it conclude "executed well, nothing to
  change" would fix it. Not addressed here.
```

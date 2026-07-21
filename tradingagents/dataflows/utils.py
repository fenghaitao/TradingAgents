import re
from datetime import date, datetime, timedelta
from typing import Annotated

import pandas as pd

SavePathType = Annotated[str, "File path to save data. If None, data is not saved."]

# Tickers can contain letters, digits, dot, dash, underscore, caret
# (index symbols like ^GSPC), equals (futures like GC=F), and plus
# (forex/CFD symbols like XAUUSD+). None of these enable directory
# traversal, so the value never escapes a containing directory when
# interpolated into a path. Anything else is rejected.
_TICKER_PATH_RE = re.compile(r"^[A-Za-z0-9._\-\^=+]+$")


def safe_ticker_component(value: str, *, max_len: int = 32) -> str:
    """Validate ``value`` is safe to interpolate into a filesystem path.

    Tickers come from user CLI input or from LLM tool calls, both of which
    can be influenced by attacker-controlled content (e.g. prompt injection
    embedded in fetched news). Without validation, a value like
    ``"../../../etc/foo"`` flows into ``os.path.join`` / ``Path /`` and
    escapes the configured cache, checkpoint, or results directory.

    Returns ``value`` unchanged when it matches the allowed pattern; raises
    ``ValueError`` otherwise.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"ticker must be a non-empty string, got {value!r}")
    if len(value) > max_len:
        raise ValueError(f"ticker exceeds {max_len} chars: {value!r}")
    if not _TICKER_PATH_RE.fullmatch(value):
        raise ValueError(
            f"ticker contains characters not allowed in a filesystem path: {value!r}"
        )
    # The regex above allows '.', so values like '.', '..', '...' would pass,
    # and as a path component they traverse the parent directory. Reject any
    # value that's only dots.
    if set(value) == {"."}:
        raise ValueError(f"ticker cannot consist solely of dots: {value!r}")
    return value


def save_output(data: pd.DataFrame, tag: str, save_path: SavePathType = None) -> None:
    if save_path:
        data.to_csv(save_path, encoding="utf-8")
        print(f"{tag} saved to {save_path}")


def get_current_date():
    return date.today().strftime("%Y-%m-%d")


def guard_fundamentals_asof(
    ticker: str,
    canonical: str | None = None,
    curr_date: str | None = None,
) -> None:
    """Refuse a live fundamentals snapshot when ``curr_date`` is materially past.

    Vendor overview endpoints (yfinance ``.info``, Alpha Vantage ``OVERVIEW``)
    return only a *current* snapshot — there is no as-of version to request.
    Serving one for a historical ``curr_date`` silently injects look-ahead bias:
    a 2024 run reads today's market cap and TTM revenue while the report claims
    to speak "as of" the trade date. Statement-level calls dodge this by dropping
    fiscal periods after ``curr_date``; the snapshot has no equivalent lever, so
    refusing is the only correct answer rather than a stopgap for a missing feature.

    Raises ``NoMarketDataError`` — the taxonomy's "no usable rows" case, which the
    router converts into an explicit ``NO_DATA_AVAILABLE`` sentinel — so the agent
    reports fundamentals as unavailable instead of quoting figures from the future.
    Silent when ``curr_date`` is absent, unparseable, or within
    ``fundamentals_max_staleness_days`` (None disables the guard entirely).
    """
    if not curr_date:
        return

    # Imported lazily: config imports default_config, and errors is a leaf
    # module — keeping both out of this module's import-time graph avoids a
    # cycle with the vendor modules that call this helper.
    from .config import get_config
    from .errors import NoMarketDataError

    try:
        asked = datetime.strptime(str(curr_date)[:10], "%Y-%m-%d").date()
    except ValueError:
        return  # Malformed dates are the caller's problem, not a staleness signal.

    max_stale = get_config().get("fundamentals_max_staleness_days", 7)
    if max_stale is None:
        return

    today = date.today()
    age = (today - asked).days
    if age <= max_stale:
        return

    raise NoMarketDataError(
        ticker,
        canonical,
        f"fundamentals snapshot is live-only (as of {today.isoformat()}), but the "
        f"requested date {asked.isoformat()} is {age} days earlier — beyond the "
        f"fundamentals_max_staleness_days={max_stale} limit. Serving it would report "
        f"present-day figures as historical ones. This vendor cannot provide "
        f"point-in-time fundamentals; use the statement-level tools, which filter "
        f"by fiscal period"
    )


def decorate_all_methods(decorator):
    def class_decorator(cls):
        for attr_name, attr_value in cls.__dict__.items():
            if callable(attr_value):
                setattr(cls, attr_name, decorator(attr_value))
        return cls

    return class_decorator


def get_next_weekday(date):

    if not isinstance(date, datetime):
        date = datetime.strptime(date, "%Y-%m-%d")

    if date.weekday() >= 5:
        days_to_add = 7 - date.weekday()
        next_weekday = date + timedelta(days=days_to_add)
        return next_weekday
    else:
        return date

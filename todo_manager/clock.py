"""Time helpers. All persisted timestamps are UTC ISO-8601 strings with a trailing ``Z``.

The current time can be frozen for tests through ``set_now`` so that batching and
due-date logic is deterministic.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_frozen: datetime | None = None


def set_now(value: datetime | None) -> None:
    """Freeze ``now()`` to ``value`` (aware datetime) or unfreeze with ``None``."""
    global _frozen
    if value is not None and value.tzinfo is None:
        raise ValueError("frozen time must be timezone-aware")
    _frozen = value


def advance(**kwargs: float) -> datetime:
    """Advance a frozen clock (tests only)."""
    global _frozen
    if _frozen is None:
        raise RuntimeError("clock is not frozen")
    _frozen = _frozen + timedelta(**kwargs)
    return _frozen


def now() -> datetime:
    return _frozen if _frozen is not None else datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    value = value or now()
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def local_date(tz_name: str, value: datetime | None = None) -> date:
    return (value or now()).astimezone(ZoneInfo(tz_name)).date()


def local_dt(tz_name: str, value: datetime | None = None) -> datetime:
    return (value or now()).astimezone(ZoneInfo(tz_name))


def minutes_since(value: str | None) -> float | None:
    parsed = parse(value)
    if parsed is None:
        return None
    return (now() - parsed).total_seconds() / 60.0

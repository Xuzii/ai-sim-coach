"""Timestamp helpers. Everything is stored in UTC and converted at query time --
future-you debugging a session that straddles a DST change will be grateful.
"""

from __future__ import annotations

from datetime import datetime, timezone, tzinfo


def utc_now() -> datetime:
    """Timezone-aware current time in UTC."""
    return datetime.now(timezone.utc)


def to_iso_utc(dt: datetime) -> str:
    """Serialise a datetime to an ISO-8601 string in UTC for storage.

    Naive datetimes are assumed to already be UTC (we never store local time).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def from_iso_utc(s: str) -> datetime:
    """Parse a stored ISO-8601 timestamp into a timezone-aware UTC datetime."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_local(dt: datetime, tz: tzinfo | None = None) -> datetime:
    """Convert a (UTC) datetime to local time at query time.

    ``tz=None`` uses the system local timezone.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def epoch_to_iso(seconds: int | float | None, milliseconds: int | None = None) -> str | None:
    if milliseconds is not None:
        return iso_utc(datetime.fromtimestamp(milliseconds / 1000.0, UTC))
    if seconds is None:
        return None
    return iso_utc(datetime.fromtimestamp(float(seconds), UTC))

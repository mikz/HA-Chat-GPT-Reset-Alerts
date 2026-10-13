"""Normalized data models for ChatGPT/Codex usage."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from math import isfinite
from typing import Any


@dataclass(frozen=True, slots=True)
class UsageWindow:
    """One usage allowance window."""

    id: str
    display_name: str
    used_percent: float | None = None
    remaining_percent: float | None = None
    reset_at: datetime | None = None
    duration_seconds: int | None = None
    limit_name: str = "Codex"
    kind: str = "usage"
    allowed: bool | None = None
    limit_reached: bool | None = None
    is_main: bool = True
    reset_status: str = "unknown"

    def to_safe_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["reset_at"] = self.reset_at.isoformat() if self.reset_at else None
        return data


@dataclass(frozen=True, slots=True)
class CreditStatus:
    """Read-only credit information returned by the usage endpoint."""

    has_credits: bool | None = None
    unlimited: bool | None = None
    balance: float | None = None
    overage_limit_reached: bool | None = None


@dataclass(frozen=True, slots=True)
class ChatGPTUsageData:
    """Normalized provider response."""

    windows: tuple[UsageWindow, ...] = field(default_factory=tuple)
    plan: str | None = None
    account_id: str | None = None
    source: str = "unknown"
    credits: CreditStatus | None = None
    available_reset_credits: int | None = None
    # Only available reset rows; None means details were not reported. A null
    # date inside the tuple means that row did not report a usable expiry.
    reset_credit_expiries: tuple[datetime | None, ...] | None = None
    blocker_reason: str | None = None
    last_updated: datetime = field(default_factory=lambda: datetime.now(UTC))

    def window(self, window_id: str) -> UsageWindow | None:
        return next((window for window in self.windows if window.id == window_id), None)

    @property
    def upcoming_reset_credit_expiries(self) -> tuple[datetime, ...]:
        """Known available expiries as of the last successful provider reading."""
        if self.available_reset_credits == 0:
            return ()
        return tuple(sorted(
            expiry for expiry in self.reset_credit_expiries or ()
            if expiry is not None and expiry > self.last_updated
        ))

    @property
    def next_reset_credit_expiry(self) -> datetime | None:
        return next(iter(self.upcoming_reset_credit_expiries), None)

    def reset_credit_expiry_attributes(self) -> dict[str, Any]:
        """One expiry representation shared by the count and timestamp entities."""
        dates = self.upcoming_reset_credit_expiries
        complete = self.available_reset_credits == 0 or (
            self.reset_credit_expiries is not None
            and len(self.reset_credit_expiries) == len(dates) == self.available_reset_credits
        )
        status = (
            "none_available" if self.available_reset_credits == 0
            else "not_reported" if not dates
            else "scheduled" if complete
            else "partial"
        )
        return {
            "next_expiry": dates[0].isoformat() if dates else None,
            "expiry_dates": [date.isoformat() for date in dates],
            "expiry_details_complete": complete,
            "expiry_status": status,
        }


@dataclass(slots=True)
class PersistedWindowState:
    """Minimal non-sensitive reset state."""

    reset_at: str | None = None
    used_percent: float | None = None
    remaining_percent: float | None = None
    last_event_key: str | None = None
    last_reset_at: str | None = None
    is_main: bool = True
    limited: bool | None = None

    @classmethod
    def from_window(
        cls, window: UsageWindow, last_event_key: str | None = None,
        last_reset_at: str | None = None,
    ) -> "PersistedWindowState":
        return cls(
            reset_at=window.reset_at.isoformat() if window.reset_at else None,
            used_percent=window.used_percent,
            remaining_percent=window.remaining_percent,
            last_event_key=last_event_key,
            last_reset_at=last_reset_at,
            is_main=window.is_main,
            limited=(
                window.limit_reached is True or window.allowed is False
                or (window.used_percent is not None and window.used_percent >= 100)
            ),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PersistedWindowState":
        return cls(
            reset_at=data.get("reset_at"),
            used_percent=to_float(data.get("used_percent")),
            remaining_percent=to_float(data.get("remaining_percent")),
            last_event_key=data.get("last_event_key"),
            last_reset_at=data.get("last_reset_at"),
            is_main=data.get("is_main", True),
            limited=data.get("limited"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "reset_at": self.reset_at,
            "used_percent": self.used_percent,
            "remaining_percent": self.remaining_percent,
            "last_event_key": self.last_event_key,
            "last_reset_at": self.last_reset_at,
            "is_main": self.is_main,
            "limited": self.limited,
        }


def parse_datetime(value: Any) -> datetime | None:
    """Parse ISO or epoch timestamps into aware UTC datetimes."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (ValueError, OSError, OverflowError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        if text.replace(".", "", 1).isdigit():
            return datetime.fromtimestamp(float(text), tz=UTC)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, OSError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def clamp_percent(value: Any) -> float | None:
    number = to_float(value)
    if number is None or number < 0 or number > 100:
        return None
    return round(number, 2)


def remaining_from_used(used: float | None) -> float | None:
    return None if used is None else round(max(0.0, 100.0 - used), 2)


def to_float(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        result = float(value)
        return result if isfinite(result) else None
    except (TypeError, ValueError):
        return None

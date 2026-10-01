"""Stable quota observations, independent of Home Assistant or transport."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from .models import ChatGPTUsageData, CreditStatus, PersistedWindowState, UsageWindow, parse_datetime

RESET_JITTER = timedelta(seconds=5)
IDLE_TOLERANCE = timedelta(seconds=60)


def stabilize_window(
    window: UsageWindow, previous: PersistedWindowState | None, now: datetime
) -> UsageWindow:
    """Hide an unstarted full window and retain an existing deadline through jitter."""
    reset = window.reset_at
    if reset is None:
        return replace(window, reset_status="unknown")
    if (
        window.used_percent == 0
        and window.allowed is not False
        and window.limit_reached is not True
        and window.duration_seconds is not None
        and abs(reset - now - timedelta(seconds=window.duration_seconds)) <= IDLE_TOLERANCE
    ):
        return replace(window, reset_at=None, reset_status="awaiting_usage")
    old_reset = parse_datetime(previous.reset_at) if previous else None
    if old_reset and abs(reset - old_reset) <= RESET_JITTER:
        reset = old_reset
    return replace(window, reset_at=reset, reset_status="scheduled")


def account_status(
    data: ChatGPTUsageData, previous: dict[str, PersistedWindowState]
) -> str:
    """Describe ordinary Codex allowance; named feature limits stay separate."""
    if data.blocker_reason in ("credits", "spend", "unknown"):
        return "blocked"
    limited = ordinary_limit_reached(data, previous)
    if limited is None:
        return "incomplete"
    return "limited" if limited else "available"


def ordinary_limit_reached(
    data: ChatGPTUsageData, previous: dict[str, PersistedWindowState]
) -> bool | None:
    """Use the same main-window rules for status and the limit binary sensor."""
    main = [window for window in data.windows if window.is_main]
    if data.blocker_reason == "usage_limit" or any(window_limited(w) for w in main):
        return True
    current_ids = {window.id for window in main}
    # Never call disappearance of an exhausted window a recovery.
    missing_blocker = any(
        state.is_main and (state.limited is True or (state.used_percent or 0) >= 100)
        and window_id not in current_ids
        for window_id, state in previous.items()
    )
    if not main or missing_blocker or any(w.used_percent is None for w in main):
        return None
    return False


def credits_available(credits: CreditStatus | None) -> bool | None:
    """Report provider credit entitlement without inventing missing information."""
    if credits is None:
        return None
    if credits.unlimited is True or credits.has_credits is True:
        return True
    if credits.has_credits is False:
        return False
    return None


def access_status(data: ChatGPTUsageData, ordinary_status: str) -> str | None:
    """Presentation status; credit availability never counts as quota recovery."""
    if ordinary_status == "incomplete":
        return None
    if ordinary_status == "limited":
        if data.credits and data.credits.overage_limit_reached is True:
            return "blocked"
        if credits_available(data.credits) is True:
            return "credits_available"
    return ordinary_status


def window_limited(window: UsageWindow) -> bool:
    return (
        window.allowed is False or window.limit_reached is True
        or (window.used_percent is not None and window.used_percent >= 100)
    )

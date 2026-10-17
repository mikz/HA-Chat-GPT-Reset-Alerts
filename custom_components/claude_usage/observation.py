"""Stable deadlines and conservative ordinary-allowance availability."""

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from .models import ClaudeUsageData, UsageWindow, parse_datetime


def stabilize_window(window: UsageWindow, saved: dict[str, Any], now: datetime) -> UsageWindow:
    reset = window.reset_at
    if reset is None:
        return replace(window, reset_status="unknown")
    if (window.used_percent == 0 and window.duration_seconds is not None
            and abs(reset - now - timedelta(seconds=window.duration_seconds)) <= timedelta(seconds=60)):
        return replace(window, reset_at=None, reset_status="awaiting_usage")
    old = parse_datetime(saved.get("reset_at"))
    if old and abs(reset - old) <= timedelta(seconds=5):
        reset = old
    return replace(window, reset_at=reset, reset_status="scheduled")


def ordinary_available(data: ClaudeUsageData, saved: dict[str, Any]) -> bool | None:
    main = [w for w in data.windows.values() if w.is_main]
    if any(w.used_percent is not None and w.used_percent >= 100 for w in main):
        return False
    if not main or any(w.used_percent is None for w in main):
        return None
    # A missing exhausted bucket is incomplete evidence, not a recovery.
    if any(wid in ("session", "weekly") and wid not in data.windows
           and (state.get("used_percent") or 0) >= 100 for wid, state in saved.items()):
        return None
    return True


def access_status(data: ClaudeUsageData, usable: bool | None) -> str | None:
    if usable is None:
        return None
    if usable:
        return "available"
    extra = data.extra_usage
    if extra and extra.enabled is True and extra.remaining is not None and extra.remaining > 0:
        return "extra_usage_available"
    return "limited"

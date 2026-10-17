"""Provider observations must not manufacture resets or availability."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from conftest import load_module

models = load_module("models")
observation = load_module("observation")
logic = load_module("reset_logic")


def window(used, reset, window_id="weekly"):
    return models.UsageWindow(window_id, window_id, used, reset, 604800)


def test_jitter_and_idle_deadlines_do_not_become_resets():
    now = datetime(2026, 10, 17, tzinfo=UTC)
    deadline = now + timedelta(days=3)
    old = {"reset_at": deadline.isoformat(), "used_percent": 50}
    stable = observation.stabilize_window(window(50, deadline + timedelta(seconds=1)), old, now)
    assert stable.reset_at == deadline
    assert stable.reset_status == "scheduled"
    idle = observation.stabilize_window(window(0, now + timedelta(days=7)), {}, now)
    assert idle.reset_at is None
    assert idle.reset_status == "awaiting_usage"
    assert not logic.reset_detected(logic.WindowSnapshot(0, now), window(0, now + timedelta(days=7)), now=now)


def test_missing_exhausted_bucket_never_recovers_and_scoped_limits_are_separate():
    data = models.ClaudeUsageData(windows={"session": window(10, None, "session")})
    assert observation.ordinary_available(data, {"weekly": {"used_percent": 100}}) is None
    data = replace(data, windows={**data.windows, "weekly_sonnet": window(100, None, "weekly_sonnet")})
    assert observation.ordinary_available(data, {}) is True
    assert observation.ordinary_available(replace(data, windows={}), {}) is None


@pytest.mark.parametrize("bad", [True, False, "NaN", "Infinity", -1, {}, "bad"])
def test_malformed_usage_remains_unknown(bad):
    data = models.parse_anthropic_usage({"seven_day": {"utilization": bad, "resets_at": "2026-10-20T12:00:00Z"}})
    assert data.windows["weekly"].used_percent is None
    assert observation.ordinary_available(data, {}) is None


def test_extra_usage_preserves_precision_and_unknown_flags():
    data = models.parse_anthropic_usage({"extra_usage": {"is_enabled": "false", "used_credits": 1234.5678,
                                                        "monthly_limit": 10000, "decimal_places": 2}})
    assert data.extra_usage.enabled is None
    assert data.extra_usage.used == 12.345678
    assert observation.access_status(data, False) == "limited"
    zero_exponent = models.parse_anthropic_usage({"extra_usage": {"is_enabled": True, "used_credits": 12,
                                                               "monthly_limit": 100, "decimal_places": 0}})
    assert zero_exponent.extra_usage.used == 12
    assert observation.access_status(zero_exponent, False) == "extra_usage_available"


def test_dynamic_opus_window_is_not_a_global_blocker():
    data = models.parse_anthropic_usage({"seven_day": {"utilization": 20}, "seven_day_opus": {"utilization": 100}})
    assert data.windows["weekly_opus"].remaining_percent == 0
    assert observation.ordinary_available(data, {}) is True

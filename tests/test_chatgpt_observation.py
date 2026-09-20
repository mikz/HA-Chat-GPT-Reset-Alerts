from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.chatgpt_usage.history import last_refill
from custom_components.chatgpt_usage.models import ChatGPTUsageData, PersistedWindowState, UsageWindow
from custom_components.chatgpt_usage.observation import account_status, stabilize_window
from custom_components.chatgpt_usage.parsing import parse_openai_usage
from custom_components.chatgpt_usage.reset import detect_reset

NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)


def window(used=50, reset=None, **kwargs):
    return UsageWindow("weekly", "Weekly", used, 100 - used,
                       reset or NOW + timedelta(days=2), 604800, **kwargs)


def test_one_second_jitter_does_not_change_deadline_over_many_polls_or_reload():
    initial = window()
    previous = PersistedWindowState.from_window(initial)
    for seconds in [1, -1, 1, 0, -1]:
        current = stabilize_window(replace(initial, reset_at=initial.reset_at + timedelta(seconds=seconds)), previous, NOW)
        assert current.reset_at == initial.reset_at
        assert detect_reset(previous, current, NOW) is None
        previous = PersistedWindowState.from_dict(PersistedWindowState.from_window(current).as_dict())


def test_idle_relative_deadline_stays_absent_then_activity_starts_window():
    previous = None
    for hours in range(24):
        now = NOW + timedelta(hours=hours)
        raw = window(0, now + timedelta(days=7))
        current = stabilize_window(raw, previous, now)
        assert current.reset_at is None
        assert current.reset_status == "awaiting_usage"
        if previous:
            assert detect_reset(previous, current, now) is None
        previous = PersistedWindowState.from_window(current)
    active = stabilize_window(window(1, now + timedelta(days=7)), previous, now)
    assert active.reset_at == now + timedelta(days=7)
    assert active.reset_status == "scheduled"


def test_real_deadline_change_and_zero_usage_mid_window_are_preserved():
    previous = PersistedWindowState.from_window(window())
    moved = stabilize_window(window(10, NOW + timedelta(days=3)), previous, NOW)
    assert moved.reset_at == NOW + timedelta(days=3)
    zero = stabilize_window(window(0), previous, NOW)
    assert zero.reset_at == NOW + timedelta(days=2)


def test_reset_to_idle_is_observed_without_a_new_deadline():
    previous = PersistedWindowState.from_window(window(100, NOW - timedelta(minutes=1)))
    current = stabilize_window(window(0, NOW + timedelta(days=7)), previous, NOW)
    assert current.reset_at is None
    assert detect_reset(previous, current, NOW) is not None


def test_named_feature_does_not_block_ordinary_usage():
    data = ChatGPTUsageData(windows=(window(20), replace(window(100), id="spark", is_main=False)))
    assert account_status(data, {}) == "available"


def test_all_reported_main_windows_must_recover():
    data = ChatGPTUsageData(windows=(window(0), replace(window(100), id="five_hour")))
    assert account_status(data, {}) == "limited"


def test_disappearing_exhausted_main_window_is_not_a_recovery():
    previous = {"weekly": PersistedWindowState.from_window(window(100))}
    data = ChatGPTUsageData(windows=(replace(window(10), id="five_hour"),))
    assert account_status(data, previous) == "incomplete"


@pytest.mark.parametrize("blocker", ["credits", "spend", "unknown"])
def test_non_usage_blocker_prevents_usable_status(blocker):
    assert account_status(ChatGPTUsageData(windows=(window(0),), blocker_reason=blocker), {}) == "blocked"


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf")])
def test_invalid_usage_never_means_available(bad):
    main = replace(window(), used_percent=bad, remaining_percent=None)
    if bad is None:
        assert account_status(ChatGPTUsageData(windows=(main,)), {}) == "incomplete"
    else:
        with pytest.raises(ValueError):
            parse_openai_usage({"rate_limit": {"primary_window": {"used_percent": bad}}})


def test_history_import_uses_observation_time_and_skips_missing_data():
    records = [SimpleNamespace(state=str(value), last_changed=NOW + timedelta(minutes=index))
               for index, value in enumerate([100, "unavailable", 0, 1, 2])]
    assert last_refill(records) == NOW + timedelta(minutes=2)
    assert last_refill(records[2:]) is None
    assert last_refill([SimpleNamespace(state="100", last_changed=NOW)]) is None


def test_moved_deadline_with_unchanged_exhausted_usage_is_not_a_reset():
    previous = PersistedWindowState.from_window(window(100, NOW - timedelta(minutes=1)))
    assert detect_reset(previous, window(100, NOW + timedelta(days=7)), NOW) is None


def test_observed_recovery_counts_even_before_provider_changes_deadline():
    previous = PersistedWindowState.from_window(window(100))
    assert detect_reset(previous, window(70), NOW).confidence == "usage_recovered"

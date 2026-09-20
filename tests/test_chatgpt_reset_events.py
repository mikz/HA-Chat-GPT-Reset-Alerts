"""Test production reset processing with Home Assistant boundaries stubbed."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.chatgpt_usage.const import EVENT_ACCOUNT_RECOVERED, EVENT_USAGE_RESET
from custom_components.chatgpt_usage.models import ChatGPTUsageData, UsageWindow

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
OLD_RESET = NOW - timedelta(minutes=5)
NEW_RESET = NOW + timedelta(days=7)


@pytest.fixture
def coordinator_class(monkeypatch):
    """Load the real coordinator without installing or starting Home Assistant."""
    class CoordinatorBase:
        def __class_getitem__(cls, item):
            return cls

    dependencies = {
        "homeassistant": {},
        "homeassistant.helpers": {},
        "homeassistant.config_entries": {"ConfigEntry": object},
        "homeassistant.core": {"HomeAssistant": object},
        "homeassistant.exceptions": {"ConfigEntryAuthFailed": Exception},
        "homeassistant.helpers.storage": {"Store": object},
        "homeassistant.helpers.update_coordinator": {
            "DataUpdateCoordinator": CoordinatorBase,
            "UpdateFailed": Exception,
        },
        "custom_components.chatgpt_usage.api": {
            "LocalCodexProvider": object,
            "RemoteOpenAIProvider": object,
            "UsageProvider": object,
            "ProviderAuthError": Exception,
            "ProviderConnectionError": Exception,
            "ProviderError": Exception,
            "ProviderRateLimited": Exception,
            "ProviderSchemaError": Exception,
        },
    }
    path = (
        Path(__file__).resolve().parents[1]
        / "custom_components/chatgpt_usage/coordinator.py"
    )
    spec = importlib.util.spec_from_file_location(
        "custom_components.chatgpt_usage._reset_event_test_coordinator", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Keep dependency stubs scoped to loading this isolated module. Real reset
    # detection, event construction, and persistence-state updates are exercised.
    with monkeypatch.context() as context:
        for name, attributes in dependencies.items():
            stub = ModuleType(name)
            stub.__dict__.update(attributes)
            context.setitem(sys.modules, name, stub)
        context.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
    return module.ChatGPTUsageCoordinator


def _coordinator(coordinator_class, entry_id="entry-a", title="Personal", provider="remote"):
    coordinator = object.__new__(coordinator_class)
    coordinator.entry = SimpleNamespace(
        entry_id=entry_id,
        title=title,
        data={
            "provider": provider,
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "id_token": "id-secret",
            "api_key": "helper-secret",
        },
    )
    coordinator.hass = SimpleNamespace(bus=SimpleNamespace(async_fire=Mock()))
    coordinator._store = SimpleNamespace(async_save=AsyncMock())
    coordinator._window_state = {}
    coordinator._last_known_usable = None
    coordinator.last_recovered_at = None
    coordinator._pending_events = []
    coordinator._ready = True
    coordinator._history_imported = True
    return coordinator


def _data(used, reset_at):
    return ChatGPTUsageData(windows=(UsageWindow(
        id="weekly",
        display_name="Weekly",
        used_percent=used,
        remaining_percent=100 - used,
        reset_at=reset_at,
        duration_seconds=604800,
    ),))


def _prime(coordinator):
    asyncio.run(coordinator._process_resets(
        _data(98, OLD_RESET), NOW - timedelta(minutes=10)
    ))


def _reset(coordinator):
    asyncio.run(coordinator._process_resets(_data(2, NEW_RESET), NOW))


@pytest.mark.parametrize("provider", ["remote", "local"])
def test_reset_event_preserves_payload_and_adds_only_entry_metadata(coordinator_class, provider):
    coordinator = _coordinator(coordinator_class, provider=provider)
    _prime(coordinator)
    _reset(coordinator)

    coordinator.hass.bus.async_fire.assert_called_once_with(EVENT_USAGE_RESET, {
        "entry_id": "entry-a",
        "entry_title": "Personal",
        "window_id": "weekly",
        "window": "Weekly",
        "limit_name": "Codex",
        "previous_used_percent": 98,
        "new_used_percent": 2,
        "previous_remaining_percent": 2,
        "new_remaining_percent": 98,
        "remaining_percent": 98,
        "previous_reset_at": OLD_RESET.isoformat(),
        "new_reset_at": NEW_RESET.isoformat(),
        "confidence": "timestamp_rollover",
        "observed_at": NOW.isoformat(),
    })


def test_two_accounts_with_same_title_and_window_have_distinct_event_ids(coordinator_class):
    first = _coordinator(coordinator_class, "entry-a", "ChatGPT Usage")
    second = _coordinator(coordinator_class, "entry-b", "ChatGPT Usage")
    second.hass = first.hass
    for coordinator in (first, second):
        _prime(coordinator)
        _reset(coordinator)

    calls = first.hass.bus.async_fire.call_args_list
    assert len(calls) == 2
    assert [call.args[0] for call in calls] == [EVENT_USAGE_RESET, EVENT_USAGE_RESET]
    assert [call.args[1]["entry_id"] for call in calls] == ["entry-a", "entry-b"]
    assert all(call.args[1]["window_id"] == "weekly" for call in calls)
    assert all(call.args[1]["entry_title"] == "ChatGPT Usage" for call in calls)


def test_no_startup_event_or_duplicate_reset_event(coordinator_class):
    coordinator = _coordinator(coordinator_class)
    _prime(coordinator)
    coordinator.hass.bus.async_fire.assert_not_called()
    _reset(coordinator)
    _reset(coordinator)
    coordinator.hass.bus.async_fire.assert_called_once()
    assert coordinator._store.async_save.await_count == 3


def test_event_uses_current_entry_title_without_changing_identifier(coordinator_class):
    coordinator = _coordinator(coordinator_class)
    _prime(coordinator)
    coordinator.entry.title = "Renamed account"
    _reset(coordinator)
    event = coordinator.hass.bus.async_fire.call_args.args[1]
    assert event["entry_id"] == "entry-a"
    assert event["entry_title"] == "Renamed account"


def test_usable_again_fires_once_and_survives_reload(coordinator_class):
    coordinator = _coordinator(coordinator_class)
    asyncio.run(coordinator._process_resets(_data(100, OLD_RESET), NOW - timedelta(minutes=10)))
    assert coordinator.account_status == "limited"
    asyncio.run(coordinator._process_resets(_data(0, NEW_RESET), NOW))
    events = coordinator.hass.bus.async_fire.call_args_list
    recovered = [event.args[1] for event in events if event.args[0] == EVENT_ACCOUNT_RECOVERED]
    assert len(recovered) == 1
    assert recovered[0]["remaining_percent"] == 100
    assert coordinator.last_reset_at("weekly") == NOW
    assert coordinator.last_recovered_at == NOW

    stored = coordinator._store.async_save.call_args.args[0]
    restarted = _coordinator(coordinator_class)
    restarted._store.async_load = AsyncMock(return_value=stored)
    asyncio.run(restarted.async_initialize())
    asyncio.run(restarted._process_resets(_data(0, NEW_RESET + timedelta(minutes=5)), NOW + timedelta(minutes=5)))
    restarted.hass.bus.async_fire.assert_not_called()
    assert restarted.last_reset_at("weekly") == NOW
    assert restarted.last_recovered_at == NOW


def test_first_healthy_sample_never_sends_recovery(coordinator_class):
    coordinator = _coordinator(coordinator_class)
    asyncio.run(coordinator._process_resets(_data(0, NEW_RESET), NOW))
    coordinator.hass.bus.async_fire.assert_not_called()
    assert coordinator.last_reset_at() is None


def test_recovery_waits_for_ha_start_and_pending_event_survives_restart(coordinator_class):
    coordinator = _coordinator(coordinator_class)
    coordinator._ready = False
    asyncio.run(coordinator._process_resets(_data(100, OLD_RESET), NOW - timedelta(minutes=10)))
    asyncio.run(coordinator._process_resets(_data(0, NEW_RESET), NOW))
    coordinator.hass.bus.async_fire.assert_not_called()
    stored = coordinator._store.async_save.call_args.args[0]
    assert any(event["type"] == EVENT_ACCOUNT_RECOVERED for event in stored["pending_events"])
    restarted = _coordinator(coordinator_class)
    restarted._store.async_load = AsyncMock(return_value=stored)
    asyncio.run(restarted.async_initialize())
    asyncio.run(restarted.async_start(restarted.hass))
    asyncio.run(restarted.async_start(restarted.hass))
    events = restarted.hass.bus.async_fire.call_args_list
    assert sum(event.args[0] == EVENT_ACCOUNT_RECOVERED for event in events) == 1


def test_missing_main_data_keeps_recovery_armed_without_false_alert(coordinator_class):
    coordinator = _coordinator(coordinator_class)
    asyncio.run(coordinator._process_resets(_data(100, OLD_RESET), NOW - timedelta(minutes=10)))
    asyncio.run(coordinator._process_resets(ChatGPTUsageData(), NOW))
    assert coordinator.account_status == "incomplete"
    coordinator.hass.bus.async_fire.assert_not_called()
    asyncio.run(coordinator._process_resets(_data(5, NEW_RESET), NOW + timedelta(minutes=5)))
    assert sum(call.args[0] == EVENT_ACCOUNT_RECOVERED for call in coordinator.hass.bus.async_fire.call_args_list) == 1


def test_two_main_windows_emit_one_account_recovery_after_both_are_usable(coordinator_class):
    from dataclasses import replace
    coordinator = _coordinator(coordinator_class)
    def both(weekly, short):
        return ChatGPTUsageData(windows=(
            _data(weekly, NEW_RESET).windows[0],
            replace(_data(short, NEW_RESET).windows[0], id="five_hour", duration_seconds=18000),
        ))
    asyncio.run(coordinator._process_resets(both(100, 100), NOW))
    asyncio.run(coordinator._process_resets(both(0, 100), NOW + timedelta(minutes=5)))
    assert all(call.args[0] != EVENT_ACCOUNT_RECOVERED for call in coordinator.hass.bus.async_fire.call_args_list)
    asyncio.run(coordinator._process_resets(both(0, 0), NOW + timedelta(minutes=10)))
    recovered = [call for call in coordinator.hass.bus.async_fire.call_args_list if call.args[0] == EVENT_ACCOUNT_RECOVERED]
    assert len(recovered) == 1

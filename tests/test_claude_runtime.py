"""Validate the installed HA lifecycle, entity metadata, and reset persistence."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.core import CoreState  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.claude_usage.api import ClaudeConnectionError, ClaudeRateLimitError  # noqa: E402
from custom_components.claude_usage.const import EVENT_ACCOUNT_RECOVERED  # noqa: E402
from custom_components.claude_usage.models import ClaudeUsageData, ExtraUsage, UsageWindow  # noqa: E402


def usage(used, reset):
    return ClaudeUsageData(windows={"weekly": UsageWindow("weekly", "Weekly", used, reset, 604800)})


def entity(hass, entry, domain, suffix):
    return er.async_get(hass).async_get_entity_id(domain, "claude_usage", f"{entry.entry_id}_{suffix}")


@pytest.mark.asyncio
async def test_recovery_persists_and_failures_do_not_replay(hass, enable_custom_integrations):
    entry = MockConfigEntry(domain="claude_usage", title="test@example.com", unique_id="test",
                            data={"provider": "remote", "account_name": "test@example.com", "access_token": "secret"})
    entry.add_to_hass(hass)
    provider = AsyncMock()
    provider.async_get_usage.return_value = usage(100, datetime.now(UTC) - timedelta(minutes=1))
    events = []
    hass.bus.async_listen(EVENT_ACCOUNT_RECOVERED, events.append)
    with patch("custom_components.claude_usage.RemoteAnthropicProvider", return_value=provider):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await hass.async_start()
        await hass.async_block_till_done()
        usable = entity(hass, entry, "binary_sensor", "usable")
        status = entity(hass, entry, "sensor", "account_status")
        last_reset = entity(hass, entry, "sensor", "last_reset")
        reset = entity(hass, entry, "sensor", "weekly_reset")
        recovery = entity(hass, entry, "event", "recovery_event")
        assert hass.states.get(status).state == "limited"
        assert hass.states.get(usable).state == "off"
        assert not events

        # Losing the exhausted bucket is not evidence of recovery.
        provider.async_get_usage.return_value = ClaudeUsageData(windows={"session": UsageWindow("session", "5 hour", 10, None, 18000)})
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(usable).state == "unknown"
        assert not events

        provider.async_get_usage.return_value = usage(0, datetime.now(UTC) + timedelta(days=7))
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(status).state == "available"
        assert hass.states.get(usable).state == "on"
        assert hass.states.get(reset).state == "unknown"
        assert hass.states.get(reset).attributes["reset_status"] == "awaiting_usage"
        observed = hass.states.get(last_reset).state
        assert observed not in ("unknown", "unavailable")
        assert len(events) == 1
        assert hass.states.get(recovery).attributes["event_type"] == "recovered"

        provider.async_get_usage.side_effect = ClaudeConnectionError("offline")
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(usable).state == "unavailable"
        assert hass.states.get(last_reset).state == observed
        refresh = entity(hass, entry, "button", "refresh")
        assert hass.states.get(refresh).state != "unavailable"
        provider.async_get_usage.side_effect = None
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get(last_reset).state == observed
        assert len(events) == 1

        from custom_components.claude_usage.diagnostics import async_get_config_entry_diagnostics
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        assert "secret" not in repr(diagnostics)
        assert diagnostics["entry"]["data"]["access_token"] == "**REDACTED**"


@pytest.mark.asyncio
async def test_numeric_money_polling_and_retry_backoff(hass, enable_custom_integrations):
    assert await async_setup_component(hass, "homeassistant", {})
    entry = MockConfigEntry(domain="claude_usage", title="test", unique_id="test", data={"provider": "remote"})
    entry.add_to_hass(hass)
    provider = AsyncMock()
    data = replace(usage(100, datetime.now(UTC) + timedelta(days=3)),
                   extra_usage=ExtraUsage(True, 12.345678, 100, 12.345678, "USD"))
    provider.async_get_usage.return_value = data
    with patch("custom_components.claude_usage.RemoteAnthropicProvider", return_value=provider):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        spent = entity(hass, entry, "sensor", "extra_used")
        state = hass.states.get(spent)
        assert float(state.state) == 12.345678
        assert state.attributes["unit_of_measurement"] == "USD"
        assert state.attributes["device_class"] == "monetary"
        assert "state_class" not in state.attributes
        assert hass.states.get(entity(hass, entry, "sensor", "account_status")).state == "extra_usage_available"
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"update_interval": 777})
        assert result["type"] == "create_entry"
        await hass.async_block_till_done()
        assert entry.runtime_data.update_interval.total_seconds() == 777
        provider.async_get_usage.side_effect = ClaudeRateLimitError("429", 1200)
        await entry.runtime_data.async_refresh()
        calls = provider.async_get_usage.call_count
        await hass.services.async_call("homeassistant", "update_entity", {"entity_id": spent}, blocking=True)
        assert provider.async_get_usage.call_count == calls


@pytest.mark.asyncio
async def test_queued_recovery_waits_for_start_and_reloads_once(hass, enable_custom_integrations, hass_storage):
    hass.set_state(CoreState.not_running)
    entry = MockConfigEntry(domain="claude_usage", title="test", unique_id="test", data={"provider": "remote"})
    entry.add_to_hass(hass)
    hass_storage[f"claude_usage.{entry.entry_id}.reset_state"] = {
        "version": 1, "minor_version": 1, "key": f"claude_usage.{entry.entry_id}.reset_state",
        "data": {"windows": {}, "event_keys": [], "last_known_usable": False,
                 "pending_events": []},
    }
    provider = AsyncMock()
    provider.async_get_usage.return_value = usage(5, datetime.now(UTC) + timedelta(days=3))
    events = []
    hass.bus.async_listen(EVENT_ACCOUNT_RECOVERED, events.append)
    with patch("custom_components.claude_usage.RemoteAnthropicProvider", return_value=provider):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert not events
        await hass.async_start()
        await hass.async_block_till_done()
        assert len(events) == 1
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert len(events) == 1

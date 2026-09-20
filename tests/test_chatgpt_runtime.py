"""Exercise setup, entities, persistence, and recovery on the deployed HA version."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.helpers import entity_registry as er  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.chatgpt_usage.const import EVENT_ACCOUNT_RECOVERED  # noqa: E402
from custom_components.chatgpt_usage.models import ChatGPTUsageData, UsageWindow  # noqa: E402


def usage(used, reset):
    return ChatGPTUsageData(windows=(UsageWindow(
        "weekly", "Weekly", used, 100 - used, reset, 604800,
        allowed=used < 100, limit_reached=used >= 100,
    ),))


@pytest.mark.asyncio
async def test_runtime_recovery_entities_and_reload(hass, enable_custom_integrations):
    entry = MockConfigEntry(domain="chatgpt_usage", title="Test account", unique_id="test-account",
                            data={"provider": "remote", "access_token": "test-secret"})
    entry.add_to_hass(hass)
    now = datetime.now(UTC)
    provider = AsyncMock()
    provider.async_get_usage.return_value = usage(100, now - timedelta(minutes=1))
    events = []
    hass.bus.async_listen(EVENT_ACCOUNT_RECOVERED, events.append)
    with patch("custom_components.chatgpt_usage.coordinator.RemoteOpenAIProvider", return_value=provider):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await hass.async_start()
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        usable = registry.async_get_entity_id("binary_sensor", "chatgpt_usage", "test-account_usable")
        last_reset = registry.async_get_entity_id("sensor", "chatgpt_usage", "test-account_last_reset")
        weekly_reset = registry.async_get_entity_id("sensor", "chatgpt_usage", "test-account_weekly_reset")
        assert hass.states.get(usable).state == "off"
        assert events == []
        from custom_components.chatgpt_usage.diagnostics import async_get_config_entry_diagnostics
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        assert "test-secret" not in repr(diagnostics)
        assert diagnostics["config"]["access_token"] == "**REDACTED**"

        provider.async_get_usage.return_value = usage(0, datetime.now(UTC) + timedelta(days=7))
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(usable).state == "on"
        assert hass.states.get(weekly_reset).state == "unknown"
        assert hass.states.get(weekly_reset).attributes["reset_status"] == "awaiting_usage"
        observed = hass.states.get(last_reset).state
        assert observed not in ("unknown", "unavailable")
        assert len(events) == 1


        # A failed fetch must make live availability unknown, while preserving the
        # previously observed quota and recovery baseline for the next poll.
        from custom_components.chatgpt_usage.api import ProviderConnectionError
        provider.async_get_usage.side_effect = ProviderConnectionError("test outage")
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(usable).state == "unavailable"
        provider.async_get_usage.side_effect = None
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert len(events) == 1

        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get(last_reset).state == observed
        assert hass.states.get(usable).state == "on"
        assert len(events) == 1


@pytest.mark.asyncio
async def test_history_import_resolves_renamed_entities_without_notifications(hass):
    from types import SimpleNamespace
    from custom_components.chatgpt_usage.history import async_import_reset_history
    from custom_components.chatgpt_usage.models import PersistedWindowState

    entry = MockConfigEntry(domain="chatgpt_usage", unique_id="existing-account")
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    entity = registry.async_get_or_create("sensor", "chatgpt_usage", "existing-account_weekly_usage",
                                         config_entry=entry, suggested_object_id="renamed_account_usage")
    observed = datetime(2026, 9, 19, 9, 16, 42, tzinfo=UTC)
    history = {entity.entity_id: [
        SimpleNamespace(state="100", last_changed=observed - timedelta(hours=1)),
        SimpleNamespace(state="0", last_changed=observed),
    ]}
    windows = {"weekly": PersistedWindowState(used_percent=1)}
    recorder = SimpleNamespace(async_add_executor_job=AsyncMock(return_value=history))
    hass.config.components.add("recorder")
    with patch("homeassistant.components.recorder.get_instance", return_value=recorder):
        assert await async_import_reset_history(hass, entry, windows)
    assert windows["weekly"].last_reset_at == observed.isoformat()
    query = recorder.async_add_executor_job.call_args.args[0]
    assert query.keywords["entity_ids"] == [entity.entity_id]
    assert query.keywords["no_attributes"] is True


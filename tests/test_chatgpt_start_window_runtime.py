"""Exercise the opt-in inference through HA, including persistence and guards."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.exceptions import HomeAssistantError  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.util.file import WriteError  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.chatgpt_usage.api import ProviderConnectionError  # noqa: E402
from custom_components.chatgpt_usage.const import EVENT_ACCOUNT_RECOVERED, EVENT_USAGE_RESET  # noqa: E402
from custom_components.chatgpt_usage.models import ChatGPTUsageData, UsageWindow  # noqa: E402


def reading(used=0, reset=None):
    return ChatGPTUsageData(windows=(UsageWindow(
        "weekly", "Weekly", used, 100 - used,
        reset or datetime.now(UTC) + timedelta(days=7), 604800,
        allowed=used < 100, limit_reached=used >= 100,
    ),))


def entity(hass, domain, suffix):
    return er.async_get(hass).async_get_entity_id(domain, "chatgpt_usage", f"start-test_{suffix}")


@pytest.fixture
def account(hass):
    entry = MockConfigEntry(domain="chatgpt_usage", title="Start test", unique_id="start-test",
                            data={"provider": "remote"})
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def provider():
    provider = AsyncMock()
    provider.async_get_usage.side_effect = lambda: reading()
    provider.async_start_usage_window.return_value = {"model": "test-model", "total_tokens": 8}
    with patch("custom_components.chatgpt_usage.coordinator.RemoteOpenAIProvider", return_value=provider):
        yield provider


@pytest.mark.asyncio
async def test_button_confirms_fixed_deadline_even_at_zero_percent(
    hass, enable_custom_integrations, account, provider, freezer,
):
    events = []
    hass.bus.async_listen(EVENT_ACCOUNT_RECOVERED, events.append)
    hass.bus.async_listen(EVENT_USAGE_RESET, events.append)
    assert await hass.config_entries.async_setup(account.entry_id)
    await hass.async_block_till_done()
    await hass.async_start()
    await hass.async_block_till_done()
    button = entity(hass, "button", "start_weekly_window")
    reset_sensor = entity(hass, "sensor", "weekly_reset")
    assert hass.states.get(button).state != "unavailable"
    assert hass.states.get(reset_sensor).state == "unknown"
    deadline = datetime.now(UTC) + timedelta(days=7)
    provider.async_get_usage.side_effect = lambda: reading(reset=deadline)

    await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    await hass.async_block_till_done()
    provider.async_start_usage_window.assert_awaited_once()
    assert hass.states.get(button).attributes["status"] == "accepted"
    assert hass.states.get(button).state not in ("unknown", "unavailable")
    assert hass.states.get(reset_sensor).state == "unknown"

    freezer.tick(timedelta(seconds=71))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(button).attributes["status"] == "confirmed"
    assert hass.states.get(button).attributes["total_tokens"] == 8
    assert datetime.fromisoformat(hass.states.get(reset_sensor).state) == deadline.replace(microsecond=0)
    assert events == []

    assert await hass.config_entries.async_reload(account.entry_id)
    await hass.async_block_till_done()
    assert entity(hass, "button", "start_weekly_window") == button
    assert hass.states.get(button).attributes["status"] == "confirmed"
    provider.async_start_usage_window.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("new_reading", ["active", "limited", "unknown", "offline"])
async def test_preflight_rejects_changed_or_unreadable_allowance(
    hass, enable_custom_integrations, account, provider, new_reading,
):
    assert await hass.config_entries.async_setup(account.entry_id)
    await hass.async_block_till_done()
    if new_reading == "offline":
        provider.async_get_usage.side_effect = ProviderConnectionError("unavailable")
    else:
        provider.async_get_usage.side_effect = None
        provider.async_get_usage.return_value = {
            "active": reading(1, datetime.now(UTC) + timedelta(days=6)),
            "limited": reading(100),
            "unknown": ChatGPTUsageData(),
        }[new_reading]
    with pytest.raises(HomeAssistantError):
        await account.runtime_data.async_start_usage_window()
    provider.async_start_usage_window.assert_not_awaited()
    assert account.runtime_data.start_usage_result == {}


@pytest.mark.asyncio
async def test_concurrent_press_cannot_send_another_inference(
    hass, enable_custom_integrations, account, provider,
):
    assert await hass.config_entries.async_setup(account.entry_id)
    await hass.async_block_till_done()
    started, finish = asyncio.Event(), asyncio.Event()

    async def inference():
        started.set()
        await finish.wait()
        return {"model": "test-model", "total_tokens": 8}

    provider.async_start_usage_window.side_effect = inference
    task = hass.async_create_task(account.runtime_data.async_start_usage_window())
    await started.wait()
    with pytest.raises(HomeAssistantError, match="already running"):
        await account.runtime_data.async_start_usage_window()
    finish.set()
    await task
    provider.async_start_usage_window.assert_awaited_once()


@pytest.mark.asyncio
async def test_uncertain_request_is_persisted_and_never_automatically_retried(
    hass, enable_custom_integrations, account, provider, freezer,
):
    assert await hass.config_entries.async_setup(account.entry_id)
    await hass.async_block_till_done()
    provider.async_start_usage_window.side_effect = ProviderConnectionError("completion unknown")
    with pytest.raises(HomeAssistantError, match="completion unknown"):
        await account.runtime_data.async_start_usage_window()
    assert account.runtime_data.start_usage_result["status"] == "unconfirmed"
    assert await hass.config_entries.async_reload(account.entry_id)
    await hass.async_block_till_done()
    assert account.runtime_data.start_usage_result["status"] == "unconfirmed"
    with pytest.raises(HomeAssistantError, match="already sent recently"):
        await account.runtime_data.async_start_usage_window()
    freezer.tick(timedelta(minutes=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    await account.runtime_data.async_refresh()
    assert hass.states.get(entity(hass, "button", "start_weekly_window")).state != "unavailable"
    provider.async_start_usage_window.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_inference_without_durable_request_marker(
    hass, enable_custom_integrations, account, provider,
):
    assert await hass.config_entries.async_setup(account.entry_id)
    await hass.async_block_till_done()
    with patch.object(account.runtime_data._store, "_async_write_data", side_effect=WriteError("storage failed")):
        with pytest.raises(HomeAssistantError, match="Could not persist"):
            await account.runtime_data.async_start_usage_window()
    provider.async_start_usage_window.assert_not_awaited()

    account.runtime_data.start_usage_result = {}
    account.runtime_data._store.make_read_only()
    with pytest.raises(HomeAssistantError, match="Could not persist"):
        await account.runtime_data.async_start_usage_window()
    provider.async_start_usage_window.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_provider_has_no_start_button(hass, enable_custom_integrations, account):
    hass.config_entries.async_update_entry(account, data={"provider": "local"})
    provider = AsyncMock()
    provider.async_get_usage.return_value = reading()
    with patch("custom_components.chatgpt_usage.coordinator.LocalCodexProvider", return_value=provider):
        assert await hass.config_entries.async_setup(account.entry_id)
        await hass.async_block_till_done()
        assert entity(hass, "button", "start_weekly_window") is None
        with pytest.raises(HomeAssistantError, match="requires Remote OpenAI"):
            await account.runtime_data.async_start_usage_window()
        provider.async_start_usage_window.assert_not_awaited()

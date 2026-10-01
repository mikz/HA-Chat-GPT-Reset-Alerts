"""Exercise native representations through real Home Assistant entity platforms."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.chatgpt_usage.api import ProviderConnectionError, ProviderRateLimited  # noqa: E402
from custom_components.chatgpt_usage.const import EVENT_ACCOUNT_RECOVERED, EVENT_USAGE_RESET  # noqa: E402
from custom_components.chatgpt_usage.models import ChatGPTUsageData, CreditStatus, UsageWindow  # noqa: E402


def reading(used=100, **kwargs):
    return ChatGPTUsageData(windows=(UsageWindow(
        "weekly", "Weekly", used, 100 - used,
        datetime.now(UTC) + timedelta(days=3), 604800,
        allowed=used < 100, limit_reached=used >= 100,
    ),), **kwargs)


def entity(hass, domain, suffix):
    return er.async_get(hass).async_get_entity_id(domain, "chatgpt_usage", f"native-test_{suffix}")


@pytest.fixture
def account(hass):
    entry = MockConfigEntry(
        domain="chatgpt_usage", title="Native test", unique_id="native-test",
        data={"provider": "remote"},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.asyncio
async def test_native_status_metadata_and_legacy_identity(hass, enable_custom_integrations, account):
    registry = er.async_get(hass)
    # Existing IDs must survive more precise display names and the new default.
    old_usable = registry.async_get_or_create(
        "binary_sensor", "chatgpt_usage", "native-test_usable",
        config_entry=account, suggested_object_id="my_account_usable",
    )
    old_countdown = registry.async_get_or_create(
        "sensor", "chatgpt_usage", "native-test_weekly_time_remaining",
        config_entry=account, suggested_object_id="my_account_countdown",
    )
    provider = AsyncMock()
    provider.async_get_usage.return_value = reading(
        credits=CreditStatus(has_credits=True, balance=12.5), available_reset_credits=2,
    )
    with patch("custom_components.chatgpt_usage.coordinator.RemoteOpenAIProvider", return_value=provider):
        assert await hass.config_entries.async_setup(account.entry_id)
        await hass.async_block_till_done()
        status_id = entity(hass, "sensor", "account_status")
        status = hass.states.get(status_id)
        assert status.state == "credits_available"
        assert status.attributes["device_class"] == "enum"
        assert set(status.attributes["options"]) == {"available", "credits_available", "limited", "blocked"}
        assert hass.states.get(entity(hass, "binary_sensor", "usable")).state == "off"
        assert entity(hass, "binary_sensor", "usable") == old_usable.entity_id
        assert hass.states.get(entity(hass, "binary_sensor", "connected")).attributes["device_class"] == "connectivity"
        assert registry.async_get(old_countdown.entity_id).disabled_by is None
        assert hass.states.get(old_countdown.entity_id) is not None
        for domain, suffix in (("sensor", "credit_balance"), ("sensor", "reset_credits_available"), ("binary_sensor", "credits_available")):
            assert registry.async_get(entity(hass, domain, suffix)).entity_category is None

        for sample, expected in (
            (reading(20), "available"),
            (reading(100), "limited"),
            (reading(100, blocker_reason="spend", credits=CreditStatus(has_credits=True)), "blocked"),
            (ChatGPTUsageData(), "unknown"),
        ):
            provider.async_get_usage.return_value = sample
            await account.runtime_data.async_refresh()
            await hass.async_block_till_done()
            assert hass.states.get(status_id).state == expected
        assert hass.states.get(entity(hass, "binary_sensor", "credits_available")).state == "unknown"
        assert hass.states.get(entity(hass, "binary_sensor", "limit_reached")).state == "unknown"


@pytest.mark.asyncio
async def test_new_countdowns_disabled_and_limit_rules_consistent(hass, enable_custom_integrations, account):
    provider = AsyncMock()
    baseline = reading(20)
    provider.async_get_usage.return_value = replace(baseline, windows=baseline.windows + (
        replace(reading(100).windows[0], id="spark", display_name="Spark", is_main=False),
    ))
    with patch("custom_components.chatgpt_usage.coordinator.RemoteOpenAIProvider", return_value=provider):
        assert await hass.config_entries.async_setup(account.entry_id)
        await hass.async_block_till_done()
        countdown = entity(hass, "sensor", "weekly_time_remaining")
        assert er.async_get(hass).async_get(countdown).disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert hass.states.get(countdown) is None
        assert hass.states.get(entity(hass, "binary_sensor", "limit_reached")).state == "off"
        assert hass.states.get(entity(hass, "binary_sensor", "usable")).state == "on"
        provider.async_get_usage.return_value = replace(
            baseline, windows=(replace(baseline.windows[0], allowed=False, limit_reached=None),),
        )
        await account.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(entity(hass, "binary_sensor", "limit_reached")).state == "on"
        assert hass.states.get(entity(hass, "binary_sensor", "usable")).state == "off"


@pytest.mark.asyncio
async def test_native_events_notify_once_and_preserve_observations_on_failure(hass, enable_custom_integrations, account):
    provider = AsyncMock()
    provider.async_get_usage.return_value = reading()
    notifications = []
    hass.services.async_register("test", "notify", lambda call: notifications.append(call.data))
    with patch("custom_components.chatgpt_usage.coordinator.RemoteOpenAIProvider", return_value=provider):
        assert await hass.config_entries.async_setup(account.entry_id)
        await hass.async_block_till_done()
        recovered = entity(hass, "event", "recovery_event")
        reset_event = entity(hass, "event", "reset_event")
        assert hass.states.get(recovered).state == "unknown"
        assert hass.states.get(reset_event).state == "unknown"
        assert await async_setup_component(hass, "automation", {"automation": [{
            "id": "native_recovery", "alias": "Native recovery", "mode": "queued",
            "triggers": [{"trigger": "event.received", "target": {"entity_id": recovered},
                          "options": {"event_type": ["recovered"]}}],
            "actions": [{"action": "test.notify", "data": {
                "entity": "{{ trigger.entity_id }}",
                "remaining": "{{ trigger.to_state.attributes.remaining_percent }}",
                "entry": "{{ trigger.to_state.attributes.entry_id }}",
            }}],
        }]})
        await hass.async_start()
        await hass.async_block_till_done()
        assert notifications == []

        # Purchasing credits while exhausted is not an ordinary quota recovery.
        provider.async_get_usage.return_value = reading(credits=CreditStatus(has_credits=True))
        await account.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert notifications == []
        provider.async_get_usage.return_value = reading(20)
        await account.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert notifications == [{"entity": recovered, "remaining": 80.0, "entry": account.entry_id}]
        reset_state = hass.states.get(reset_event)
        recovery_state = hass.states.get(recovered)
        assert reset_state.attributes["event_type"] == "reset"
        assert recovery_state.attributes["event_type"] == "recovered"
        observed = datetime.fromisoformat(reset_state.attributes["observed_at"]).replace(microsecond=0).isoformat()
        assert observed == hass.states.get(entity(hass, "sensor", "last_reset")).state

        # Repeated polls, another account, and feature-only resets don't create events.
        await account.runtime_data.async_refresh()
        hass.bus.async_fire(EVENT_ACCOUNT_RECOVERED, {"entry_id": "another-account"})
        hass.bus.async_fire(EVENT_USAGE_RESET, {"entry_id": account.entry_id, "is_main": False})
        await hass.async_block_till_done()
        assert len(notifications) == 1
        assert hass.states.get(reset_event).state == reset_state.state

        provider.async_get_usage.side_effect = ProviderConnectionError("test outage")
        await account.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(entity(hass, "sensor", "account_status")).state == "unavailable"
        assert hass.states.get(entity(hass, "binary_sensor", "connected")).state == "off"
        assert hass.states.get(entity(hass, "sensor", "last_reset")).state == observed
        assert hass.states.get(entity(hass, "sensor", "weekly_last_reset")).state == observed
        assert hass.states.get(entity(hass, "sensor", "last_recovered")).state not in ("unknown", "unavailable")
        refresh = entity(hass, "button", "refresh")
        assert hass.states.get(refresh).state != "unavailable"

        provider.async_get_usage.side_effect = None
        calls = provider.async_get_usage.await_count
        await hass.services.async_call("button", "press", {"entity_id": refresh}, blocking=True)
        await hass.async_block_till_done()
        assert provider.async_get_usage.await_count == calls + 1
        assert hass.states.get(entity(hass, "sensor", "account_status")).state == "available"
        assert len(notifications) == 1

        assert await hass.config_entries.async_reload(account.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get(recovered).state == recovery_state.state
        assert hass.states.get(reset_event).state == reset_state.state
        assert len(notifications) == 1

        # Another genuine recovery remains a distinct event after reloading.
        provider.async_get_usage.return_value = reading(100)
        await account.runtime_data.async_refresh()
        provider.async_get_usage.return_value = reading(10)
        await account.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert len(notifications) == 2
        assert hass.states.get(recovered).state != recovery_state.state


@pytest.mark.asyncio
async def test_manual_refresh_respects_provider_backoff(hass, enable_custom_integrations, account):
    provider = AsyncMock()
    provider.async_get_usage.return_value = reading(20)
    with patch("custom_components.chatgpt_usage.coordinator.RemoteOpenAIProvider", return_value=provider):
        assert await hass.config_entries.async_setup(account.entry_id)
        await hass.async_block_till_done()
        provider.async_get_usage.side_effect = ProviderRateLimited(retry_after=900)
        await account.runtime_data.async_refresh()
        calls = provider.async_get_usage.await_count
        refresh = entity(hass, "button", "refresh")
        assert hass.states.get(refresh).state != "unavailable"
        await hass.services.async_call("button", "press", {"entity_id": refresh}, blocking=True)
        await hass.async_block_till_done()
        assert provider.async_get_usage.await_count == calls

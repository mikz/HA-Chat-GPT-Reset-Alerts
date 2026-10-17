"""Account identity and native polling controls are part of the integration contract."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed  # noqa: E402

from custom_components.claude_usage.models import ClaudeUsageData, UsageWindow  # noqa: E402


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", [False, True])
async def test_native_polling_can_be_disabled_and_manually_updated(hass, enable_custom_integrations, disabled):
    entry = MockConfigEntry(domain="claude_usage", title="test", unique_id="test", data={"provider": "remote"},
                            options={"update_interval": 900}, pref_disable_polling=disabled)
    entry.add_to_hass(hass)
    provider = AsyncMock()
    now = datetime.now(UTC)
    provider.async_get_usage.return_value = ClaudeUsageData(windows={
        "weekly": UsageWindow("weekly", "Weekly", 20, now + timedelta(days=3), 604800)})
    assert await async_setup_component(hass, "homeassistant", {})
    with patch("custom_components.claude_usage.RemoteAnthropicProvider", return_value=provider):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        async_fire_time_changed(hass, now + timedelta(seconds=301))
        await hass.async_block_till_done()
        assert provider.async_get_usage.await_count == 1
        async_fire_time_changed(hass, now + timedelta(seconds=901))
        await hass.async_block_till_done()
        assert provider.async_get_usage.await_count == (1 if disabled else 2)
        provider.async_get_usage.reset_mock()
        sensor = er.async_get(hass).async_get_entity_id("sensor", "claude_usage", f"{entry.entry_id}_weekly_usage")
        await hass.services.async_call("homeassistant", "update_entity", {"entity_id": sensor}, blocking=True)
        await hass.async_block_till_done()
        provider.async_get_usage.assert_awaited_once()


@pytest.mark.asyncio
async def test_setup_names_account_by_email_and_reauth_rejects_different_account(hass, enable_custom_integrations):
    with patch("custom_components.claude_usage.config_flow.async_exchange_oauth_code", AsyncMock(return_value={"access_token": "secret"})), \
         patch("custom_components.claude_usage.config_flow.async_fetch_profile", AsyncMock(return_value={"account": {"uuid": "account-1", "email": "user@example.com", "display_name": "Display name"}})), \
         patch("custom_components.claude_usage.async_setup_entry", AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_init("claude_usage", context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"provider": "remote"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"auth_code": "test-code"})
        assert result["type"] == "create_entry"
        assert result["data"]["account_name"] == "user@example.com"
        entry = result["result"]
        assert entry.unique_id == "remote:account-1"
        await hass.async_block_till_done()
    with patch("custom_components.claude_usage.config_flow.async_exchange_oauth_code", AsyncMock(return_value={"access_token": "new-secret"})), \
         patch("custom_components.claude_usage.config_flow.async_fetch_profile", AsyncMock(return_value={"account": {"uuid": "account-2", "email": "other@example.com"}})):
        result = await hass.config_entries.flow.async_init("claude_usage", context={"source": "reauth", "entry_id": entry.entry_id}, data=entry.data)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"auth_code": "test-code"})
        assert result["type"] == "abort"
        assert result["reason"] == "wrong_account"
        assert entry.data["access_token"] == "secret"

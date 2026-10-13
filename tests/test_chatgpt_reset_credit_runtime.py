"""Verify remote expiry requests and real HA timestamp/attribute behavior."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.helpers import aiohttp_client, entity_registry as er  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.chatgpt_usage.api import (  # noqa: E402
    OpenAIAuthClient, OpenAICredentials, ProviderConnectionError,
)
from custom_components.chatgpt_usage.const import (  # noqa: E402
    EVENT_ACCOUNT_RECOVERED, EVENT_USAGE_RESET, RESET_CREDITS_API_URL, USAGE_API_URL,
)
from custom_components.chatgpt_usage.parsing import parse_openai_usage  # noqa: E402


def payload(count=2, rows=None):
    return {
        "rate_limit": {"primary_window": {"used_percent": 51, "limit_window_seconds": 604800}},
        "rate_limit_reset_credits": {"available_count": count, "credits": rows},
    }


def credentials():
    return OpenAICredentials("test-access", "test-refresh", "test-id", 4102444800, "test-account")


@pytest.mark.asyncio
async def test_remote_reads_separate_details_without_consuming_resets(hass, aioclient_mock):
    future = datetime.now(UTC).replace(microsecond=0) + timedelta(days=8)
    aioclient_mock.get(USAGE_API_URL, json=payload())
    aioclient_mock.get(RESET_CREDITS_API_URL, json={"available_count": 2, "credits": [
        {"status": "available", "expires_at": future.isoformat()},
        {"status": "available", "expires_at": (future + timedelta(days=7)).isoformat()},
    ]})
    client = OpenAIAuthClient(aiohttp_client.async_get_clientsession(hass))
    data, updated = await client.async_get_usage(credentials())
    assert data.next_reset_credit_expiry == future
    assert data.available_reset_credits == 2
    assert updated == credentials()
    assert aioclient_mock.call_count == 2
    assert all(request[0] == "GET" for request in aioclient_mock.mock_calls)
    assert aioclient_mock.mock_calls[1][3]["ChatGPT-Account-Id"] == "test-account"


@pytest.mark.asyncio
@pytest.mark.parametrize("summary", [payload(0), payload(None), payload(1, [{
    "status": "available", "expires_at": "2030-01-01T00:00:00Z",
}])])
async def test_no_extra_request_for_empty_unknown_or_inline_details(hass, aioclient_mock, summary):
    aioclient_mock.get(USAGE_API_URL, json=summary)
    await OpenAIAuthClient(aiohttp_client.async_get_clientsession(hass)).async_get_usage(credentials())
    assert aioclient_mock.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [401, 403, 404, 429, 500, "timeout", "connection", "malformed"])
async def test_optional_details_failure_does_not_break_usage_and_backs_off(hass, aioclient_mock, failure):
    aioclient_mock.get(USAGE_API_URL, json=payload())
    options = (
        {"exc": TimeoutError()} if failure == "timeout"
        else {"exc": aiohttp.ClientConnectionError()} if failure == "connection"
        else {"json": {"credits": "not a list"}} if failure == "malformed"
        else {"status": failure, "headers": {"Retry-After": "1200"}}
    )
    aioclient_mock.get(RESET_CREDITS_API_URL, **options)
    client = OpenAIAuthClient(aiohttp_client.async_get_clientsession(hass))
    for _ in range(2):
        data, _ = await client.async_get_usage(credentials())
        assert data.available_reset_credits == 2
        assert data.window("weekly").used_percent == 51
        assert data.next_reset_credit_expiry is None
    assert aioclient_mock.call_count == 3  # Two usage GETs, one optional detail GET.


@pytest.mark.asyncio
async def test_native_expiry_and_count_attribute_follow_consumption_and_failure(hass, enable_custom_integrations):
    entry = MockConfigEntry(domain="chatgpt_usage", title="Banked test", unique_id="banked-test",
                            data={"provider": "remote"})
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old_count = registry.async_get_or_create("sensor", "chatgpt_usage", "banked-test_reset_credits_available",
                                            config_entry=entry, suggested_object_id="existing_banked_resets")
    future = datetime.now(UTC).replace(microsecond=0) + timedelta(days=8)
    later = future + timedelta(days=7)
    rows = [{"status": "available", "expires_at": date.isoformat()} for date in (future, later)]
    provider = AsyncMock()
    provider.async_get_usage.return_value = parse_openai_usage(payload(2, rows))
    events = []
    hass.bus.async_listen(EVENT_ACCOUNT_RECOVERED, events.append)
    hass.bus.async_listen(EVENT_USAGE_RESET, events.append)
    with patch("custom_components.chatgpt_usage.coordinator.RemoteOpenAIProvider", return_value=provider):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_start()
        await hass.async_block_till_done()
        expiry = registry.async_get_entity_id("sensor", "chatgpt_usage", "banked-test_next_banked_reset_expiry")
        count = old_count.entity_id
        assert hass.states.get(expiry).state == future.isoformat()
        assert hass.states.get(expiry).attributes["device_class"] == "timestamp"
        assert "state_class" not in hass.states.get(expiry).attributes
        assert hass.states.get(count).state == "2"
        assert hass.states.get(count).attributes["next_expiry"] == future.isoformat()

        # A replacement grant changes expiry even if the count stays the same.
        for reading, expected in ((payload(2, [rows[1], rows[1]]), later.isoformat()),
                                  (payload(1, [rows[1]]), later.isoformat()),
                                  (payload(0, []), "unknown"),
                                  (payload(2), "unknown")):
            provider.async_get_usage.return_value = parse_openai_usage(reading)
            await entry.runtime_data.async_refresh()
            await hass.async_block_till_done()
            assert hass.states.get(expiry).state == expected
            assert hass.states.get(count).attributes["next_expiry"] == (None if expected == "unknown" else expected)
        assert hass.states.get(count).state == "2"
        assert hass.states.get(count).attributes["expiry_status"] == "not_reported"
        assert events == []

        provider.async_get_usage.side_effect = ProviderConnectionError("test outage")
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(count).state == "unavailable"
        assert hass.states.get(expiry).state == "unavailable"
        provider.async_get_usage.side_effect = None
        provider.async_get_usage.return_value = parse_openai_usage(payload(1, [rows[1]]))
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get(expiry).state == later.isoformat()
        assert registry.async_get_entity_id("sensor", "chatgpt_usage", "banked-test_reset_credits_available") == count
        assert events == []

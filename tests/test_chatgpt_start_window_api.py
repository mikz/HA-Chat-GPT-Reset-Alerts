"""Bounded subscription inference and refresh safety without real credentials."""

import asyncio
from dataclasses import replace
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

import aiohttp  # noqa: E402
from homeassistant.helpers import aiohttp_client  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.chatgpt_usage.api import (  # noqa: E402
    OpenAIAuthClient, OpenAICredentials, ProviderAuthError, ProviderConnectionError,
    ProviderError, ProviderRateLimited, ProviderSchemaError, RemoteOpenAIProvider,
    _async_start_window_result, _start_window_event, credentials_to_entry_data,
)
from custom_components.chatgpt_usage.const import (  # noqa: E402
    CODEX_RESPONSES_API_URL, START_WINDOW_MAX_RESPONSE_BYTES, START_WINDOW_MODEL,
)


def credentials():
    return OpenAICredentials("test-access", "test-refresh", "test-id", 4102444800, "test-account")


def completion(**extra):
    return {"type": "response.completed", "response": {
        "id": "test-response", "status": "completed", "usage": {"total_tokens": 7}, **extra,
    }}


def sse(event):
    return "data: " + json.dumps(event) + "\n\n"


@pytest.mark.asyncio
async def test_one_subscription_request_uses_account_auth_and_disables_tools(hass, aioclient_mock):
    aioclient_mock.post(CODEX_RESPONSES_API_URL,
                        text=sse(completion(output=[{"text": "private-output"}])),
                        headers={"Content-Type": "text/event-stream; charset=utf-8"})
    result = await OpenAIAuthClient(aiohttp_client.async_get_clientsession(hass)).async_start_usage_window(credentials())
    assert result == {"model": START_WINDOW_MODEL, "total_tokens": 7}
    assert aioclient_mock.call_count == 1
    request = aioclient_mock.mock_calls[0]
    assert request[0] == "POST"
    assert request[3]["Authorization"] == "Bearer test-access"
    assert request[3]["ChatGPT-Account-Id"] == "test-account"
    assert request[3]["Accept"] == "text/event-stream"
    body = request[2]
    assert body["model"] == START_WINDOW_MODEL
    assert body["tools"] == [] and body["tool_choice"] == "none"
    assert body["store"] is False and body["stream"] is True
    assert body["parallel_tool_calls"] is False
    assert body["reasoning"] == {"effort": "low"}
    assert body["input"][-1]["content"] == [{"type": "input_text", "text": "Reply OK."}]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,error", [
    (302, ProviderError), (400, ProviderError), (401, ProviderAuthError),
    (403, ProviderAuthError), (429, ProviderRateLimited), (500, ProviderConnectionError),
])
async def test_http_failures_do_not_retry_or_expose_provider_body(hass, aioclient_mock, status, error):
    aioclient_mock.post(CODEX_RESPONSES_API_URL, status=status, text="private-provider-body",
                        headers={"Retry-After": "120", "Location": "https://example.org/"})
    with pytest.raises(error) as raised:
        await OpenAIAuthClient(aiohttp_client.async_get_clientsession(hass)).async_start_usage_window(credentials())
    assert "private" not in str(raised.value)
    assert aioclient_mock.call_count == 1
    if status == 429:
        assert raised.value.retry_after == 120


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError("private"), aiohttp.ClientConnectionError("private")])
async def test_uncertain_network_failure_never_retries(hass, aioclient_mock, failure):
    aioclient_mock.post(CODEX_RESPONSES_API_URL, exc=failure)
    with pytest.raises(ProviderConnectionError, match="usage may have been consumed") as raised:
        await OpenAIAuthClient(aiohttp_client.async_get_clientsession(hass)).async_start_usage_window(credentials())
    assert raised.value.__suppress_context__
    assert aioclient_mock.call_count == 1


@pytest.mark.asyncio
async def test_non_sse_response_is_rejected(hass, aioclient_mock):
    aioclient_mock.post(CODEX_RESPONSES_API_URL, json=completion())
    with pytest.raises(ProviderSchemaError):
        await OpenAIAuthClient(aiohttp_client.async_get_clientsession(hass)).async_start_usage_window(credentials())


@pytest.mark.parametrize("event", [
    {"type": "error", "error": {"message": "private"}},
    {"type": "response.failed", "response": {"error": "private"}},
    {"type": "response.incomplete", "response": {"incomplete_details": "private"}},
])
def test_terminal_sse_errors_are_sanitized(event):
    with pytest.raises(ProviderError, match="failed or was incomplete") as raised:
        _start_window_event(json.dumps(event).encode())
    assert "private" not in str(raised.value)


@pytest.mark.parametrize("data", [b"private", b"\xff", b"[]", b"{}", b'{"type":7}',
                                 b'{"type":"response.completed"}',
                                 json.dumps(completion(id=None)).encode(),
                                 json.dumps(completion(status="failed")).encode()])
def test_invalid_sse_events_and_completions_are_rejected(data):
    with pytest.raises(ProviderSchemaError):
        _start_window_event(data)


@pytest.mark.parametrize("usage,total", [(None, None), ({"total_tokens": True}, None),
                                       ({"total_tokens": -1}, None), ({"total_tokens": 2}, 2)])
def test_only_valid_token_count_is_retained(usage, total):
    assert _start_window_event(json.dumps(completion(usage=usage)).encode()) == {
        "model": START_WINDOW_MODEL, "total_tokens": total,
    }


async def chunks(values):
    for value in values:
        yield value


@pytest.mark.asyncio
async def test_split_crlf_multiline_events_and_comment_keepalives():
    body = (": heartbeat\r\nevent: response.completed\r\ndata: {\r\ndata: "
            + json.dumps(completion())[1:] + "\r\n\r\n").encode()
    response = MagicMock()
    response.content.iter_chunked.return_value = chunks([body[i:i+3] for i in range(0, len(body), 3)])
    assert await _async_start_window_result(response) == {"model": START_WINDOW_MODEL, "total_tokens": 7}


@pytest.mark.asyncio
@pytest.mark.parametrize("body,error", [
    (sse({"type": "response.created"}).encode(), ProviderConnectionError),
    (b"data: " + b"x" * START_WINDOW_MAX_RESPONSE_BYTES, ProviderSchemaError),
    (sse(completion()).encode().rstrip(b"\n"), ProviderConnectionError),
])
async def test_capped_or_unfinished_stream_fails(body, error):
    response = MagicMock()
    response.content.iter_chunked.return_value = chunks([body])
    with pytest.raises(error):
        await _async_start_window_result(response)


@pytest.mark.asyncio
async def test_refreshed_credentials_saved_before_failed_inference(hass):
    old = replace(credentials(), expires_at=1)
    updated = replace(credentials(), access_token="new-access", refresh_token="new-refresh")
    entry = MockConfigEntry(domain="chatgpt_usage", data=credentials_to_entry_data(old))
    entry.add_to_hass(hass)
    provider = RemoteOpenAIProvider(hass, entry)
    provider.client.async_refresh = AsyncMock(return_value=updated)

    async def fail(current):
        assert current == updated
        assert entry.data["refresh_token"] == "new-refresh"
        raise ProviderConnectionError("uncertain completion")

    provider.client.async_start_usage_window = AsyncMock(side_effect=fail)
    with pytest.raises(ProviderConnectionError):
        await provider.async_start_usage_window()
    provider.client.async_refresh.assert_awaited_once_with(old)
    provider.client.async_start_usage_window.assert_awaited_once_with(updated)


@pytest.mark.asyncio
async def test_provider_serializes_usage_and_inference(hass):
    entry = MockConfigEntry(domain="chatgpt_usage", data=credentials_to_entry_data(credentials()))
    entry.add_to_hass(hass)
    provider = RemoteOpenAIProvider(hass, entry)
    reading = asyncio.Event()
    release = asyncio.Event()

    async def usage(current):
        reading.set()
        await release.wait()
        return "usage-data", current

    provider.client.async_get_usage = AsyncMock(side_effect=usage)
    provider.client.async_start_usage_window = AsyncMock(return_value={"model": START_WINDOW_MODEL})
    usage_task = asyncio.create_task(provider.async_get_usage())
    await reading.wait()
    start_task = asyncio.create_task(provider.async_start_usage_window())
    await asyncio.sleep(0)
    provider.client.async_start_usage_window.assert_not_awaited()
    release.set()
    assert await usage_task == "usage-data"
    assert await start_task == {"model": START_WINDOW_MODEL}

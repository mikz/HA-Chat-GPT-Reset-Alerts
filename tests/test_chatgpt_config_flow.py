"""Show the account before accepting a device login."""
import base64
import json
from unittest.mock import AsyncMock, patch

import pytest
pytest.importorskip("pytest_homeassistant_custom_component")

from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.chatgpt_usage.api import (  # noqa: E402
    DeviceCode, credentials_from_entry, credentials_from_token_response,
    credentials_to_entry_data,
)


def token(payload):
    return 'header.' + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=') + '.signature'


def credentials(user='user-1'):
    return credentials_from_token_response({
        'access_token': token({'chatgpt_account_id': 'workspace-1', 'exp': 9999999999}),
        'refresh_token': 'secret',
        'id_token': token({'email': 'user@example.com', 'https://api.openai.com/auth': {'chatgpt_user_id': user}}),
    })


def test_email_roundtrip_and_legacy_entry():
    creds = credentials()
    data = credentials_to_entry_data(creds)
    assert data['email'] == 'user@example.com'
    del data['email']
    assert credentials_from_entry(MockConfigEntry(domain='chatgpt_usage', data=data)).email == creds.email


@pytest.mark.asyncio
async def test_device_login_confirms_identity(hass, enable_custom_integrations):
    client = AsyncMock()
    client.async_request_device_code.return_value = DeviceCode('id', 'CODE', 5)
    client.async_exchange_device_code.return_value = credentials()
    client.async_get_accounts.return_value = ()
    client.async_get_usage.return_value = (None, credentials())
    with patch('custom_components.chatgpt_usage.config_flow.OpenAIAuthClient', return_value=client), patch('custom_components.chatgpt_usage.async_setup_entry', AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_init('chatgpt_usage', context={'source': 'user'})
        result = await hass.config_entries.flow.async_configure(result['flow_id'], {'provider': 'remote'})
        result = await hass.config_entries.flow.async_configure(result['flow_id'], {'confirm': False})
        client.async_poll_device_code.assert_not_called()
        result = await hass.config_entries.flow.async_configure(result['flow_id'], {'confirm': True})
        assert result['step_id'] == 'confirm_account'
        assert result['description_placeholders']['account'] == 'user@example.com'
        result = await hass.config_entries.flow.async_configure(result['flow_id'], {})
        assert result['type'] == 'create_entry'
        assert result['title'] == 'ChatGPT Usage (user@example.com)'
        await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_reauth_shows_expected_account_and_rejects_other_user(hass, enable_custom_integrations):
    entry = MockConfigEntry(domain='chatgpt_usage', title='ChatGPT Usage', unique_id='remote:workspace-1:user-1', data={'provider': 'remote', **credentials_to_entry_data(credentials())})
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_request_device_code.return_value = DeviceCode('id', 'CODE', 5)
    client.async_exchange_device_code.return_value = credentials('other-user')
    client.async_get_accounts.return_value = ()
    with patch('custom_components.chatgpt_usage.config_flow.OpenAIAuthClient', return_value=client):
        result = await hass.config_entries.flow.async_init('chatgpt_usage', context={'source': 'reauth', 'entry_id': entry.entry_id}, data=entry.data)
        assert result['step_id'] == 'reauth_device'
        assert result['description_placeholders']['account'] == 'user@example.com'
        result = await hass.config_entries.flow.async_configure(result['flow_id'], {'confirm': True})
        assert result['reason'] == 'wrong_account'
        client.async_get_usage.assert_not_called()

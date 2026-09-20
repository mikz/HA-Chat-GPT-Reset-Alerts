from custom_components.chatgpt_usage.security import redact_mapping
from types import MappingProxyType


def test_redacts_all_credentials_recursively():
    result = redact_mapping(
        {
            "access_token": "a",
            "refresh_token": "b",
            "id_token": "c",
            "api_key": "d",
            "nested": {"Authorization": "Bearer secret", "safe": 123},
        }
    )
    assert result["access_token"] == "**REDACTED**"
    assert result["refresh_token"] == "**REDACTED**"
    assert result["id_token"] == "**REDACTED**"
    assert result["api_key"] == "**REDACTED**"
    assert result["nested"]["Authorization"] == "**REDACTED**"
    assert result["nested"]["safe"] == 123


def test_config_entry_mapping_proxy_does_not_leak_credentials():
    result = redact_mapping(MappingProxyType({
        "access_token": "access-secret",
        "nested": MappingProxyType({"refresh_token": "refresh-secret"}),
        "email": "account@example.test",
    }))
    assert result["access_token"] == "**REDACTED**"
    assert result["nested"]["refresh_token"] == "**REDACTED**"
    assert "secret" not in repr(result)

"""Banked expiry normalization must not invent deadlines or available counts."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from custom_components.chatgpt_usage.parsing import (
    parse_app_server_rate_limits,
    parse_helper_usage,
    parse_openai_usage,
    parse_reset_credits,
)

OBSERVED = datetime(2026, 10, 14, tzinfo=UTC)
FIRST = datetime(2026, 10, 22, 20, 32, 58, tzinfo=UTC)
SECOND = datetime(2026, 10, 29, 18, 51, 57, tzinfo=UTC)


def remote_payload(summary):
    return {
        "rate_limit": {"primary_window": {
            "used_percent": 51, "reset_at": 1792506948, "limit_window_seconds": 604800,
        }},
        "rate_limit_reset_credits": summary,
    }


@pytest.mark.parametrize("source", ["remote", "local", "legacy_helper"])
def test_normalizes_available_expiries_across_both_provider_contracts(source):
    summary = {"available_count": 2, "credits": [
        {"id": "later", "status": "available", "expires_at": SECOND.isoformat(),
         "profile_user_id": "private-profile", "description": "private-description"},
        {"id": "first", "status": "available", "expires_at": FIRST.isoformat()},
        {"id": "used", "status": "redeemed", "expires_at": "2026-10-15T00:00:00Z"},
        {"id": "expired", "status": "expired", "expires_at": "2026-10-16T00:00:00Z"},
        {"id": "first", "status": "available", "expires_at": FIRST.isoformat()},
        {"status": "redeeming", "expires_at": "2026-10-17T00:00:00Z"},
    ]}
    if source == "local":
        summary["availableCount"] = summary.pop("available_count")
        for row in summary["credits"]:
            row["expiresAt"] = int(datetime.fromisoformat(row.pop("expires_at")).timestamp())
        data = parse_app_server_rate_limits({
            "rateLimits": {"primary": {"usedPercent": 51, "windowDurationMins": 10080}},
            "rateLimitResetCredits": summary,
        })
    elif source == "legacy_helper":
        data = parse_helper_usage({"api_version": 1, "usage": remote_payload(summary)})
    else:
        data = parse_openai_usage(remote_payload(summary))
    data = replace(data, last_updated=OBSERVED)
    assert data.available_reset_credits == 2
    assert data.next_reset_credit_expiry == FIRST
    assert data.reset_credit_expiry_attributes() == {
        "next_expiry": FIRST.isoformat(),
        "expiry_dates": [FIRST.isoformat(), SECOND.isoformat()],
        "expiry_details_complete": True,
        "expiry_status": "scheduled",
    }
    assert "private-" not in repr(data)
    assert "later" not in repr(data)


@pytest.mark.parametrize(("count", "rows", "expected", "status", "complete"), [
    (0, None, None, "none_available", True),
    (0, [{"status": "available", "expires_at": FIRST.isoformat()}], None, "none_available", True),
    (2, None, None, "not_reported", False),
    (2, [], None, "not_reported", False),
    (2, [{"status": "available", "expires_at": FIRST.isoformat()}], FIRST, "partial", False),
    (None, [{"status": "available", "expires_at": FIRST.isoformat()}], FIRST, "partial", False),
    (2, [{"status": "available", "expires_at": FIRST.isoformat()},
         {"status": "available", "expires_at": None}], FIRST, "partial", False),
    (2, [{"status": "available", "expires_at": "2026-10-01T00:00:00Z"},
         {"status": "available", "expires_at": SECOND.isoformat()}], SECOND, "partial", False),
    (1, [{"status": "available", "expires_at": None}], None, "not_reported", False),
    (1, [{"status": "available", "expires_at": "nonsense"}], None, "not_reported", False),
    (1, [{"status": "available", "expires_at": True}], None, "not_reported", False),
    (1, [{"status": "available", "expires_at": 1e100}], None, "not_reported", False),
    (1, [{"expires_at": FIRST.isoformat()}], None, "not_reported", False),
    (1, [False, None, "bad row"], None, "not_reported", False),
])
def test_missing_capped_invalid_or_stale_details_preserve_uncertainty(count, rows, expected, status, complete):
    data = replace(parse_openai_usage(remote_payload({"available_count": count, "credits": rows})),
                   last_updated=OBSERVED)
    assert data.available_reset_credits == count
    assert data.next_reset_credit_expiry == expected
    attributes = data.reset_credit_expiry_attributes()
    assert attributes["expiry_status"] == status
    assert attributes["expiry_details_complete"] is complete


def test_bounds_details_without_replacing_the_authoritative_count():
    count, expiries = parse_reset_credits({"available_count": 101, "credits": [
        {"id": str(i), "status": "available", "expires_at": FIRST.isoformat()} for i in range(101)
    ]})
    assert count == 101
    assert len(expiries) == 100


def test_rolls_to_next_expiry_only_on_a_new_provider_observation():
    data = replace(parse_openai_usage(remote_payload({"available_count": 2, "credits": [
        {"status": "available", "expires_at": FIRST.isoformat()},
        {"status": "available", "expires_at": SECOND.isoformat()},
    ]})), last_updated=OBSERVED)
    assert data.next_reset_credit_expiry == FIRST
    assert replace(data, last_updated=FIRST).next_reset_credit_expiry == SECOND

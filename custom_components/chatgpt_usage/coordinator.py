"""DataUpdateCoordinator and reset persistence for ChatGPT Usage."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    LocalCodexProvider,
    ProviderAuthError,
    ProviderConnectionError,
    ProviderError,
    ProviderRateLimited,
    ProviderSchemaError,
    RemoteOpenAIProvider,
    UsageProvider,
)
from .const import (
    CONF_PROVIDER,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    EVENT_ACCOUNT_RECOVERED,
    EVENT_USAGE_RESET,
    PROVIDER_LOCAL,
    STORAGE_KEY_TEMPLATE,
    STORAGE_VERSION,
)
from .models import ChatGPTUsageData, PersistedWindowState, UsageWindow, parse_datetime
from .observation import account_status, stabilize_window
from .reset import detect_reset

_LOGGER = logging.getLogger(__name__)


class ChatGPTUsageCoordinator(DataUpdateCoordinator[ChatGPTUsageData]):
    """Coordinate polling, persisted observations, and deduplicated recovery events."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        interval = int(entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}:{entry.entry_id}",
            update_interval=timedelta(seconds=interval),
            config_entry=entry,
            always_update=False,
        )
        self.provider: UsageProvider = (
            LocalCodexProvider(hass, entry)
            if entry.data.get(CONF_PROVIDER) == PROVIDER_LOCAL
            else RemoteOpenAIProvider(hass, entry)
        )
        self._store = Store[dict[str, Any]](
            hass, STORAGE_VERSION, STORAGE_KEY_TEMPLATE.format(entry_id=entry.entry_id)
        )
        self._window_state: dict[str, PersistedWindowState] = {}
        self._backoff_until: datetime | None = None
        self.last_successful_update: datetime | None = None
        self.last_error_category: str | None = None
        self.account_status = "incomplete"
        self._last_known_usable: bool | None = None
        self.last_recovered_at: datetime | None = None
        self._pending_events: list[dict[str, Any]] = []
        self._ready = False
        self._history_imported = False

    async def async_initialize(self) -> None:
        stored = await self._store.async_load() or {}
        raw = stored.get("windows") or {}
        if isinstance(raw, dict):
            self._window_state = {
                str(window_id): PersistedWindowState.from_dict(state)
                for window_id, state in raw.items()
                if isinstance(state, dict)
            }
            # Version 1 stored no main/additional distinction.
            for window_id, state in self._window_state.items():
                if "is_main" not in raw[window_id]:
                    state.is_main = window_id in ("weekly", "five_hour") or window_id.startswith("codex_")
        self._last_known_usable = stored.get("last_known_usable")
        if self._last_known_usable is None:
            main = [s for s in self._window_state.values() if s.is_main]
            if main and all(s.used_percent is not None for s in main):
                self._last_known_usable = not any(s.used_percent >= 100 for s in main)
        self.last_recovered_at = parse_datetime(stored.get("last_recovered_at"))
        self._pending_events = stored.get("pending_events", [])
        self._history_imported = stored.get("history_imported", False)
        if not self._history_imported:
            await self._async_import_history()
            if self._history_imported:
                await self._async_save_state()

    async def _async_update_data(self) -> ChatGPTUsageData:
        now = datetime.now(UTC)
        if self._backoff_until and now < self._backoff_until:
            seconds = int((self._backoff_until - now).total_seconds())
            raise UpdateFailed(f"Provider backoff active for another {seconds} seconds")
        try:
            data = await self.provider.async_get_usage()
        except ProviderAuthError as err:
            self.last_error_category = "authentication"
            raise ConfigEntryAuthFailed(str(err)) from err
        except ProviderRateLimited as err:
            self.last_error_category = "rate_limited"
            backoff = max(300, int(err.retry_after or 900))
            self._backoff_until = now + timedelta(seconds=min(backoff, 14400))
            raise UpdateFailed(
                f"OpenAI usage endpoint rate limited requests; backing off for {backoff} seconds"
            ) from err
        except ProviderSchemaError as err:
            self.last_error_category = "schema"
            raise UpdateFailed(str(err)) from err
        except ProviderConnectionError as err:
            self.last_error_category = "connection"
            raise UpdateFailed(str(err)) from err
        except ProviderError as err:
            self.last_error_category = "provider"
            raise UpdateFailed(str(err)) from err

        self._backoff_until = None
        self.last_error_category = None
        self.last_successful_update = data.last_updated
        return await self._process_resets(data, datetime.now(UTC))

    async def _process_resets(self, data: ChatGPTUsageData, now: datetime) -> ChatGPTUsageData:
        data = replace(data, windows=tuple(
            stabilize_window(window, self._window_state.get(window.id), now)
            for window in data.windows
        ))
        self.account_status = account_status(data, self._window_state)
        changed = False
        for window in data.windows:
            previous = self._window_state.get(window.id)
            if previous is None:
                self._window_state[window.id] = PersistedWindowState.from_window(window)
                changed = True
                continue
            detection = detect_reset(previous, window, now)
            last_event_key = previous.last_event_key
            last_reset_at = previous.last_reset_at
            if detection is not None:
                last_event_key = detection.event_key
                last_reset_at = now.isoformat()
                event = {
                    **_reset_event_data(previous, window, detection.confidence),
                    "entry_id": self.entry.entry_id,
                    "entry_title": self.entry.title,
                    "observed_at": last_reset_at,
                }
                self._pending_events.append({"type": EVENT_USAGE_RESET, "data": event})
                _LOGGER.info(
                    "Detected ChatGPT/Codex usage reset for %s (%s)",
                    window.display_name,
                    detection.confidence,
                )
            new_state = PersistedWindowState.from_window(window, last_event_key, last_reset_at)
            if new_state != previous:
                self._window_state[window.id] = new_state
                changed = True
        if self.account_status != "incomplete":
            usable = self.account_status == "available"
            if usable and self._last_known_usable is False:
                self.last_recovered_at = now
                self._pending_events.append({"type": EVENT_ACCOUNT_RECOVERED, "data": {
                    "entry_id": self.entry.entry_id,
                    "entry_title": self.entry.title,
                    "observed_at": now.isoformat(),
                    "remaining_percent": min(
                        w.remaining_percent for w in data.windows
                        if w.is_main and w.remaining_percent is not None
                    ),
                    "windows": [w.to_safe_dict() for w in data.windows if w.is_main],
                }})
            if usable != self._last_known_usable:
                self._last_known_usable = usable
                changed = True
        if changed or self._pending_events:
            await self._async_save_state()
        if self._ready:
            await self._async_publish_events()
        return data

    def last_reset_at(self, window_id: str | None = None) -> datetime | None:
        states = (
            [self._window_state[window_id]] if window_id in self._window_state
            else [] if window_id is not None
            else [s for s in self._window_state.values() if s.is_main]
        )
        dates = [date for state in states if (date := parse_datetime(state.last_reset_at))]
        return max(dates, default=None)

    async def _async_save_state(self) -> None:
        await self._store.async_save({
            "windows": {key: value.as_dict() for key, value in self._window_state.items()},
            "last_known_usable": self._last_known_usable,
            "last_recovered_at": self.last_recovered_at.isoformat() if self.last_recovered_at else None,
            "pending_events": self._pending_events,
            "history_imported": self._history_imported,
        })

    async def async_start(self, hass: HomeAssistant) -> None:
        """Release persisted events only after entities and HA automations have started."""
        self._ready = True
        await self._async_publish_events()

    async def _async_publish_events(self) -> None:
        if not self._pending_events:
            return
        pending = self._pending_events
        self._pending_events = []
        try:
            # Persist deduplication before dispatch; restarting must not repeat alerts.
            await self._async_save_state()
        except Exception:
            self._pending_events = pending
            raise
        for event in pending:
            self.hass.bus.async_fire(event["type"], event["data"])

    async def _async_import_history(self) -> None:
        """Recover observed refill times from existing Recorder history on upgrade."""
        if not self._window_state:
            self._history_imported = True
            return
        from .history import async_import_reset_history

        self._history_imported = await async_import_reset_history(self.hass, self.entry, self._window_state)

    async def async_shutdown(self) -> None:
        await self.provider.async_close()


def _reset_event_data(previous: PersistedWindowState, current: UsageWindow, confidence: str) -> dict[str, Any]:
    return {
        "window_id": current.id,
        "window": current.display_name,
        "limit_name": current.limit_name,
        "previous_used_percent": previous.used_percent,
        "new_used_percent": current.used_percent,
        "previous_remaining_percent": previous.remaining_percent,
        "new_remaining_percent": current.remaining_percent,
        "remaining_percent": current.remaining_percent,
        "previous_reset_at": previous.reset_at,
        "new_reset_at": current.reset_at.isoformat() if current.reset_at else None,
        "confidence": confidence,
    }

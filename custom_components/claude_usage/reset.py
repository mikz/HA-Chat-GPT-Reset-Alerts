"""Persist observations and publish deduplicated reset and recovery events."""

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, EVENT_ACCOUNT_RECOVERED, EVENT_USAGE_RESET, RESET_STORE_VERSION
from .models import ClaudeUsageData, parse_datetime
from .observation import ordinary_available, stabilize_window
from .reset_logic import WindowSnapshot, reset_detected


class ResetTracker:
    """Retain the existing storage key and add optional observation fields."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.store = Store(hass, RESET_STORE_VERSION, f"{DOMAIN}.{entry_id}.reset_state")
        self._loaded = False
        self._ready = False
        self._state: dict[str, Any] = {"windows": {}, "event_keys": [], "pending_events": []}
        self.usable: bool | None = None

    async def async_load(self) -> None:
        if self._loaded:
            return
        stored = await self.store.async_load()
        if isinstance(stored, dict):
            self._state.update(stored)
        self._loaded = True

    def last_reset_at(self, window_id: str | None = None) -> datetime | None:
        dates = [date for wid, state in self._state["windows"].items()
                 if (wid == window_id if window_id else wid in ("session", "weekly"))
                 and (date := parse_datetime(state.get("last_reset_at")))]
        return max(dates, default=None)

    @property
    def last_recovered_at(self) -> datetime | None:
        return parse_datetime(self._state.get("last_recovered_at"))

    async def async_process(self, data: ClaudeUsageData) -> ClaudeUsageData:
        await self.async_load()
        previous = self._state["windows"]
        now = datetime.now(UTC)
        data = replace(data, windows={wid: stabilize_window(w, previous.get(wid, {}), now)
                                      for wid, w in data.windows.items()})
        self.usable = ordinary_available(data, previous)
        event_keys = self._state["event_keys"]
        pending = self._state["pending_events"]
        for wid, window in data.windows.items():
            saved = previous.get(wid)
            last_reset = saved.get("last_reset_at") if saved else None
            if saved:
                snapshot = WindowSnapshot(saved.get("used_percent"), parse_datetime(saved.get("reset_at")))
                marker = window.reset_at.isoformat() if window.reset_at else data.last_updated.strftime("%Y%m%dT%H")
                event_key = f"{wid}:{marker}"
                if reset_detected(snapshot, window, now=now) and event_key not in event_keys:
                    last_reset = now.isoformat()
                    pending.append({"type": EVENT_USAGE_RESET, "data": {
                        "entry_id": self.entry_id, "window_id": wid, "window": window.display_name,
                        "is_main": window.is_main, "kind": window.kind, "model": window.model,
                        "surface": window.surface, "previous_used_percent": snapshot.used_percent,
                        "new_used_percent": window.used_percent, "remaining_percent": window.remaining_percent,
                        "previous_reset_at": saved.get("reset_at"),
                        "new_reset_at": window.reset_at.isoformat() if window.reset_at else None,
                        "observed_at": last_reset, "detected_at": last_reset,
                    }})
                    event_keys.append(event_key)
            previous[wid] = {
                "used_percent": window.used_percent,
                "reset_at": window.reset_at.isoformat() if window.reset_at else None,
                "last_reset_at": last_reset,
            }
        if self.usable is not None:
            if self.usable and self._state.get("last_known_usable") is False:
                self._state["last_recovered_at"] = now.isoformat()
                pending.append({"type": EVENT_ACCOUNT_RECOVERED, "data": {
                    "entry_id": self.entry_id, "observed_at": now.isoformat(),
                    "remaining_percent": min(w.remaining_percent for w in data.windows.values()
                                             if w.is_main and w.remaining_percent is not None),
                }})
            self._state["last_known_usable"] = self.usable
        self._state["event_keys"] = event_keys[-100:]
        await self.store.async_save(self._state)
        if self._ready:
            await self._async_publish()
        return data

    async def async_start(self, hass: HomeAssistant) -> None:
        self._ready = True
        await self._async_publish()

    async def _async_publish(self) -> None:
        pending = self._state["pending_events"]
        if not pending:
            return
        self._state["pending_events"] = []
        try:
            # Persist before dispatch so reloads cannot replay notifications.
            await self.store.async_save(self._state)
        except Exception:
            self._state["pending_events"] = pending
            raise
        for event in pending:
            self.hass.bus.async_fire(event["type"], event["data"])

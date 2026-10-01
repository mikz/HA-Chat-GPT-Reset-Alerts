"""Native event history for observed ordinary allowance resets and recoveries."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import EVENT_ACCOUNT_RECOVERED, EVENT_USAGE_RESET
from .coordinator import ChatGPTUsageCoordinator
from .entity import ChatGPTUsageEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([
        ChatGPTUsageEvent(entry.runtime_data, "reset", EVENT_USAGE_RESET),
        ChatGPTUsageEvent(entry.runtime_data, "recovery", EVENT_ACCOUNT_RECOVERED),
    ])


class ChatGPTUsageEvent(ChatGPTUsageEntity, EventEntity):
    """Mirror deduplicated coordinator events without replaying historical states."""

    _attr_icon = "mdi:history"

    def __init__(
        self, coordinator: ChatGPTUsageCoordinator, kind: str, bus_event: str
    ) -> None:
        super().__init__(coordinator)
        self._kind = kind
        self._bus_event = bus_event
        self._event_type = "reset" if kind == "reset" else "recovered"
        self._attr_event_types = [self._event_type]
        self._attr_translation_key = f"{kind}_event"
        identifier = self._entry.unique_id or self._entry.entry_id
        self._attr_unique_id = f"{identifier}_{kind}_event"

    @property
    def available(self) -> bool:
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.hass.bus.async_listen(self._bus_event, self._handle_event))

    @callback
    def _handle_event(self, event: Event) -> None:
        if event.data.get("entry_id") != self._entry.entry_id:
            return
        if self._kind == "reset" and event.data.get("is_main") is not True:
            return
        self._trigger_event(self._event_type, dict(event.data))
        self.async_write_ha_state()

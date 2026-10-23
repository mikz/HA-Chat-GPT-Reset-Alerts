"""Manual usage refresh and opt-in weekly-window start buttons."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import ChatGPTUsageCoordinator
from .const import CONF_PROVIDER, PROVIDER_LOCAL
from .entity import ChatGPTUsageEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    entities: list[ButtonEntity] = [ChatGPTRefreshButton(entry.runtime_data)]
    if entry.data.get(CONF_PROVIDER) != PROVIDER_LOCAL:
        entities.append(ChatGPTStartWindowButton(entry.runtime_data))
    async_add_entities(entities)


class ChatGPTRefreshButton(ChatGPTUsageEntity, ButtonEntity):
    _attr_name = "Refresh usage"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:refresh"

    def __init__(self, coordinator: ChatGPTUsageCoordinator) -> None:
        super().__init__(coordinator)
        identifier = self._entry.unique_id or self._entry.entry_id
        self._attr_unique_id = f"{identifier}_refresh"

    @property
    def available(self) -> bool:
        """A failed poll must not disable the action used to retry it."""
        return True

    async def async_press(self) -> None:
        await self.coordinator.async_request_refresh()


class ChatGPTStartWindowButton(ChatGPTUsageEntity, ButtonEntity):
    _attr_name = "Start weekly window"
    _attr_icon = "mdi:play-circle-outline"

    def __init__(self, coordinator: ChatGPTUsageCoordinator) -> None:
        super().__init__(coordinator)
        identifier = self._entry.unique_id or self._entry.entry_id
        self._attr_unique_id = f"{identifier}_start_weekly_window"

    @property
    def available(self) -> bool:
        # Keep the last action and its result inspectable; the coordinator guards presses.
        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.coordinator.start_usage_result

    async def async_press(self) -> None:
        await self.coordinator.async_start_usage_window()

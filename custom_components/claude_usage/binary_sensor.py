"""Binary sensor platform for Claude Usage."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import ClaudeUsageCoordinator
from .entity import ClaudeUsageEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator: ClaudeUsageCoordinator = entry.runtime_data
    async_add_entities(
        [
            ClaudeConnectedBinarySensor(coordinator),
            ClaudeLimitReachedBinarySensor(coordinator),
            ClaudeExtraUsageEnabledBinarySensor(coordinator),
            ClaudeUsableBinarySensor(coordinator),
        ]
    )


class ClaudeConnectedBinarySensor(ClaudeUsageEntity, BinarySensorEntity):
    _attr_name = "Connected"
    _attr_icon = "mdi:cloud-check"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: ClaudeUsageCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_connected"

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def extra_state_attributes(self) -> dict:
        return {"last_error_category": self.coordinator.last_error_category}


class ClaudeLimitReachedBinarySensor(ClaudeUsageEntity, BinarySensorEntity):
    _attr_name = "Ordinary allowance limit reached"
    _attr_icon = "mdi:speedometer-slow"

    def __init__(self, coordinator: ClaudeUsageCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_limit_reached"

    @property
    def is_on(self) -> bool | None:
        usable = self.coordinator.reset_tracker.usable
        return None if usable is None else not usable


class ClaudeUsableBinarySensor(ClaudeLimitReachedBinarySensor):
    _attr_name = "Ordinary allowance available"
    _attr_icon = "mdi:check-circle-outline"

    def __init__(self, coordinator: ClaudeUsageCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_usable"

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.reset_tracker.usable


class ClaudeExtraUsageEnabledBinarySensor(ClaudeUsageEntity, BinarySensorEntity):
    _attr_name = "Extra Usage enabled"
    _attr_icon = "mdi:credit-card-check-outline"

    def __init__(self, coordinator: ClaudeUsageCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_extra_usage_enabled"

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator.data is not None
            and self.coordinator.data.extra_usage is not None
        )

    @property
    def is_on(self) -> bool | None:
        extra = self.coordinator.data.extra_usage if self.coordinator.data else None
        return extra.enabled if extra else None

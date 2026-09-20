"""One-time, read-only migration of observed resets from Recorder history."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from .const import DOMAIN
from .models import PersistedWindowState, clamp_percent

_LOGGER = logging.getLogger(__name__)


def last_refill(records: list[Any]) -> datetime | None:
    """Return the time HA first observed a refill, never a scheduled provider time."""
    previous: float | None = None
    observed: datetime | None = None
    for record in records:
        used = clamp_percent(record.state)
        if used is None:
            continue
        if previous is not None and (
            (previous > 0 and used == 0)
            or (previous >= 90 and used <= 20 and previous - used >= 70)
        ):
            observed = record.last_changed
        previous = used
    return observed


async def async_import_reset_history(hass, entry, windows: dict[str, PersistedWindowState]) -> bool:
    """Import existing usage observations without emitting historical notifications."""
    if "recorder" not in hass.config.components:
        return False
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.history import get_significant_states
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    identifier = entry.unique_id or entry.entry_id
    by_unique_id = {entity.unique_id: entity.entity_id for entity in entries if entity.platform == DOMAIN}
    entities = {
        window_id: by_unique_id[f"{identifier}_{window_id}_usage"]
        for window_id, state in windows.items()
        if state.last_reset_at is None and f"{identifier}_{window_id}_usage" in by_unique_id
    }
    if not entities:
        return True
    try:
        history = await get_instance(hass).async_add_executor_job(partial(
            get_significant_states,
            hass,
            datetime.now(UTC) - timedelta(days=14),
            entity_ids=list(entities.values()),
            no_attributes=True,
        ))
    except Exception:  # History is optional; quota polling must remain available.
        _LOGGER.warning("Could not import prior usage reset observations; will retry on reload")
        return False
    for window_id, entity_id in entities.items():
        if observed := last_refill(history.get(entity_id, [])):
            windows[window_id].last_reset_at = observed.isoformat()
    return True

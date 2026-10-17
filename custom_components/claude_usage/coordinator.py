"""DataUpdateCoordinator for Claude Usage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    ClaudeAuthError,
    ClaudeConnectionError,
    ClaudePayloadError,
    ClaudeRateLimitError,
    ClaudeUsageError,
    ClaudeUsageProvider,
)
from .const import CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL, DOMAIN
from .models import ClaudeUsageData
from .reset import ResetTracker

_LOGGER = logging.getLogger(__name__)


class ClaudeUsageCoordinator(DataUpdateCoordinator[ClaudeUsageData]):
    """Coordinate usage polling and reset detection."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        provider: ClaudeUsageProvider,
    ) -> None:
        self.entry = entry
        self.provider = provider
        self.reset_tracker = ResetTracker(hass, entry.entry_id)
        self.last_error_category: str | None = None
        self._rate_limit_failures = 0
        self._backoff_until: datetime | None = None
        interval = int(entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=interval),
            config_entry=entry,
            always_update=False,
        )

    async def _async_update_data(self) -> ClaudeUsageData:
        now = datetime.now(UTC)
        if self._backoff_until is not None and now < self._backoff_until:
            remaining = round((self._backoff_until - now).total_seconds())
            raise UpdateFailed(f"Claude usage polling is backed off for {remaining}s")
        try:
            data = await self.provider.async_get_usage()
        except ClaudeAuthError as err:
            self.last_error_category = "authentication"
            raise ConfigEntryAuthFailed(str(err)) from err
        except ClaudeRateLimitError as err:
            self.last_error_category = "rate_limited"
            self._rate_limit_failures += 1
            delay = min(86400, err.retry_after or 900 * (2 ** min(self._rate_limit_failures - 1, 7)))
            self._backoff_until = now + timedelta(seconds=max(60, delay))
            detail = f"; backoff={delay}s"
            raise UpdateFailed(f"Anthropic usage endpoint rate limited{detail}") from err
        except (ClaudeConnectionError, ClaudePayloadError, ClaudeUsageError) as err:
            self.last_error_category = "provider"
            raise UpdateFailed(str(err)) from err

        self._rate_limit_failures = 0
        self._backoff_until = None
        self.last_error_category = None
        return await self.reset_tracker.async_process(data)

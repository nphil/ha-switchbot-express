"""Cover support for SwitchBot Express."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DEFAULT_CURTAIN_SPEED, SERVICE_PREWARM
from .coordinator import SwitchbotExpressConfigEntry, SwitchbotExpressCoordinator
from .device import SwitchbotExpressCurtain
from .entity import SwitchbotExpressEntity, exception_handler

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 0

# Below this the curtain counts as closed, matching core.
CLOSED_THRESHOLD = 20


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SwitchbotExpressConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the SwitchBot cover."""
    # One line per platform is all a future Bot or Plug Mini needs to expose
    # the prewarm service too; the implementation lives on the base entity.
    entity_platform.async_get_current_platform().async_register_entity_service(
        SERVICE_PREWARM, None, "async_prewarm"
    )
    async_add_entities([SwitchbotExpressCurtainEntity(entry.runtime_data)])


class SwitchbotExpressCurtainEntity(SwitchbotExpressEntity, CoverEntity, RestoreEntity):
    """A SwitchBot Curtain whose link is governed by the connection policy."""

    _device: SwitchbotExpressCurtain
    _attr_device_class = CoverDeviceClass.CURTAIN
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )
    # name=None with has_entity_name means the entity takes the device's name,
    # so the entity id is cover.<device name>, exactly as core derives it.
    _attr_name = None

    def __init__(self, coordinator: SwitchbotExpressCoordinator) -> None:
        """Initialise the cover."""
        super().__init__(coordinator)
        self._attr_is_closed = None

    async def async_added_to_hass(self) -> None:
        """Restore the last known position."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if not last_state or ATTR_CURRENT_POSITION not in last_state.attributes:
            return
        self._attr_current_cover_position = last_state.attributes[ATTR_CURRENT_POSITION]
        self._last_run_success = last_state.attributes.get("last_run_success")
        if self._attr_current_cover_position is not None:
            self._attr_is_closed = self._attr_current_cover_position <= CLOSED_THRESHOLD

    @exception_handler
    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the curtain."""
        _LOGGER.debug("%s: Opening curtain", self._address)
        self._last_run_success = bool(await self._device.open(DEFAULT_CURTAIN_SPEED))
        self._async_update_motion()

    @exception_handler
    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the curtain."""
        _LOGGER.debug("%s: Closing curtain", self._address)
        self._last_run_success = bool(await self._device.close(DEFAULT_CURTAIN_SPEED))
        self._async_update_motion()

    @exception_handler
    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the curtain."""
        _LOGGER.debug("%s: Stopping curtain", self._address)
        self._last_run_success = bool(await self._device.stop())
        self._async_update_motion()

    @exception_handler
    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the curtain to a position."""
        position = kwargs[ATTR_POSITION]
        _LOGGER.debug("%s: Moving curtain to %s", self._address, position)
        self._last_run_success = bool(
            await self._device.set_position(position, DEFAULT_CURTAIN_SPEED)
        )
        self._async_update_motion()

    @callback
    def _async_update_motion(self) -> None:
        """Publish the optimistic opening/closing state after a command.

        The command itself never disconnects the link: the policy decides when
        it goes, so the read-back and any follow-up command reuse it.
        """
        self._attr_is_opening = self._device.is_opening()
        self._attr_is_closing = self._device.is_closing()
        self.async_write_ha_state()

    @callback
    def _async_update_attrs(self) -> None:
        """Update the cover from the device."""
        self._attr_is_opening = self._device.is_opening()
        self._attr_is_closing = self._device.is_closing()
        if (position := self.parsed_data.get("position")) is None:
            return
        self._attr_current_cover_position = position
        self._attr_is_closed = position <= CLOSED_THRESHOLD

"""Base entity for SwitchBot Express."""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Coroutine, Mapping
from typing import Any, Concatenate, ParamSpec, TypeVar

from switchbot import SwitchbotOperationError

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.const import ATTR_CONNECTIONS
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, MANUFACTURER
from .coordinator import SwitchbotExpressCoordinator

_LOGGER = logging.getLogger(__name__)


class SwitchbotExpressEntity(
    PassiveBluetoothCoordinatorEntity[SwitchbotExpressCoordinator]
):
    """Common behaviour for every SwitchBot Express entity."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SwitchbotExpressCoordinator) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        self._device = coordinator.device
        self._last_run_success: bool | None = None
        self._address = coordinator.ble_device.address
        self._attr_unique_id = coordinator.base_unique_id
        self._attr_device_info = DeviceInfo(
            connections={(dr.CONNECTION_BLUETOOTH, self._address)},
            manufacturer=MANUFACTURER,
            model=str(coordinator.model),
            name=coordinator.device_name,
        )
        if ":" in self._address:
            # A Bluetooth address that is also a MAC address; registering it
            # keeps the device from being split in two on some platforms.
            self._attr_device_info[ATTR_CONNECTIONS].add(
                (dr.CONNECTION_NETWORK_MAC, self._address)
            )

    @property
    def parsed_data(self) -> dict[str, Any]:
        """Return the device's parsed advertisement data."""
        return self.coordinator.device.parsed_data

    @property
    def available(self) -> bool:
        """Return if the device is reachable.

        Advertisement based like core, plus: if we are holding a GATT link to
        it, it is by definition there, even if a proxy has not reported an
        advertisement for a while.
        """
        return super().available or self.coordinator.device.is_connected

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        """Return the state attributes."""
        return {"last_run_success": self._last_run_success}

    @callback
    def _async_update_attrs(self) -> None:
        """Update the entity attributes from the device."""

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle a data update."""
        self._async_update_attrs()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Register callbacks."""
        self.async_on_remove(self._device.subscribe(self._handle_coordinator_update))
        await super().async_added_to_hass()

    async def async_prewarm(self) -> None:
        """Connect now and keep the link warm.

        Backs the ``switchbot.prewarm`` entity service. Registering it
        on a platform is one line, so a future Bot or Plug Mini platform gets
        it for free (see ``cover.py``).
        """
        try:
            await self.coordinator.async_prewarm()
        except SwitchbotOperationError as error:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="operation_error",
                translation_placeholders={"error": str(error)},
            ) from error


_EntityT = TypeVar("_EntityT", bound=SwitchbotExpressEntity)
_P = ParamSpec("_P")


def exception_handler(
    func: Callable[Concatenate[_EntityT, _P], Coroutine[Any, Any, Any]],
) -> Callable[Concatenate[_EntityT, _P], Coroutine[Any, Any, None]]:
    """Turn a pySwitchbot operation error into a translated HA error."""

    async def handler(self: _EntityT, *args: _P.args, **kwargs: _P.kwargs) -> None:
        try:
            await func(self, *args, **kwargs)
        except SwitchbotOperationError as error:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="operation_error",
                translation_placeholders={"error": str(error)},
            ) from error

    return handler


@callback
def async_add_entities_as_data_arrives(
    coordinator: SwitchbotExpressCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
    keys: Collection[str],
    factory: Callable[[str], SwitchbotExpressEntity],
) -> None:
    """Add one entity per advertised value in ``keys``, now or once it first appears.

    Which values a device advertises is only known after its first advertisement,
    and setup no longer waits for that. Entities for keys already known are added
    at once; the rest are added the moment an advertisement or a poll brings them,
    until the coordinator closes discovery (unload starts).
    """
    added: set[str] = set()

    @callback
    def _add_new() -> None:
        if not coordinator.discovery_open:
            return
        new = [
            key
            for key in coordinator.device.parsed_data
            if key in keys and key not in added
        ]
        if not new:
            return
        added.update(new)
        async_add_entities([factory(key) for key in new])

    _add_new()
    coordinator.async_add_discovery_listener(_add_new)

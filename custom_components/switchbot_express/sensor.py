"""Sensors for SwitchBot Express, including the Connection diagnostic."""

from __future__ import annotations

import logging
from typing import Any

from habluetooth import HaBluetoothSlotAllocations, get_manager

from homeassistant.components.bluetooth import async_scanner_by_source
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import STATE_DISCONNECTED
from .coordinator import SwitchbotExpressConfigEntry, SwitchbotExpressCoordinator
from .entity import SwitchbotExpressEntity, async_add_entities_as_data_arrives

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 0

SENSOR_TYPES: dict[str, SensorEntityDescription] = {
    "battery": SensorEntityDescription(
        key="battery",
        translation_key="battery",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    "lightLevel": SensorEntityDescription(
        key="lightLevel",
        translation_key="light_level",
        state_class=SensorStateClass.MEASUREMENT,
    ),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SwitchbotExpressConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the SwitchBot sensors."""
    coordinator = entry.runtime_data
    async_add_entities([SwitchbotExpressConnectionSensor(coordinator)])
    # Battery and light level exist once the device has advertised them, which
    # setup does not wait for.
    async_add_entities_as_data_arrives(
        coordinator,
        async_add_entities,
        SENSOR_TYPES,
        lambda key: SwitchbotExpressSensor(coordinator, key),
    )


class SwitchbotExpressSensor(SwitchbotExpressEntity, SensorEntity):
    """A value the device advertises."""

    def __init__(
        self, coordinator: SwitchbotExpressCoordinator, sensor: str
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator)
        self._sensor = sensor
        self.entity_description = SENSOR_TYPES[sensor]
        self._attr_unique_id = f"{coordinator.base_unique_id}_{sensor}"

    @property
    def native_value(self) -> str | int | None:
        """Return the sensor value."""
        return self.parsed_data.get(self._sensor)


class SwitchbotExpressConnectionSensor(SwitchbotExpressEntity, SensorEntity):
    """Which proxy is holding a GATT link to this device, if any.

    The link is a shared, scarce resource: a proxy has a handful of connection
    slots, and knowing which one is spending a slot on this curtain -- and how
    often that link drops -- is the difference between "Bluetooth is flaky" and
    a diagnosis.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "connection"
    _attr_icon = "mdi:bluetooth-connect"
    _attr_entity_registry_enabled_default = True

    def __init__(self, coordinator: SwitchbotExpressCoordinator) -> None:
        """Initialise the connection sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.base_unique_id}_connection"
        self._source: str | None = None

    async def async_added_to_hass(self) -> None:
        """Subscribe to slot allocations and to policy changes."""
        await super().async_added_to_hass()
        manager = get_manager()
        self.async_on_remove(
            manager.async_register_allocation_callback(
                self._async_allocations_changed, None
            )
        )
        self.async_on_remove(
            self.coordinator.async_add_connection_listener(self._async_state_changed)
        )
        self._async_refresh_source()

    @property
    def native_value(self) -> str:
        """Return the scanner holding the link, or ``disconnected``."""
        if self._source is None:
            return STATE_DISCONNECTED
        if scanner := async_scanner_by_source(self.hass, self._source):
            return scanner.name
        return self._source

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return why the link is (or is not) up."""
        last_drop = self.coordinator.last_drop
        return {
            "hold": self.coordinator.policy.hold_active,
            "drops_1h": self.coordinator.drops_1h,
            "last_drop": last_drop.isoformat() if last_drop else None,
            "reconnect_attempt": self.coordinator.reconnect_attempt,
        }

    @callback
    def _async_allocations_changed(
        self, allocations: HaBluetoothSlotAllocations
    ) -> None:
        """Handle a slot allocation change on any source."""
        self._async_refresh_source()

    @callback
    def _async_state_changed(self) -> None:
        """Handle a hold, drop or reconnect change."""
        self._async_refresh_source()

    @callback
    def _async_refresh_source(self) -> None:
        """Recompute which source holds this address and write the state."""
        source = self._async_find_source()
        self._source = source
        self.async_write_ha_state()

    @callback
    def _async_find_source(self) -> str | None:
        """Return the source whose slot this address occupies."""
        address = self._address.upper()
        for allocations in get_manager().async_current_allocations() or ():
            for allocated in allocations.allocated:
                if allocated.upper() == address:
                    return allocations.source
        return None

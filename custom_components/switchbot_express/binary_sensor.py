"""Binary sensors for SwitchBot Express."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SwitchbotExpressConfigEntry, SwitchbotExpressCoordinator
from .entity import SwitchbotExpressEntity, async_add_entities_as_data_arrives

PARALLEL_UPDATES = 0

BINARY_SENSOR_TYPES: dict[str, BinarySensorEntityDescription] = {
    "calibration": BinarySensorEntityDescription(
        key="calibration",
        translation_key="calibration",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SwitchbotExpressConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the SwitchBot binary sensors."""
    coordinator = entry.runtime_data
    async_add_entities_as_data_arrives(
        entry,
        async_add_entities,
        BINARY_SENSOR_TYPES,
        lambda key: SwitchbotExpressBinarySensor(coordinator, key),
    )


class SwitchbotExpressBinarySensor(SwitchbotExpressEntity, BinarySensorEntity):
    """A boolean the device advertises."""

    def __init__(
        self, coordinator: SwitchbotExpressCoordinator, binary_sensor: str
    ) -> None:
        """Initialise the binary sensor."""
        super().__init__(coordinator)
        self._sensor = binary_sensor
        self.entity_description = BINARY_SENSOR_TYPES[binary_sensor]
        self._attr_unique_id = f"{coordinator.base_unique_id}_{binary_sensor}"

    @property
    def is_on(self) -> bool | None:
        """Return the sensor state."""
        return self.parsed_data.get(self._sensor)

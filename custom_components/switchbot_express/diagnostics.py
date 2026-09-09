"""Diagnostics for SwitchBot Express."""

from __future__ import annotations

from typing import Any

from habluetooth import get_manager

from homeassistant.components.bluetooth import (
    async_last_service_info,
    async_scanner_by_source,
)
from homeassistant.core import HomeAssistant

from .coordinator import SwitchbotExpressConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SwitchbotExpressConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    address = coordinator.address.upper()

    service_info = async_last_service_info(hass, address, True)
    holders = [
        {
            "source": allocations.source,
            "name": (
                scanner.name
                if (scanner := async_scanner_by_source(hass, allocations.source))
                else None
            ),
            "slots": allocations.slots,
            "free": allocations.free,
        }
        for allocations in get_manager().async_current_allocations() or ()
        if any(allocated.upper() == address for allocated in allocations.allocated)
    ]

    return {
        "entry": {
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "coordinator": coordinator.as_dict(),
        "advertisement": {
            "rssi": service_info.rssi if service_info else None,
            "source": service_info.source if service_info else None,
            "parsed": dict(coordinator.device.parsed_data),
        },
        "holding": holders,
    }

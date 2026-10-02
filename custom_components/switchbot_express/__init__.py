"""SwitchBot Express.

A replacement for core's ``switchbot`` integration for Bluetooth SwitchBots,
with one difference that matters: core drops the GATT link 8.5 s after the last
command, so the first command of every burst pays a fresh proxy connect (1-6 s
on a typical ESPHome proxy). This integration keeps the protocol handling in
pySwitchbot and replaces only the connection policy -- a configurable linger
after a *user* command, an opt-in permanent hold for mains or solar fed units,
a ``prewarm`` service for automations that know a command is coming, and a
hard cap on a single connect attempt so a bad proxy path fails fast instead of
wedging a command for a minute.
"""

from __future__ import annotations

import logging

import switchbot

from homeassistant.components import bluetooth
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import HassJob, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir

from .const import CONF_DEVICE_TYPE, DOMAIN, ISSUE_LOW_BATTERY, PLATFORMS_BY_TYPE
from .coordinator import SwitchbotExpressConfigEntry, SwitchbotExpressCoordinator
from .device import SUPPORTED_TYPES, core_disconnect_delay, create_device
from .policy import ConnectionPolicy, PolicyOptions

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: SwitchbotExpressConfigEntry
) -> bool:
    """Set up a SwitchBot Express device from a config entry."""
    address: str = entry.data[CONF_ADDRESS].upper()
    device_type: str = entry.data[CONF_DEVICE_TYPE]
    if device_type not in SUPPORTED_TYPES:
        _LOGGER.error(
            "Config entry %s asks for unsupported device type %s",
            entry.title,
            device_type,
        )
        return False

    # Another integration (or a previous run) may still be holding this
    # device; a stale link would make every command here time out.
    await switchbot.close_stale_connections_by_address(address)

    ble_device = bluetooth.async_ble_device_from_address(hass, address, True)
    if not ble_device:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="device_not_found",
            translation_placeholders={"address": address},
        )

    policy = ConnectionPolicy(
        PolicyOptions.from_mapping(entry.options),
        core_disconnect_delay=core_disconnect_delay(),
    )
    device = create_device(device_type, ble_device, policy)
    coordinator = entry.runtime_data = SwitchbotExpressCoordinator(
        hass,
        _LOGGER,
        ble_device,
        device,
        base_unique_id=address,
        device_name=entry.data.get(CONF_NAME, entry.title),
        device_type=device_type,
        model=SUPPORTED_TYPES[device_type].model,
        config_entry=entry,
    )
    entry.async_on_unload(
        hass.async_add_shutdown_job(
            HassJob(
                coordinator.async_release_at_shutdown,
                f"switchbot_express release BLE link {entry.title}",
            )
        )
    )
    entry.async_on_unload(coordinator.async_start())
    if not await coordinator.async_wait_ready():
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="not_advertising",
            translation_placeholders={"address": address},
        )

    coordinator.async_setup_link()
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(
        entry, PLATFORMS_BY_TYPE[device_type]
    )
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: SwitchbotExpressConfigEntry
) -> bool:
    """Unload a config entry, releasing any link we were holding."""
    coordinator = entry.runtime_data
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS_BY_TYPE[entry.data[CONF_DEVICE_TYPE]]
    )
    if unload_ok:
        await coordinator.async_teardown()
    return unload_ok


async def async_remove_entry(
    hass: HomeAssistant, entry: SwitchbotExpressConfigEntry
) -> None:
    """Clean up repairs this device raised."""
    ir.async_delete_issue(
        hass, DOMAIN, f"{ISSUE_LOW_BATTERY}_{entry.data[CONF_ADDRESS].upper()}"
    )


async def _async_update_listener(
    hass: HomeAssistant, entry: SwitchbotExpressConfigEntry
) -> None:
    """Apply changed options.

    ``retry_count`` is handed to pySwitchbot at construction, so a change to it
    needs a rebuild; the rest are read live. Reloading for all of them keeps
    one obvious code path.
    """
    await hass.config_entries.async_reload(entry.entry_id)

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

import asyncio
import logging

import switchbot

from homeassistant.components import bluetooth
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import HassJob, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.typing import ConfigType

from . import shutdown
from .const import CONF_DEVICE_TYPE, DOMAIN, ISSUE_LOW_BATTERY, PLATFORMS_BY_TYPE
from .coordinator import (
    SETUP_BUDGET,
    SwitchbotExpressConfigEntry,
    SwitchbotExpressCoordinator,
)
from .device import SUPPORTED_TYPES, core_disconnect_delay, create_device
from .policy import ConnectionPolicy, PolicyOptions

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the domain-wide shutdown latch, once per Home Assistant run.

    Home Assistant reads its shutdown-job list once when the stage starts, so an entry that is
    set up or reloaded during the stage registers its own job too late to ever run. This job is
    registered here, is never removed with an entry, and latches everything: loaded devices are
    told to refuse connects, and entry setup refuses to start (see ``_refuse_while_shutting_down``).
    """
    hass.async_add_shutdown_job(HassJob(_async_latch_for_shutdown, "switchbot_express shutdown latch"), hass)
    return True


async def _async_latch_for_shutdown(hass: HomeAssistant) -> None:
    """Domain-wide latch: from now on nothing in this integration opens a Bluetooth link."""
    shutdown.begin(hass)
    for entry in hass.config_entries.async_entries(DOMAIN):
        coordinator = getattr(entry, "runtime_data", None)
        if isinstance(coordinator, SwitchbotExpressCoordinator):
            coordinator.device.latch_closing()


def _refuse_while_shutting_down(hass: HomeAssistant) -> None:
    """Entry setup/reload during Home Assistant's shutdown must not start anything."""
    if shutdown.in_progress(hass):
        raise ConfigEntryNotReady("Home Assistant is shutting down")


async def async_setup_entry(
    hass: HomeAssistant, entry: SwitchbotExpressConfigEntry
) -> bool:
    """Set up a SwitchBot Express device from a config entry."""
    _refuse_while_shutting_down(hass)
    address: str = entry.data[CONF_ADDRESS].upper()
    device_type: str = entry.data[CONF_DEVICE_TYPE]
    if device_type not in SUPPORTED_TYPES:
        _LOGGER.error(
            "Config entry %s asks for unsupported device type %s",
            entry.title,
            device_type,
        )
        return False

    # One budget for everything setup itself waits on (the stale-link cleanup
    # and the first advertisement): whatever the device or the radio does,
    # setup returns within SETUP_BUDGET s and the rest happens in the background.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SETUP_BUDGET

    # Another integration (or a previous run) may still be holding this
    # device; a stale link would make every command here time out. The library
    # call has no timeout of its own (it talks to BlueZ over D-Bus).
    try:
        async with asyncio.timeout_at(deadline):
            await switchbot.close_stale_connections_by_address(address)
    except TimeoutError:
        _LOGGER.debug("%s: Stale-connection cleanup timed out; carrying on", address)
    _refuse_while_shutting_down(hass)

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
    # Closed again by unload before the platforms go (and here if setup fails).
    entry.async_on_unload(coordinator.async_stop_discovery)
    entry.async_on_unload(coordinator.async_start())
    # Not hearing the device inside the budget is not a failure: the entities
    # stay unavailable and fill in when the first advertisement arrives, and the
    # connection supervisor retries by itself (a retry backoff would only delay it).
    if not await coordinator.async_wait_ready(deadline - loop.time()):
        _LOGGER.debug(
            "%s: Not heard within %s s; entities stay unavailable until it is",
            address,
            SETUP_BUDGET,
        )
    # Shutdown may have begun while waiting for the first advertisement.
    _refuse_while_shutting_down(hass)

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
    # First data arriving while the platforms unload must not add entities to
    # a platform that is going away (they would duplicate after the reload).
    coordinator.async_stop_discovery()
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

"""Coordinator for SwitchBot Express.

Advertisement handling is core's: passive advertisements keep the state fresh
and a poll is only needed for the values the device does not advertise. On top
of that this coordinator owns the connection policy for its device: it feeds
the policy the battery level, runs the single reconnect supervisor that keeps
a held link up, and tells the Connection sensor when any of that changed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import switchbot
from bleak.backends.device import BLEDevice
from switchbot import SwitchbotModel

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, CoreState, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from . import shutdown
from .const import DOMAIN, ISSUE_LOW_BATTERY
from .device import ExpressConnectionMixin
from .policy import ConnectionPolicy

_LOGGER = logging.getLogger(__name__)

# Setup returns within this many seconds of being called, whatever the device
# is doing. Connecting, authenticating and polling carry on in the background.
SETUP_BUDGET = 5.0

# Upper bound for the shutdown-time release. Home Assistant gives all shutdown
# jobs one shared 20 s budget.
SHUTDOWN_RELEASE_TIMEOUT = 8

SwitchbotExpressConfigEntry = ConfigEntry["SwitchbotExpressCoordinator"]


class SwitchbotExpressCoordinator(ActiveBluetoothDataUpdateCoordinator[None]):
    """Fetch SwitchBot data and govern the device's connection."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        ble_device: BLEDevice,
        device: ExpressConnectionMixin,
        base_unique_id: str,
        device_name: str,
        device_type: str,
        model: SwitchbotModel,
        config_entry: SwitchbotExpressConfigEntry,
    ) -> None:
        """Initialise the coordinator for one device."""
        super().__init__(
            hass=hass,
            logger=logger,
            address=ble_device.address,
            needs_poll_method=self._needs_poll,
            poll_method=self._async_update,
            mode=bluetooth.BluetoothScanningMode.ACTIVE,
            connectable=True,
        )
        self.ble_device = ble_device
        self.device = device
        self.device_name = device_name
        self.device_type = device_type
        self.base_unique_id = base_unique_id
        self.model = model
        self.config_entry = config_entry
        self._ready_event = asyncio.Event()
        self._was_unavailable = True
        self._connection_listeners: list[CALLBACK_TYPE] = []
        self._low_battery_issue = False
        self._supervisor_task: asyncio.Task[None] | None = None
        self._unsub_state: Callable[[], None] | None = None
        self._discovery_open = True
        self._discovery_unsubs: list[CALLBACK_TYPE] = []

    @property
    def policy(self) -> ConnectionPolicy:
        """Return the connection policy for this device."""
        return self.device.policy

    @property
    def reconnect_attempt(self) -> int:
        """Return the in-flight reconnect attempt, 0 when there is none."""
        return self.device.reconnect_attempt

    @property
    def drops_1h(self) -> int:
        """Return unexpected disconnects in the trailing hour."""
        return self.policy.drops_1h()

    @property
    def last_drop(self) -> datetime | None:
        """Return the last unexpected disconnect."""
        return self.policy.last_drop

    # -- late entity discovery --------------------------------------------

    @property
    def discovery_open(self) -> bool:
        """Whether new entities may still be added (closed for good once unloading starts)."""
        return self._discovery_open

    @callback
    def async_add_discovery_listener(self, update_callback: CALLBACK_TYPE) -> None:
        """Call ``update_callback`` on every advertisement and every poll result.

        Used to add entities whose existence depends on data the device has not
        sent yet. ``async_stop_discovery`` removes every such listener.
        """
        if not self._discovery_open:
            return
        self._discovery_unsubs.append(self.async_add_listener(update_callback))
        self._discovery_unsubs.append(self.device.subscribe(update_callback))

    @callback
    def async_stop_discovery(self) -> None:
        """Stop adding entities. One-way and idempotent; unload calls it before the platforms go."""
        self._discovery_open = False
        unsubs, self._discovery_unsubs = self._discovery_unsubs, []
        for unsub in unsubs:
            unsub()

    # -- setup and teardown ----------------------------------------------

    @callback
    def async_setup_link(self) -> None:
        """Start the connection supervisor and prime the battery state."""
        self._unsub_state = self.device.register_state_callback(
            self.async_update_connection_listeners
        )
        self._async_update_battery()
        self.device.refresh_link_policy()
        self._supervisor_task = self.config_entry.async_create_background_task(
            self.hass,
            self.device.async_supervise(),
            name=f"{DOMAIN} connection supervisor {self.address}",
        )

    async def async_teardown(self) -> None:
        """Stop the supervisor and let the link go."""
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor_task
            self._supervisor_task = None
        await self.device.async_release()

    async def async_release_at_shutdown(self) -> None:
        """Home Assistant shutdown job (Stage 1, before Bluetooth and the proxies go away).

        Stops the reconnect supervisor first so the deliberate disconnect is not
        chased by a reconnect, latches the device closed and drops the link.
        Bounded, never raises, and deliberately does not unload the entry (that
        would write a wave of ``unavailable`` states).
        """
        shutdown.begin(self.hass)
        self.device.latch_closing()
        started = time.monotonic()
        try:
            async with asyncio.timeout(SHUTDOWN_RELEASE_TIMEOUT):
                if self._supervisor_task is not None:
                    self._supervisor_task.cancel()
                    await asyncio.wait([self._supervisor_task])
                    self._supervisor_task = None
                await self.device.async_release_for_shutdown()
        except TimeoutError:
            _LOGGER.warning(
                "Timed out after %s s releasing the BLE link to %s at shutdown",
                SHUTDOWN_RELEASE_TIMEOUT,
                self.device_name,
            )
        except Exception as err:  # noqa: BLE001 - a shutdown job must never raise
            _LOGGER.warning(
                "Could not release the BLE link to %s at shutdown: %s",
                self.device_name,
                err,
            )
        else:
            _LOGGER.info(
                "Released BLE link to %s at shutdown in %.2f s",
                self.device_name,
                time.monotonic() - started,
            )

    # -- advertisements and polling --------------------------------------

    @callback
    def _needs_poll(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        seconds_since_last_poll: float | None,
    ) -> bool:
        # Only poll if hass is running, we need to poll, and we actually have
        # a way to connect to the device.
        return (
            self.hass.state is CoreState.running
            and not shutdown.in_progress(self.hass)
            and not self.device.closing
            and self.device.poll_needed(seconds_since_last_poll)
            and bool(
                bluetooth.async_ble_device_from_address(
                    self.hass, service_info.device.address, connectable=True
                )
            )
        )

    async def _async_update(
        self, service_info: bluetooth.BluetoothServiceInfoBleak
    ) -> None:
        """Poll the device.

        A poll deliberately does not arm the linger window: only a user
        command does that.
        """
        await self.device.update()
        self._async_update_battery()

    @callback
    def _async_handle_unavailable(
        self, service_info: bluetooth.BluetoothServiceInfoBleak
    ) -> None:
        """Handle the device going unavailable."""
        super()._async_handle_unavailable(service_info)
        self._was_unavailable = True
        _LOGGER.info("Device %s is unavailable", self.device_name)

    @callback
    def _async_handle_bluetooth_event(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Handle a Bluetooth event."""
        self.ble_device = service_info.device
        if not (
            adv := switchbot.parse_advertisement_data(
                service_info.device, service_info.advertisement, self.model
            )
        ):
            return
        if "modelName" in adv.data:
            self._ready_event.set()
        if not self.device.advertisement_changed(adv) and not self._was_unavailable:
            return
        self._was_unavailable = False
        self.device.update_from_advertisement(adv)
        self._async_update_battery()
        super()._async_handle_bluetooth_event(service_info, change)

    async def async_wait_ready(self, timeout: float = SETUP_BUDGET) -> bool:
        """Wait up to ``timeout`` s for the first advertisement; False if none came.

        Never raises on a timeout: the entities simply stay unavailable until
        the device is heard, which the coordinator picks up by itself.
        """
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(max(timeout, 0)):
                await self._ready_event.wait()
                return True
        return False

    # -- battery -----------------------------------------------------------

    @callback
    def _async_update_battery(self) -> None:
        """Feed the battery level to the policy and the repairs issue."""
        battery = self.device.get_battery_percent()
        if battery is not None:
            try:
                battery = int(battery)
            except (TypeError, ValueError):
                battery = None
        if not self.policy.set_battery(battery):
            # No threshold crossing: nothing about the link or the repair can
            # have changed.
            return
        _LOGGER.debug(
            "%s: Battery %s%% crossed the %s%% threshold; linger and hold are "
            "now %s",
            self.device_name,
            battery,
            self.policy.options.low_battery_percent,
            "suspended" if self.policy.battery_saver_active else "available",
        )
        self._async_update_low_battery_issue()
        self.device.refresh_link_policy()

    @callback
    def _async_update_low_battery_issue(self) -> None:
        """Raise or clear the low battery repair."""
        low = self.policy.battery_saver_active
        if low is self._low_battery_issue:
            return
        self._low_battery_issue = low
        issue_id = f"{ISSUE_LOW_BATTERY}_{self.address.upper()}"
        if not low:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_LOW_BATTERY,
            translation_placeholders={
                "name": self.device_name,
                "battery": str(self.policy.battery),
                "threshold": str(self.policy.options.low_battery_percent),
            },
        )

    # -- connection state --------------------------------------------------

    @callback
    def async_add_connection_listener(
        self, update_callback: CALLBACK_TYPE
    ) -> CALLBACK_TYPE:
        """Subscribe to connection state changes (hold, drops, attempts)."""
        self._connection_listeners.append(update_callback)

        @callback
        def _remove() -> None:
            if update_callback in self._connection_listeners:
                self._connection_listeners.remove(update_callback)

        return _remove

    @callback
    def async_update_connection_listeners(self) -> None:
        """Tell the Connection sensor something changed."""
        for update_callback in list(self._connection_listeners):
            update_callback()

    # -- services ----------------------------------------------------------

    async def async_prewarm(self) -> int:
        """Connect now and keep the link for the prewarm window."""
        duration = await self.device.async_prewarm()
        _LOGGER.debug(
            "%s: Prewarmed, holding the link for %ss", self.device_name, duration
        )
        self.async_update_connection_listeners()
        return duration

    def as_dict(self) -> dict[str, Any]:
        """Return coordinator state, for diagnostics."""
        return {
            "address": self.address,
            "device_name": self.device_name,
            "device_type": self.device_type,
            "model": str(self.model),
            "available": self.available,
            "connected": self.device.is_connected,
            "reconnect_attempt": self.reconnect_attempt,
            "policy": self.policy.as_dict(),
        }

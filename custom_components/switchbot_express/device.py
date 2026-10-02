"""pySwitchbot devices with a configurable connection policy.

The protocol stays in the library; only the connection lifecycle is ours.

Upstream surface this file depends on -- pySwitchbot 2.4.1,
``switchbot/devices/device.py``. Audit exactly these when bumping the pin in
``manifest.json``; everything else is untouched.

* ``SwitchbotBaseDevice._reset_disconnect_timer`` -- OVERRIDDEN. Upstream arms
  ``call_later(DISCONNECT_DELAY, self._disconnect_from_timer)`` unconditionally.
  We ask the policy for the delay instead, and arm nothing at all while
  holding. Upstream calls this from ``_ensure_connected`` (three sites) and
  from ``_disconnect_from_timer``; all three keep working unchanged.
* ``SwitchbotBaseDevice._ensure_connected`` -- WRAPPED. Upstream hands
  ``establish_connection`` bleak-retry-connector's own defaults (4 attempts,
  20 s each, 60 s safety), so one bad proxy path can wedge a command for over
  a minute. We cap the whole connect at ``connect_timeout`` and re-raise
  ``TimeoutError``, which is in ``BLEAK_RETRY_EXCEPTIONS``, so the library's
  own retry loop tries again -- and habluetooth re-scores the proxies for that
  next attempt, which is how roaming happens.
* ``SwitchbotBaseDevice._disconnected`` -- EXTENDED. Upstream logs and cancels
  the timer; we additionally record unexpected drops and wake the supervisor.
  We read ``_expected_disconnect`` before delegating because upstream does not
  tell us which kind of disconnect it was.
* ``SwitchbotBaseDevice._execute_forced_disconnect`` -- CALLED (error paths).
* ``SwitchbotBaseDevice._cancel_disconnect_timer`` -- CALLED (teardown).
* ``SwitchbotBaseDevice._client`` / ``_expected_disconnect`` -- READ.
* module constant ``DISCONNECT_DELAY`` -- READ, and handed to the policy as its
  floor, so the two can never drift.
* ``SwitchbotCurtain.open`` / ``close`` / ``stop`` / ``set_position`` --
  OVERRIDDEN only to arm the linger window before delegating; each upstream
  method keeps its ``@update_after_operation`` read-back.

Untouched and relied upon as-is: ``_disconnect_from_timer``,
``_execute_timed_disconnect``, ``_execute_disconnect``,
``_execute_disconnect_with_lock`` (its "skip if the timer was reset" guard is
what lets a re-armed timer cancel an in-flight disconnect),
``_send_command``/``_send_command_locked_with_retry`` and the whole protocol.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from bleak.backends.device import BLEDevice
from switchbot import SwitchbotCurtain, SwitchbotModel, SwitchbotOperationError
from switchbot.devices.device import DISCONNECT_DELAY, SwitchbotBaseDevice

from .policy import ConnectionPolicy, reconnect_delay

_LOGGER = logging.getLogger(__name__)

_CLOSING_MESSAGE = "Home Assistant is shutting down; not opening a Bluetooth link"


class ExpressConnectionMixin(SwitchbotBaseDevice):
    """Give a pySwitchbot device a policy-driven connection lifecycle.

    Rooted at ``SwitchbotBaseDevice`` so cooperative ``super()`` calls and the
    attributes we read resolve for real; it is always mixed in *in front of* a
    concrete device class, never instantiated on its own.
    """

    def __init__(self, *args: Any, policy: ConnectionPolicy, **kwargs: Any) -> None:
        """Initialise the device with the policy that governs its link."""
        self._policy = policy
        self._state_callbacks: list[Callable[[], None]] = []
        self._reconnect_attempt = 0
        self._hold_wanted = asyncio.Event()
        self._link_lost = asyncio.Event()
        # Set once by async_release_for_shutdown and never cleared: Home
        # Assistant is going down, so this process must not open another link.
        self._closing = False
        super().__init__(*args, **kwargs)

    @property
    def policy(self) -> ConnectionPolicy:
        """Return the policy governing this device's link."""
        return self._policy

    @property
    def closing(self) -> bool:
        """Return True once Home Assistant's shutdown has latched the link closed."""
        return self._closing

    def latch_closing(self) -> None:
        """Refuse every future connect and wake the supervisor so it exits. One-way.

        Synchronous so the domain-wide shutdown job can latch every device at once; the link
        itself is dropped by ``async_release_for_shutdown``.
        """
        self._closing = True
        self._hold_wanted.set()  # wakes a supervisor parked while not holding; it sees the latch and exits
        self._link_lost.set()

    @property
    def is_connected(self) -> bool:
        """Return True while a GATT link to the device is up."""
        return bool(self._client and self._client.is_connected)

    @property
    def reconnect_attempt(self) -> int:
        """Return the in-flight reconnect attempt, 0 when there is none."""
        return self._reconnect_attempt

    def register_state_callback(
        self, callback: Callable[[], None]
    ) -> Callable[[], None]:
        """Register a callback fired when the link state changed.

        Fires on an unexpected disconnect and on every reconnect attempt, so
        the Connection sensor can show what is going on.
        """
        self._state_callbacks.append(callback)

        def _unsubscribe() -> None:
            if callback in self._state_callbacks:
                self._state_callbacks.remove(callback)

        return _unsubscribe

    def _notify_state(self) -> None:
        """Tell subscribers the link state changed."""
        for callback in list(self._state_callbacks):
            callback()

    # -- overrides -------------------------------------------------------

    def _reset_disconnect_timer(self) -> None:
        """Arm the idle timer for as long as the policy wants the link.

        Replaces upstream's fixed ``DISCONNECT_DELAY``. ``None`` from the
        policy means "hold": no timer is armed, so nothing ever disconnects
        the link from a timeout.
        """
        self._cancel_disconnect_timer()
        self._expected_disconnect = False
        delay = self._policy.disconnect_delay()
        if delay is None:
            _LOGGER.debug("%s: Holding connection, no idle timer armed", self.name)
            return
        self._disconnect_timer = asyncio.get_running_loop().call_later(
            delay, self._disconnect_from_timer
        )

    async def _ensure_connected(self) -> None:
        """Connect, but never let one attempt run away.

        ``TimeoutError`` is deliberately re-raised: it is a member of
        ``BLEAK_RETRY_EXCEPTIONS``, so pySwitchbot's retry loop makes the next
        attempt, and habluetooth picks the best proxy again for it.
        """
        if self._closing:
            raise SwitchbotOperationError(_CLOSING_MESSAGE)
        timeout = self._policy.options.connect_timeout
        try:
            async with asyncio.timeout(timeout):
                await super()._ensure_connected()
        except TimeoutError:
            _LOGGER.debug(
                "%s: Connect attempt exceeded %ss; failing fast so the next "
                "attempt can pick another proxy",
                self.name,
                timeout,
            )
            await self._execute_forced_disconnect()
            raise
        if self._closing:
            # Shutdown began while this connect was in flight: hand the link back.
            await self._execute_forced_disconnect()
            raise SwitchbotOperationError(_CLOSING_MESSAGE)

    def _disconnected(self, client: Any) -> None:
        """Record unexpected drops on top of upstream's handling.

        Once the shutdown latch is set every disconnect is deliberate (ours, or the proxy going
        down with Home Assistant): not a fault, so it is neither counted nor warned about.
        """
        if self._closing:
            self._expected_disconnect = True
        unexpected = not self._expected_disconnect
        super()._disconnected(client)
        if not unexpected:
            return
        self._policy.note_drop()
        _LOGGER.debug(
            "%s: Link dropped unexpectedly (%s in the last hour)",
            self.name,
            self._policy.drops_1h(),
        )
        self._link_lost.set()
        self._notify_state()

    # -- public lifecycle ------------------------------------------------

    async def async_ensure_connected(self) -> None:
        """Connect now if not connected, and (re)arm the idle timer."""
        await self._ensure_connected()

    async def async_prewarm(self) -> int:
        """Connect now and keep the link for ``prewarm_seconds``."""
        duration = self._policy.note_prewarm()
        await self._ensure_connected()
        # _ensure_connected arms the timer itself, but only on the paths that
        # touch the link; re-arming here covers "already connected with a
        # shorter window left".
        self._reset_disconnect_timer()
        return duration

    def refresh_link_policy(self) -> None:
        """Re-apply the policy after the options or the battery changed."""
        if self.is_connected:
            self._reset_disconnect_timer()
        if self._policy.hold_active and not self._closing:
            self._hold_wanted.set()
        else:
            self._hold_wanted.clear()
        # Wake a supervisor parked on the link so it re-reads the policy.
        self._link_lost.set()
        self._notify_state()

    async def async_supervise(self) -> None:
        """Keep the link up for as long as the policy says to hold it.

        One task per device, cancelled on unload. Every attempt goes back
        through ``establish_connection``, and habluetooth re-scores the
        proxies each time, so a device that drifted closer to another proxy
        roams onto it here without us choosing anything.
        """
        while not self._closing:
            if not self._policy.hold_active:
                self._set_reconnect_attempt(0)
                await self._hold_wanted.wait()
                continue
            if self.is_connected:
                self._set_reconnect_attempt(0)
                self._link_lost.clear()
                await self._link_lost.wait()
                continue
            attempt = self._reconnect_attempt + 1
            self._set_reconnect_attempt(attempt)
            await asyncio.sleep(reconnect_delay(attempt))
            if not self._policy.hold_active:
                continue
            try:
                await self._ensure_connected()
            except Exception as err:  # noqa: BLE001 - any failure just retries
                _LOGGER.debug(
                    "%s: Reconnect attempt %s failed: %s", self.name, attempt, err
                )
            else:
                _LOGGER.debug("%s: Reconnected on attempt %s", self.name, attempt)

    def _set_reconnect_attempt(self, attempt: int) -> None:
        if attempt == self._reconnect_attempt:
            return
        self._reconnect_attempt = attempt
        self._notify_state()

    async def async_release(self) -> None:
        """Cancel the idle timer and drop the link. Used on unload."""
        self._policy.cancel_prewarm()
        self._hold_wanted.clear()
        self._cancel_disconnect_timer()
        await self._execute_forced_disconnect()

    async def async_release_for_shutdown(self) -> None:
        """Latch the device closed, then drop the link. One-way, for Home Assistant shutdown.

        The latch goes first so no connect path (command, poll, prewarm,
        reconnect supervisor) can open a new link afterwards. The forced
        disconnect waits for any connect already in flight (they share
        pySwitchbot's connect lock); that connect armed its idle timer on
        success, which makes pySwitchbot skip the disconnect, so a second pass
        clears it when a link is still there.
        """
        self._closing = True
        await self.async_release()
        if self._client is not None:
            await self.async_release()


class SwitchbotExpressCurtain(ExpressConnectionMixin, SwitchbotCurtain):
    """A Curtain / Curtain 3 whose link outlives the command that opened it.

    Only the four user-facing commands arm the linger window. A poll or the
    read-back that ``@update_after_operation`` performs must not: a background
    refresh has no business buying the radio another 15 s of wakeups.
    """

    async def open(self, speed: int = 255) -> bool:
        """Open the curtain."""
        self._policy.note_user_command()
        return await super().open(speed)

    async def close(self, speed: int = 255) -> bool:
        """Close the curtain."""
        self._policy.note_user_command()
        return await super().close(speed)

    async def stop(self) -> bool:
        """Stop the curtain."""
        self._policy.note_user_command()
        return await super().stop()

    async def set_position(self, position: int, speed: int = 255) -> bool:
        """Move the curtain to a position."""
        self._policy.note_user_command()
        return await super().set_position(position, speed)


@dataclass(frozen=True, slots=True)
class DeviceSupport:
    """One supported device family."""

    key: str
    model: SwitchbotModel
    cls: type[SwitchbotBaseDevice]


# Adding Bot, Plug Mini or Blind Tilt later is: a subclass that mixes
# ExpressConnectionMixin in front of the pySwitchbot class and arms the linger
# window in its command methods, one row here, and one row in
# const.PLATFORMS_BY_TYPE. Nothing else in the integration is model aware.
SUPPORTED_TYPES: dict[str, DeviceSupport] = {
    "curtain": DeviceSupport(
        key="curtain",
        model=SwitchbotModel.CURTAIN,
        cls=SwitchbotExpressCurtain,
    ),
}

MODEL_TO_TYPE: dict[SwitchbotModel, str] = {
    support.model: key for key, support in SUPPORTED_TYPES.items()
}


def create_device(
    device_type: str,
    ble_device: BLEDevice,
    policy: ConnectionPolicy,
) -> SwitchbotBaseDevice:
    """Build the pySwitchbot device for a supported type."""
    support = SUPPORTED_TYPES[device_type]
    return support.cls(
        device=ble_device,
        policy=policy,
        retry_count=policy.options.retry_count,
    )


def core_disconnect_delay() -> float:
    """Return the library's own idle disconnect delay (its DISCONNECT_DELAY)."""
    return float(DISCONNECT_DELAY)

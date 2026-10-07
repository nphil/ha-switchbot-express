"""pySwitchbot devices with a configurable connection policy.

The protocol stays in the library; only the connection lifecycle is ours.

Upstream surface this file depends on -- pySwitchbot ``switchbot/devices/device.py``,
audited unchanged from 2.4.1 through 2.9.0. The requirement is a floor, not a
pin: the core SwitchBot integration pins an exact version, and an exact pin here
would downgrade the shared library under core on every Home Assistant release
(2.4.1 broke core's import on 2026.10). When core moves to a newer version,
audit exactly these; everything else is untouched.

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
* ``SwitchbotBaseDevice._execute_command_locked`` -- WRAPPED in a 10 s
  timeout. Upstream's ``write_gatt_char`` has none, so a stuck write would hold
  the operation lock for as long as the proxy takes to give up. The resulting
  ``TimeoutError`` takes upstream's normal disconnect-and-retry path.
* ``SwitchbotBaseDevice._start_notify`` -- OVERRIDDEN only to pass the backend a
  ``timeout`` kwarg. bleak forwards it to the backend (bleak-esphome bounds each
  proxy round-trip with it and unregisters its notify handler on failure; other
  backends ignore it). Cancelling a subscribe from outside would abandon that
  handler on the proxy, so while subscribing the outer connect guard is only a
  safety net beyond the backend's own bound.
* ``SwitchbotBaseDevice._execute_disconnect_with_lock`` -- WRAPPED. Upstream
  clears ``_client`` before it awaits ``client.disconnect()``, so a cancelled
  disconnect would lose the only handle to a still-open link. We keep that
  client as ``_pending_disconnect`` until the disconnect is confirmed, and
  every release/connect path finishes it first.
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
``_execute_timed_disconnect``, ``_execute_disconnect``, and the "skip if the
timer was reset" guard in ``_execute_disconnect_with_lock`` (what lets a
re-armed timer cancel an in-flight disconnect),
``_send_command``/``_send_command_locked_with_retry`` and the whole protocol.
"""

from __future__ import annotations

import asyncio
import contextvars
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

# Hard bounds for GATT steps the libraries leave unbounded, so no single step
# can hang for long: cleaning up after a failed connect, and one command
# exchange (write, then wait for the reply; the reply wait is 5 s on its own).
# bleak's ``write_gatt_char`` has no timeout parameter to pass, and a cancelled
# write registers nothing on the proxy, so cancelling it is safe.
DISCONNECT_CLEANUP_TIMEOUT = 5
COMMAND_EXCHANGE_TIMEOUT = 10

# A subscribe must never be cancelled mid-flight (bleak-esphome registers its
# notify handler before the proxy acknowledges and only removes it on an error
# from inside). The backend is handed this per-round-trip timeout instead so its
# own error path runs; a subscribe is at most two round-trips on a proxy. The
# margin keeps the outer guard only as a safety net for a backend that ignores it.
NOTIFY_BACKEND_TIMEOUT = 4.0
NOTIFY_SAFETY_MARGIN = 2.0

# The connect guard of the connect in progress in *this* task, so the subscribe
# step can relax it (concurrent callers each have their own).
_CONNECT_GUARD: contextvars.ContextVar[asyncio.Timeout | None] = contextvars.ContextVar(
    "switchbot_connect_guard", default=None
)


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
        # A client whose disconnect was cancelled or failed before it was
        # confirmed: still possibly connected, and no longer referenced by the
        # library. Finished by the next connect or release.
        self._pending_disconnect: Any | None = None
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
        # A link a cancelled disconnect left behind would still hold the device
        # (and a proxy slot) while the new attempt tries to connect.
        await self._release_pending_disconnect()
        if self._closing:
            raise SwitchbotOperationError(_CLOSING_MESSAGE)
        timeout = self._policy.options.connect_timeout
        try:
            async with asyncio.timeout(timeout) as guard:
                token = _CONNECT_GUARD.set(guard)
                try:
                    await super()._ensure_connected()
                finally:
                    _CONNECT_GUARD.reset(token)
        except TimeoutError:
            _LOGGER.debug(
                "%s: Connect attempt exceeded %ss; failing fast so the next "
                "attempt can pick another proxy",
                self.name,
                timeout,
            )
            await self._bounded_forced_disconnect()
            raise
        if self._closing:
            # Shutdown began while this connect was in flight: hand the link back.
            await self._execute_forced_disconnect()
            raise SwitchbotOperationError(_CLOSING_MESSAGE)

    async def _start_notify(self) -> None:
        """Subscribe, with the backend's own timeout doing the bounding.

        The connect guard is relaxed to a safety net first, so it cannot cancel
        the subscribe half-way and leave a notify handler registered on the proxy.
        """
        per_round_trip = min(
            NOTIFY_BACKEND_TIMEOUT, self._policy.options.connect_timeout / 2
        )
        guard = _CONNECT_GUARD.get()
        if guard is not None and not guard.expired():
            guard.reschedule(
                asyncio.get_running_loop().time()
                + 2 * per_round_trip
                + NOTIFY_SAFETY_MARGIN
            )
        _LOGGER.debug("%s: Subscribe to notifications; RSSI: %s", self.name, self.rssi)
        await self._client.start_notify(
            self._read_char, self._notification_handler, timeout=per_round_trip
        )

    async def _execute_disconnect_with_lock(self) -> None:
        """Disconnect, remembering the client until the disconnect is confirmed.

        Upstream forgets the client before awaiting its ``disconnect()``. If that
        is cancelled (our cleanup bound) or fails, the link may still be up, so
        the client is kept for the next connect or release to finish.
        """
        client = self._client
        try:
            await super()._execute_disconnect_with_lock()
        except BaseException:
            if client is not None and self._client is None:
                self._pending_disconnect = client
            raise

    async def _release_pending_disconnect(self) -> None:
        """Finish a disconnect that was cancelled half-way, within the cleanup bound."""
        client = self._pending_disconnect
        if client is None:
            return
        if client.is_connected:
            try:
                async with asyncio.timeout(DISCONNECT_CLEANUP_TIMEOUT):
                    await client.disconnect()
            except Exception as err:  # noqa: BLE001 - still pending, try again later
                _LOGGER.debug(
                    "%s: Finishing the earlier disconnect failed: %r", self.name, err
                )
        if not client.is_connected and self._pending_disconnect is client:
            self._pending_disconnect = None

    async def _bounded_forced_disconnect(self) -> None:
        """Drop a half-open link after a failed connect, but never wait on it for long."""
        try:
            async with asyncio.timeout(DISCONNECT_CLEANUP_TIMEOUT):
                await self._execute_forced_disconnect()
        except TimeoutError:
            _LOGGER.debug("%s: Cleanup disconnect did not finish in time", self.name)

    async def _execute_command_locked(self, key: str, command: bytes) -> bytes:
        """Send one command, but never let a stuck write wedge the device's lock.

        ``TimeoutError`` is a bleak retry exception, so pySwitchbot disconnects and
        retries exactly as it does for any other failed exchange.
        """
        async with asyncio.timeout(COMMAND_EXCHANGE_TIMEOUT):
            return await super()._execute_command_locked(key, command)

    def _disconnected(self, client: Any) -> None:
        """Record unexpected drops on top of upstream's handling.

        Once the shutdown latch is set every disconnect is deliberate (ours, or the proxy going
        down with Home Assistant): not a fault, so it is neither counted nor warned about.
        """
        if client is self._pending_disconnect:
            self._pending_disconnect = None
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
        await self._release_pending_disconnect()
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

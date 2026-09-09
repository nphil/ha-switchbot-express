"""Connection policy for SwitchBot Express.

Pure decision logic. This module imports nothing from Home Assistant, bleak or
pySwitchbot so it can be unit tested on a bare Python install, and so the one
interesting question this integration answers -- *how long do we keep the GATT
link open, and what do we do when it drops* -- is readable in one file.

The Home Assistant glue asks this module questions and applies the answers; it
never decides anything about linger, hold, prewarm or backoff itself.

Battery cost model, in one paragraph, because every default here follows from
it: a curtain that is connected keeps its radio awake for connection events at
the interval the proxy picked, which costs meaningfully more than advertising.
So the defaults keep the link only across a burst of adjustments (15 s after a
user command, and only after a *user* command), holding is opt-in and meant for
solar or mains fed units, and the supported way to get an instant first command
is to prewarm on intent -- the same trigger that will issue the command, fired
30-60 s earlier.
"""

from __future__ import annotations

import random
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

# pySwitchbot 2.4.1 switchbot/devices/device.py::DISCONNECT_DELAY. Used as the
# floor for every decision so that "SwitchBot Express does nothing" degrades
# exactly to upstream behaviour. device.py passes the library's own value in,
# so a library bump cannot silently drift from this default.
CORE_DISCONNECT_DELAY = 8.5

# Option keys. const.py re-exports these; the config flow, the entry options
# and this module all use the same strings.
CONF_LINGER_SECONDS = "linger_seconds"
CONF_HOLD_CONNECTION = "hold_connection"
CONF_PREWARM_SECONDS = "prewarm_seconds"
CONF_LOW_BATTERY_PERCENT = "low_battery_percent"
CONF_RETRY_COUNT = "retry_count"
CONF_CONNECT_TIMEOUT = "connect_timeout"

DEFAULT_LINGER_SECONDS = 15
DEFAULT_HOLD_CONNECTION = False
DEFAULT_PREWARM_SECONDS = 60
DEFAULT_LOW_BATTERY_PERCENT = 15
DEFAULT_RETRY_COUNT = 3
DEFAULT_CONNECT_TIMEOUT = 10

MIN_LINGER_SECONDS = 0
MAX_LINGER_SECONDS = 600
MIN_PREWARM_SECONDS = 5
MAX_PREWARM_SECONDS = 900
MIN_LOW_BATTERY_PERCENT = 0
MAX_LOW_BATTERY_PERCENT = 100
MIN_RETRY_COUNT = 0
MAX_RETRY_COUNT = 10
# bleak-retry-connector defaults to 4 attempts of up to 20 s (60 s safety
# timeout) per establish_connection call, so an unlucky proxy path can wedge a
# command for over a minute. We cap a single connect attempt instead and let
# the retry loop pick a freshly scored proxy.
MIN_CONNECT_TIMEOUT = 3
MAX_CONNECT_TIMEOUT = 60

# Reconnect backoff used only while holding the link, capped at 60 s.
RECONNECT_BACKOFF: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
RECONNECT_JITTER = 0.2

# Trailing window for the Connection sensor's drops_1h attribute.
DROP_WINDOW = timedelta(hours=1)


def utcnow() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _coerce_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """Return ``value`` as an int inside ``[minimum, maximum]``.

    Option values arrive from a NumberSelector as floats, and from imported or
    hand edited entries as anything at all.
    """
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return min(max(result, minimum), maximum)


@dataclass(frozen=True, slots=True)
class PolicyOptions:
    """The per-entry knobs, already validated."""

    linger_seconds: int = DEFAULT_LINGER_SECONDS
    hold_connection: bool = DEFAULT_HOLD_CONNECTION
    prewarm_seconds: int = DEFAULT_PREWARM_SECONDS
    low_battery_percent: int = DEFAULT_LOW_BATTERY_PERCENT
    retry_count: int = DEFAULT_RETRY_COUNT
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT

    @classmethod
    def from_mapping(cls, options: Mapping[str, Any] | None) -> PolicyOptions:
        """Build options from a config entry's options mapping."""
        data: Mapping[str, Any] = options or {}
        return cls(
            linger_seconds=_coerce_int(
                data.get(CONF_LINGER_SECONDS),
                DEFAULT_LINGER_SECONDS,
                MIN_LINGER_SECONDS,
                MAX_LINGER_SECONDS,
            ),
            hold_connection=bool(
                data.get(CONF_HOLD_CONNECTION, DEFAULT_HOLD_CONNECTION)
            ),
            prewarm_seconds=_coerce_int(
                data.get(CONF_PREWARM_SECONDS),
                DEFAULT_PREWARM_SECONDS,
                MIN_PREWARM_SECONDS,
                MAX_PREWARM_SECONDS,
            ),
            low_battery_percent=_coerce_int(
                data.get(CONF_LOW_BATTERY_PERCENT),
                DEFAULT_LOW_BATTERY_PERCENT,
                MIN_LOW_BATTERY_PERCENT,
                MAX_LOW_BATTERY_PERCENT,
            ),
            retry_count=_coerce_int(
                data.get(CONF_RETRY_COUNT),
                DEFAULT_RETRY_COUNT,
                MIN_RETRY_COUNT,
                MAX_RETRY_COUNT,
            ),
            connect_timeout=_coerce_int(
                data.get(CONF_CONNECT_TIMEOUT),
                DEFAULT_CONNECT_TIMEOUT,
                MIN_CONNECT_TIMEOUT,
                MAX_CONNECT_TIMEOUT,
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the options as a plain dict, for diagnostics."""
        return asdict(self)


DEFAULT_OPTIONS = PolicyOptions()


def reconnect_delay(
    attempt: int, *, rand: Callable[[], float] = random.random
) -> float:
    """Return the delay before reconnect ``attempt`` (1 based).

    1, 2, 5, 10, 30, 60 s, capped at 60 s, each with +-20 % jitter so a hub
    full of devices does not retry in lockstep.
    """
    index = min(max(attempt, 1), len(RECONNECT_BACKOFF)) - 1
    base = RECONNECT_BACKOFF[index]
    return base * (1.0 + RECONNECT_JITTER * (2.0 * rand() - 1.0))


class DropLog:
    """Unexpected disconnects in a trailing window."""

    # ``last_drop`` outlives the window: the sensor reports the last drop even
    # when it no longer counts towards drops_1h.
    __slots__ = ("_drops", "_last", "_utcnow", "_window")

    def __init__(
        self,
        *,
        window: timedelta = DROP_WINDOW,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        """Initialise an empty log."""
        self._drops: deque[datetime] = deque()
        self._last: datetime | None = None
        self._utcnow = clock
        self._window = window

    def note_drop(self, when: datetime | None = None) -> datetime:
        """Record an unexpected disconnect and return its timestamp."""
        moment = when if when is not None else self._utcnow()
        self._drops.append(moment)
        self._last = moment
        self._trim(moment)
        return moment

    def count(self, now: datetime | None = None) -> int:
        """Return the number of drops inside the trailing window."""
        self._trim(now if now is not None else self._utcnow())
        return len(self._drops)

    @property
    def last_drop(self) -> datetime | None:
        """Return the most recent drop, even if it fell out of the window."""
        return self._last

    def _trim(self, now: datetime) -> None:
        cutoff = now - self._window
        while self._drops and self._drops[0] <= cutoff:
            self._drops.popleft()


class ConnectionPolicy:
    """Decides how long a GATT link is kept open for one device."""

    def __init__(
        self,
        options: PolicyOptions | None = None,
        *,
        core_disconnect_delay: float = CORE_DISCONNECT_DELAY,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        """Initialise the policy for one device."""
        self._options = options or DEFAULT_OPTIONS
        self._core_disconnect_delay = float(core_disconnect_delay)
        self._monotonic = monotonic
        self._drops = DropLog(clock=clock)
        self._battery: int | None = None
        self._linger_until: float | None = None
        self._prewarm_until: float | None = None

    @property
    def options(self) -> PolicyOptions:
        """Return the current options."""
        return self._options

    def update_options(self, options: PolicyOptions) -> None:
        """Replace the options in place."""
        self._options = options

    @property
    def core_disconnect_delay(self) -> float:
        """Return the library's own idle disconnect delay."""
        return self._core_disconnect_delay

    # -- battery ---------------------------------------------------------

    @property
    def battery(self) -> int | None:
        """Return the last known battery percentage."""
        return self._battery

    def set_battery(self, battery: int | None) -> bool:
        """Record the battery level; return True if the saver state flipped."""
        was_suspended = self.battery_saver_active
        self._battery = battery
        return self.battery_saver_active is not was_suspended

    @property
    def battery_saver_active(self) -> bool:
        """Return True when the battery is at or below the threshold.

        An unknown battery is treated as fine: refusing to hold a link because
        we have not heard a level yet would be worse than the battery cost.
        """
        return (
            self._battery is not None
            and self._battery <= self._options.low_battery_percent
        )

    # -- windows ---------------------------------------------------------

    @property
    def hold_active(self) -> bool:
        """Return True when the link should be held open indefinitely."""
        return self._options.hold_connection and not self.battery_saver_active

    def note_user_command(self, now: float | None = None) -> None:
        """Arm the linger window; only user commands may call this.

        Polls, read-backs after a command and prewarm expiry deliberately do
        not arm it: a background poll must not buy the radio 15 s of wakeups.
        """
        if self._options.linger_seconds <= 0:
            self._linger_until = None
            return
        moment = self._monotonic() if now is None else now
        self._linger_until = moment + self._options.linger_seconds

    def note_prewarm(self, now: float | None = None) -> int:
        """Arm the prewarm window and return how long it lasts.

        Prewarm survives the battery saver on purpose: it is an explicit,
        bounded request made by the automation that is about to move the
        curtain, and it is the recommended way to pay no idle cost at all.
        """
        moment = self._monotonic() if now is None else now
        self._prewarm_until = moment + self._options.prewarm_seconds
        return self._options.prewarm_seconds

    def cancel_prewarm(self) -> None:
        """Drop any active prewarm window."""
        self._prewarm_until = None

    def linger_remaining(self, now: float | None = None) -> float:
        """Return the seconds of linger left, 0 when it is not in force."""
        if self.battery_saver_active or self._options.linger_seconds <= 0:
            return 0.0
        return self._remaining(self._linger_until, now)

    def prewarm_remaining(self, now: float | None = None) -> float:
        """Return the seconds of prewarm left, 0 when there is none."""
        return self._remaining(self._prewarm_until, now)

    def disconnect_delay(self, now: float | None = None) -> float | None:
        """Return the idle timeout to arm, or None to never disconnect.

        None means "holding": the caller must not arm an idle timer at all.
        Otherwise the answer is never below the library's own delay, so the
        worst case is exactly upstream behaviour.
        """
        if self.hold_active:
            return None
        moment = self._monotonic() if now is None else now
        delay = self._core_disconnect_delay
        prewarm = self.prewarm_remaining(moment)
        if prewarm > delay:
            delay = prewarm
        linger = self.linger_remaining(moment)
        if linger > delay:
            delay = linger
        return delay

    def _remaining(self, until: float | None, now: float | None) -> float:
        if until is None:
            return 0.0
        moment = self._monotonic() if now is None else now
        return max(until - moment, 0.0)

    # -- drops -----------------------------------------------------------

    def note_drop(self, when: datetime | None = None) -> None:
        """Record an unexpected disconnect."""
        self._drops.note_drop(when)

    def drops_1h(self, now: datetime | None = None) -> int:
        """Return unexpected disconnects in the trailing hour."""
        return self._drops.count(now)

    @property
    def last_drop(self) -> datetime | None:
        """Return the last unexpected disconnect, or None."""
        return self._drops.last_drop

    # -- diagnostics -----------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        """Return the policy state, for diagnostics."""
        last_drop = self.last_drop
        return {
            "options": self._options.as_dict(),
            "battery": self._battery,
            "battery_saver_active": self.battery_saver_active,
            "hold_active": self.hold_active,
            "linger_remaining": round(self.linger_remaining(), 1),
            "prewarm_remaining": round(self.prewarm_remaining(), 1),
            "core_disconnect_delay": self._core_disconnect_delay,
            "drops_1h": self.drops_1h(),
            "last_drop": last_drop.isoformat() if last_drop else None,
        }

"""Behavioural tests for the connection policy.

Everything that decides when the radio is awake, tested without Home
Assistant: which events arm the linger window (and which deliberately do not),
that a flat battery suspends linger and hold and that recovery restores them,
that prewarm expires, that the reconnect backoff walks its sequence inside its
jitter bounds, and that drops_1h really is a trailing hour.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "custom_components" / "switchbot"),
)

from policy import (  # noqa: E402
    CORE_DISCONNECT_DELAY,
    DEFAULT_LINGER_SECONDS,
    DEFAULT_LOW_BATTERY_PERCENT,
    DEFAULT_PREWARM_SECONDS,
    MAX_PREWARM_SECONDS,
    RECONNECT_BACKOFF,
    RECONNECT_JITTER,
    ConnectionPolicy,
    DropLog,
    PolicyOptions,
    reconnect_delay,
)


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_policy(clock: FakeClock | None = None, **options: object) -> ConnectionPolicy:
    """Build a policy with a hand driven clock."""
    return ConnectionPolicy(
        PolicyOptions(**options),  # type: ignore[arg-type]
        monotonic=clock or FakeClock(),
    )


# --- defaults ---------------------------------------------------------------


def test_defaults_match_the_documented_contract() -> None:
    """The README, the options flow and the policy must agree."""
    options = PolicyOptions()
    assert options.linger_seconds == 15
    assert options.hold_connection is False
    assert options.prewarm_seconds == 60
    assert options.low_battery_percent == 15
    assert options.retry_count == 3


def test_options_are_coerced_and_clamped() -> None:
    """NumberSelector hands back floats, and entries can hold junk."""
    options = PolicyOptions.from_mapping(
        {
            "linger_seconds": 30.0,
            "hold_connection": True,
            "prewarm_seconds": 10_000,
            "low_battery_percent": -5,
            "retry_count": "not a number",
        }
    )
    assert options.linger_seconds == 30
    assert options.hold_connection is True
    assert options.prewarm_seconds == MAX_PREWARM_SECONDS
    assert options.low_battery_percent == 0
    assert options.retry_count == 3


# --- linger -----------------------------------------------------------------


def test_idle_link_uses_the_library_delay() -> None:
    """With nothing armed we are exactly upstream pySwitchbot."""
    policy = make_policy()
    assert policy.disconnect_delay() == CORE_DISCONNECT_DELAY


def test_user_command_arms_linger() -> None:
    clock = FakeClock()
    policy = make_policy(clock)
    policy.note_user_command()
    assert policy.disconnect_delay() == pytest.approx(DEFAULT_LINGER_SECONDS)


def test_linger_counts_down_and_expires_to_the_library_delay() -> None:
    """A read-back mid-command sees the *remaining* window, not a fresh one."""
    clock = FakeClock()
    policy = make_policy(clock)
    policy.note_user_command()

    clock.advance(2)
    assert policy.disconnect_delay() == pytest.approx(DEFAULT_LINGER_SECONDS - 2)

    clock.advance(DEFAULT_LINGER_SECONDS)
    assert policy.disconnect_delay() == CORE_DISCONNECT_DELAY


def test_each_user_command_resets_linger() -> None:
    clock = FakeClock()
    policy = make_policy(clock, linger_seconds=20)
    policy.note_user_command()
    clock.advance(15)
    policy.note_user_command()
    assert policy.disconnect_delay() == pytest.approx(20)


def test_linger_zero_is_core_behaviour() -> None:
    """0 opts out: the link goes as soon as the library would drop it."""
    policy = make_policy(linger_seconds=0)
    policy.note_user_command()
    assert policy.disconnect_delay() == CORE_DISCONNECT_DELAY


def test_polls_and_readbacks_do_not_arm_linger() -> None:
    """Only note_user_command arms it; nothing else in the policy can."""
    clock = FakeClock()
    policy = make_policy(clock)
    # A poll is simply the absence of note_user_command.
    assert policy.disconnect_delay() == CORE_DISCONNECT_DELAY
    policy.note_user_command()
    clock.advance(DEFAULT_LINGER_SECONDS + 1)
    # Window gone; a later poll must not resurrect it.
    assert policy.disconnect_delay() == CORE_DISCONNECT_DELAY


# --- hold -------------------------------------------------------------------


def test_hold_is_off_by_default() -> None:
    assert make_policy().hold_active is False


def test_hold_never_arms_a_timer() -> None:
    policy = make_policy(hold_connection=True)
    assert policy.hold_active is True
    assert policy.disconnect_delay() is None


def test_hold_outranks_an_armed_linger() -> None:
    policy = make_policy(hold_connection=True)
    policy.note_user_command()
    assert policy.disconnect_delay() is None


# --- battery ----------------------------------------------------------------


def test_low_battery_suspends_hold_and_linger_then_recovery_restores_them() -> None:
    clock = FakeClock()
    policy = make_policy(clock, hold_connection=True)
    policy.note_user_command()
    assert policy.disconnect_delay() is None

    changed = policy.set_battery(4)
    assert changed is True
    assert policy.battery_saver_active is True
    assert policy.hold_active is False
    assert policy.disconnect_delay() == CORE_DISCONNECT_DELAY

    changed = policy.set_battery(80)
    assert changed is True
    assert policy.battery_saver_active is False
    assert policy.hold_active is True
    assert policy.disconnect_delay() is None


def test_threshold_is_inclusive_and_configurable() -> None:
    policy = make_policy(hold_connection=True)
    policy.set_battery(DEFAULT_LOW_BATTERY_PERCENT)
    assert policy.battery_saver_active is True

    policy.set_battery(DEFAULT_LOW_BATTERY_PERCENT + 1)
    assert policy.battery_saver_active is False

    generous = make_policy(hold_connection=True, low_battery_percent=50)
    generous.set_battery(40)
    assert generous.battery_saver_active is True


def test_unknown_battery_does_not_suspend() -> None:
    policy = make_policy(hold_connection=True)
    assert policy.battery is None
    assert policy.hold_active is True


def test_set_battery_reports_only_real_transitions() -> None:
    policy = make_policy()
    assert policy.set_battery(90) is False
    assert policy.set_battery(80) is False
    assert policy.set_battery(3) is True
    assert policy.set_battery(2) is False


# --- prewarm ----------------------------------------------------------------


def test_prewarm_holds_the_link_for_its_window_then_expires() -> None:
    clock = FakeClock()
    policy = make_policy(clock)
    assert policy.note_prewarm() == DEFAULT_PREWARM_SECONDS
    assert policy.disconnect_delay() == pytest.approx(DEFAULT_PREWARM_SECONDS)

    clock.advance(DEFAULT_PREWARM_SECONDS - 1)
    assert policy.disconnect_delay() == CORE_DISCONNECT_DELAY  # 1 s < 8.5 s floor

    clock.advance(2)
    assert policy.prewarm_remaining() == 0.0
    assert policy.disconnect_delay() == CORE_DISCONNECT_DELAY


def test_prewarm_expiry_does_not_arm_linger() -> None:
    """The window ends in the library's delay, never in a linger window."""
    clock = FakeClock()
    policy = make_policy(clock, linger_seconds=600)
    policy.note_prewarm()
    clock.advance(DEFAULT_PREWARM_SECONDS + 1)
    assert policy.disconnect_delay() == CORE_DISCONNECT_DELAY


def test_prewarm_survives_a_flat_battery() -> None:
    """Explicit, bounded and the whole point on a 4 % curtain."""
    policy = make_policy()
    policy.set_battery(4)
    policy.note_prewarm()
    assert policy.disconnect_delay() == pytest.approx(DEFAULT_PREWARM_SECONDS)


def test_command_during_prewarm_takes_the_longer_window() -> None:
    clock = FakeClock()
    policy = make_policy(clock, linger_seconds=120)
    policy.note_prewarm()
    policy.note_user_command()
    assert policy.disconnect_delay() == pytest.approx(120)


def test_cancel_prewarm() -> None:
    policy = make_policy()
    policy.note_prewarm()
    policy.cancel_prewarm()
    assert policy.disconnect_delay() == CORE_DISCONNECT_DELAY


# --- backoff ----------------------------------------------------------------


def test_backoff_walks_the_sequence_without_jitter() -> None:
    no_jitter = lambda: 0.5  # noqa: E731 - midpoint means zero jitter
    delays = [reconnect_delay(n, rand=no_jitter) for n in range(1, 7)]
    assert delays == [1.0, 2.0, 5.0, 10.0, 30.0, 60.0]


def test_backoff_caps_at_sixty() -> None:
    no_jitter = lambda: 0.5  # noqa: E731
    assert reconnect_delay(7, rand=no_jitter) == 60.0
    assert reconnect_delay(99, rand=no_jitter) == 60.0


def test_backoff_first_attempt_is_never_below_one() -> None:
    no_jitter = lambda: 0.5  # noqa: E731
    assert reconnect_delay(0, rand=no_jitter) == 1.0


@pytest.mark.parametrize("attempt", range(1, 8))
def test_jitter_stays_inside_twenty_percent(attempt: int) -> None:
    base = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF)) - 1]
    lowest = reconnect_delay(attempt, rand=lambda: 0.0)
    highest = reconnect_delay(attempt, rand=lambda: 1.0)
    assert lowest == pytest.approx(base * (1 - RECONNECT_JITTER))
    assert highest == pytest.approx(base * (1 + RECONNECT_JITTER))
    for value in (lowest, highest, reconnect_delay(attempt)):
        assert base * 0.8 <= value <= base * 1.2


# --- drops ------------------------------------------------------------------


def test_drops_counts_only_the_trailing_hour() -> None:
    start = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
    log = DropLog()
    log.note_drop(start)
    log.note_drop(start + timedelta(minutes=30))
    log.note_drop(start + timedelta(minutes=50))

    assert log.count(start + timedelta(minutes=55)) == 3
    # The first drop is now 61 minutes old.
    assert log.count(start + timedelta(minutes=61)) == 2
    assert log.count(start + timedelta(hours=3)) == 0


def test_last_drop_outlives_the_window() -> None:
    start = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
    log = DropLog()
    assert log.last_drop is None
    log.note_drop(start)
    assert log.count(start + timedelta(hours=5)) == 0
    assert log.last_drop == start


def test_policy_exposes_drops_for_the_connection_sensor() -> None:
    start = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
    policy = ConnectionPolicy(clock=lambda: start)
    policy.note_drop(start - timedelta(minutes=90))
    policy.note_drop(start - timedelta(minutes=10))
    assert policy.drops_1h(start) == 1
    assert policy.last_drop == start - timedelta(minutes=10)


def test_as_dict_is_json_friendly() -> None:
    policy = make_policy()
    policy.set_battery(42)
    state = policy.as_dict()
    assert state["battery"] == 42
    assert state["hold_active"] is False
    assert state["last_drop"] is None
    assert state["options"]["linger_seconds"] == DEFAULT_LINGER_SECONDS

"""Setup never waits on the radio: it returns inside its budget, whatever the device does.

Home Assistant reports "started" only after every integration's setup returned, so
setup waiting for a SwitchBot that is out of range, a proxy that is busy or a GATT
step that hangs delays the whole restart. These tests pin the contract:

* setup returns inside ``SETUP_BUDGET`` when nothing ever answers (and stays loaded),
* entities that depend on the first advertisement appear when it arrives,
* data arriving (or coming back after unavailable) never moves a curtain,
* no single GATT step can hang for long.

Like ``test_shutdown_release.py`` they need Home Assistant, pySwitchbot and
pytest-homeassistant-custom-component and are skipped where those are absent::

    PYTHONPATH=. python -m pytest tests/test_startup_budget.py -o asyncio_mode=auto
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")
pytest.importorskip("switchbot")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bleak.backends.device import BLEDevice  # noqa: E402
from bleak.backends.scanner import AdvertisementData  # noqa: E402
from switchbot import SwitchBotAdvertisement, SwitchbotModel  # noqa: E402

from homeassistant.components.bluetooth import (  # noqa: E402
    BluetoothChange,
    BluetoothServiceInfoBleak,
)
from homeassistant.components.bluetooth.active_update_coordinator import (  # noqa: E402
    ActiveBluetoothDataUpdateCoordinator,
)
from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.const import CONF_ADDRESS, CONF_NAME, STATE_UNAVAILABLE  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.switchbot_express import device as device_module  # noqa: E402
from custom_components.switchbot_express.const import CONF_DEVICE_TYPE, DOMAIN  # noqa: E402
from custom_components.switchbot_express.device import (  # noqa: E402
    SwitchbotExpressCurtain,
    create_device,
)
from custom_components.switchbot_express.policy import (  # noqa: E402
    ConnectionPolicy,
    PolicyOptions,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"
BUDGET = 0.5  # stands in for the real 5 s so the suite stays fast


class FakeGatt:
    """The subset of a bleak client that pySwitchbot touches, with hangs on demand."""

    def __init__(self) -> None:
        self.connected = True
        self.services = MagicMock()
        # A subscribe the proxy never acknowledges. Like bleak-esphome it ends by
        # itself after the ``timeout`` kwarg when it gets one; a backend that
        # ignores the kwarg (``notify_honours_timeout = False``) hangs forever.
        self.hang_on_start_notify = False
        self.notify_honours_timeout = True
        self.notify_timeouts: list[float | None] = []
        self.notify_cancelled = 0
        self.hang_on_write = False
        self.hang_on_disconnect = False
        self.hang_next_disconnects = 0
        self.disconnect_calls = 0

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def start_notify(self, *args, **kwargs) -> None:
        self.notify_timeouts.append(kwargs.get("timeout"))
        if not self.hang_on_start_notify:
            return
        try:
            if self.notify_honours_timeout and "timeout" in kwargs:
                await asyncio.sleep(kwargs["timeout"])
                raise TimeoutError("the proxy did not acknowledge the subscribe")
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.notify_cancelled += 1  # would leave a handler registered on a real proxy
            raise

    async def write_gatt_char(self, *args, **kwargs) -> None:
        if self.hang_on_write:
            await asyncio.sleep(3600)

    async def clear_cache(self) -> None:
        return None

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        if self.hang_next_disconnects > 0:
            self.hang_next_disconnects -= 1
            await asyncio.sleep(3600)
        if self.hang_on_disconnect:
            await asyncio.sleep(3600)
        self.connected = False


@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load this repo's custom_components as real integrations."""


@pytest.fixture
def gatt() -> FakeGatt:
    return FakeGatt()


@pytest.fixture
def connects(gatt: FakeGatt):
    """Count ``establish_connection`` calls; a test can make it hang forever."""
    state = {"n": 0, "hang": False, "delay": 0.0}

    async def _establish(*args, **kwargs):
        state["n"] += 1
        if state["hang"]:
            await asyncio.sleep(3600)
        if state["delay"]:
            await asyncio.sleep(state["delay"])
        gatt.connected = True
        return gatt

    with patch("switchbot.devices.device.establish_connection", side_effect=_establish):
        yield state


@pytest.fixture
def ble_env(hass, mock_bluetooth, gatt, connects):
    """The BLE layer is faked and *silent*: no advertisement ever arrives on its own."""
    ble_device = BLEDevice(ADDRESS, "WoCurtain", {})
    with (
        patch(
            "custom_components.switchbot_express.bluetooth.async_ble_device_from_address",
            return_value=ble_device,
        ),
        patch("switchbot.close_stale_connections_by_address", new=AsyncMock()),
        patch("custom_components.switchbot_express.SETUP_BUDGET", BUDGET),
    ):
        yield ble_device


def _new_entry(hass, **options) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="Living Room Curtain",
        data={CONF_ADDRESS: ADDRESS, CONF_NAME: "Living Room Curtain", CONF_DEVICE_TYPE: "curtain"},
        options={"hold_connection": False, "linger_seconds": 15, "prewarm_seconds": 60, **options},
    )
    entry.add_to_hass(hass)
    return entry


async def _setup_timed(hass, entry) -> float:
    started = time.monotonic()
    assert await hass.config_entries.async_setup(entry.entry_id)
    elapsed = time.monotonic() - started
    await hass.async_block_till_done()
    return elapsed


def _advertisement(ble_device: BLEDevice) -> BluetoothServiceInfoBleak:
    return BluetoothServiceInfoBleak(
        name="WoCurtain",
        address=ADDRESS,
        rssi=-60,
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        source="local",
        device=ble_device,
        advertisement=AdvertisementData("WoCurtain", {}, {}, [], None, -60, ()),
        connectable=True,
        time=time.monotonic(),
        tx_power=None,
    )


@contextlib.contextmanager
def _parsed(ble_device: BLEDevice, **data):
    """Make the coordinator read advertisements as a Curtain advertising ``data``.

    Polling is switched off: a poll is a read-only connect that is not what these
    tests are about, and its debounce timer would outlive the test.
    """
    with (
        patch(
            "custom_components.switchbot_express.coordinator.switchbot.parse_advertisement_data",
            return_value=SwitchBotAdvertisement(
                address=ADDRESS,
                data={"modelName": SwitchbotModel.CURTAIN, "data": data},
                device=ble_device,
                rssi=-60,
            ),
        ),
        patch.object(ActiveBluetoothDataUpdateCoordinator, "needs_poll", return_value=False),
    ):
        yield


# -- (a) setup returns inside the budget when nothing ever answers ------------


async def test_setup_returns_inside_budget_when_the_device_never_advertises(
    hass, ble_env, connects
) -> None:
    entry = _new_entry(hass)

    elapsed = await _setup_timed(hass, entry)

    assert BUDGET * 0.8 < elapsed < BUDGET + 1.0  # waited its budget, not 30 s
    assert entry.state is ConfigEntryState.LOADED  # no retry backoff on top
    assert connects["n"] == 0


async def test_one_budget_covers_the_stale_link_cleanup_and_the_wait(hass, ble_env, connects) -> None:
    """A hanging BlueZ cleanup and a silent device share the budget; they do not add up."""
    entry = _new_entry(hass)

    async def _hang(address: str) -> None:
        await asyncio.sleep(3600)

    with patch("switchbot.close_stale_connections_by_address", new=_hang):
        elapsed = await _setup_timed(hass, entry)

    assert elapsed < BUDGET * 1.8  # two serial waits would be >= 2 * BUDGET
    assert entry.state is ConfigEntryState.LOADED


async def test_setup_returns_inside_budget_while_a_hold_connect_hangs_forever(
    hass, ble_env, connects
) -> None:
    connects["hang"] = True
    entry = _new_entry(hass, hold_connection=True)

    with patch("custom_components.switchbot_express.device.reconnect_delay", return_value=0):
        elapsed = await _setup_timed(hass, entry)
        await asyncio.sleep(0.05)  # the supervisor's first attempt is now in flight

    assert elapsed < BUDGET + 1.0
    assert entry.state is ConfigEntryState.LOADED
    assert connects["n"] >= 1  # connecting happens, but in the background
    assert entry.runtime_data._supervisor_task is not None  # noqa: SLF001


async def test_silent_device_leaves_entities_unavailable_not_invented(hass, ble_env) -> None:
    entry = _new_entry(hass)
    await _setup_timed(hass, entry)

    registry = er.async_get(hass)
    cover_id = next(e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id) if e.domain == "cover")
    assert hass.states.get(cover_id).state == STATE_UNAVAILABLE


# -- (b) entities populate when data arrives after setup returned -------------


def _entities(hass, entry) -> dict[str, str]:
    """unique_id suffix -> entity_id, for what exists in the registry."""
    return {
        e.unique_id: e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }


async def test_entities_that_depend_on_data_appear_when_the_first_advertisement_arrives(
    hass, ble_env
) -> None:
    entry = _new_entry(hass)
    await _setup_timed(hass, entry)
    before = _entities(hass, entry)
    assert f"{ADDRESS}_battery" not in before
    assert f"{ADDRESS}_lightLevel" not in before
    assert f"{ADDRESS}_calibration" not in before

    with _parsed(ble_env, position=40, battery=88, lightLevel=3, calibration=True):
        entry.runtime_data._async_handle_bluetooth_event(  # noqa: SLF001
            _advertisement(ble_env), BluetoothChange.ADVERTISEMENT
        )
        await hass.async_block_till_done()

    after = _entities(hass, entry)
    assert {f"{ADDRESS}_battery", f"{ADDRESS}_lightLevel", f"{ADDRESS}_calibration"} <= set(after)
    assert hass.states.get(after[f"{ADDRESS}_battery"]).state == "88"
    assert hass.states.get(after[f"{ADDRESS}_calibration"]).state == "on"
    cover = hass.states.get(next(v for k, v in after.items() if v.startswith("cover.")))
    assert cover.state == "open"
    assert cover.attributes["current_position"] == 40
    # Same entities as before, plus the new ones: nothing was duplicated or renamed.
    assert set(before) <= set(after)


async def test_late_entities_are_added_once_even_if_more_advertisements_follow(hass, ble_env) -> None:
    entry = _new_entry(hass)
    await _setup_timed(hass, entry)
    coordinator = entry.runtime_data

    with _parsed(ble_env, position=40, battery=88):
        for _ in range(3):
            coordinator._async_handle_bluetooth_event(  # noqa: SLF001
                _advertisement(ble_env), BluetoothChange.ADVERTISEMENT
            )
            await hass.async_block_till_done()

    batteries = [u for u in _entities(hass, entry) if u.endswith("_battery")]
    assert batteries == [f"{ADDRESS}_battery"]


# -- (c) nothing is actuated when data arrives or comes back ------------------


async def test_data_arriving_or_returning_from_unavailable_never_moves_the_curtain(
    hass, ble_env, connects
) -> None:
    entry = _new_entry(hass)
    await _setup_timed(hass, entry)
    coordinator = entry.runtime_data
    moves = {
        name: AsyncMock(return_value=True)
        for name in ("open", "close", "stop", "set_position")
    }

    with (
        _parsed(ble_env, position=40, battery=88),
        patch.multiple(SwitchbotExpressCurtain, **moves),
    ):
        info = _advertisement(ble_env)
        coordinator._async_handle_bluetooth_event(info, BluetoothChange.ADVERTISEMENT)  # noqa: SLF001
        await hass.async_block_till_done()
        coordinator._async_handle_unavailable(info)  # noqa: SLF001
        coordinator._async_handle_bluetooth_event(info, BluetoothChange.ADVERTISEMENT)  # noqa: SLF001
        await hass.async_block_till_done()

    assert all(not move.called for move in moves.values())
    assert connects["n"] == 0


# -- S4: no single GATT step hangs for long -----------------------------------


def _fast_device(connect_timeout: float = 0.2):
    ble_device = BLEDevice(ADDRESS, "WoCurtain", {})
    policy = ConnectionPolicy(
        dataclasses.replace(PolicyOptions.from_mapping({}), connect_timeout=connect_timeout),
        core_disconnect_delay=8.5,
    )
    return create_device("curtain", ble_device, policy)


async def test_a_connect_that_never_completes_is_cut_at_the_connect_timeout(connects) -> None:
    connects["hang"] = True
    device = _fast_device()

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await device.async_ensure_connected()

    assert time.monotonic() - started < 1.5


async def test_a_subscribe_the_proxy_never_acknowledges_gets_a_backend_timeout_below_the_guard(
    gatt, connects
) -> None:
    """S8: the backend's own ``timeout`` ends the wait, so the subscribe is never cancelled."""
    gatt.hang_on_start_notify = True
    device = _fast_device(connect_timeout=10)

    started = time.monotonic()
    with (
        patch.object(device_module, "NOTIFY_BACKEND_TIMEOUT", 0.3),
        pytest.raises(TimeoutError),
    ):
        await device.async_ensure_connected()

    assert time.monotonic() - started < 1.5
    (timeout,) = gatt.notify_timeouts
    assert timeout == 0.3 and timeout < 10  # passed as a kwarg, below the outer guard
    assert gatt.notify_cancelled == 0


async def test_the_default_subscribe_timeout_is_four_seconds_a_round_trip(gatt, connects) -> None:
    device = _fast_device(connect_timeout=10)
    await device.async_ensure_connected()
    assert gatt.notify_timeouts == [4.0]
    await device.async_release()


async def test_a_slow_connect_does_not_let_the_guard_cancel_the_subscribe(gatt, connects) -> None:
    """S8: connect used up most of the guard; the guard must not cut into the subscribe."""
    gatt.hang_on_start_notify = True
    connects["delay"] = 0.8
    device = _fast_device(connect_timeout=1.0)

    with (
        patch.object(device_module, "NOTIFY_BACKEND_TIMEOUT", 0.3),
        pytest.raises(TimeoutError),
    ):
        await device.async_ensure_connected()

    # 0.8 s connect + 0.3 s subscribe is past the 1.0 s guard; the backend ended it, not a cancel.
    assert gatt.notify_timeouts == [0.3]
    assert gatt.notify_cancelled == 0


async def test_a_backend_that_ignores_the_timeout_is_still_bounded_by_the_safety_net(
    gatt, connects
) -> None:
    gatt.hang_on_start_notify = True
    gatt.notify_honours_timeout = False
    gatt.hang_on_disconnect = True
    device = _fast_device(connect_timeout=10)

    started = time.monotonic()
    with (
        patch.object(device_module, "NOTIFY_BACKEND_TIMEOUT", 0.1),
        patch.object(device_module, "NOTIFY_SAFETY_MARGIN", 0.2),
        patch.object(device_module, "DISCONNECT_CLEANUP_TIMEOUT", 0.2),
        pytest.raises(TimeoutError),
    ):
        await device.async_ensure_connected()

    assert time.monotonic() - started < 1.5  # safety net 0.4 s, then the bounded cleanup


# -- a cancelled disconnect must not lose the only handle to an open link -----


async def test_a_cancelled_disconnect_keeps_the_link_and_shutdown_releases_it(gatt, connects) -> None:
    device = _fast_device(connect_timeout=10)
    await device.async_ensure_connected()
    gatt.hang_next_disconnects = 1  # the first disconnect hangs, the retry works

    with patch.object(device_module, "DISCONNECT_CLEANUP_TIMEOUT", 0.2):
        await device._bounded_forced_disconnect()  # noqa: SLF001 - cut after 0.2 s, does not raise
    assert gatt.connected  # the proxy link is still open ...
    assert device._pending_disconnect is gatt  # noqa: SLF001 - ... and we still own it

    await device.async_release_for_shutdown()

    assert gatt.connected is False
    assert gatt.disconnect_calls == 2
    assert device._pending_disconnect is None  # noqa: SLF001
    assert device.closing


async def test_the_next_connect_finishes_the_stuck_disconnect_first(gatt) -> None:
    seen_connected: list[bool] = []

    async def _establish(*args, **kwargs):
        seen_connected.append(gatt.connected)
        gatt.connected = True
        return gatt

    gatt.connected = False
    device = _fast_device(connect_timeout=10)
    with patch("switchbot.devices.device.establish_connection", side_effect=_establish):
        await device.async_ensure_connected()
        gatt.hang_next_disconnects = 1
        with patch.object(device_module, "DISCONNECT_CLEANUP_TIMEOUT", 0.2):
            await device._bounded_forced_disconnect()  # noqa: SLF001
        assert gatt.connected

        await device.async_ensure_connected()  # the supervisor's / a command's retry

    assert seen_connected == [False, False]  # the old link was gone before the new connect
    assert device._pending_disconnect is None  # noqa: SLF001
    await device.async_release()


async def test_a_link_that_dropped_by_itself_is_no_longer_pending(gatt, connects) -> None:
    device = _fast_device(connect_timeout=10)
    await device.async_ensure_connected()
    gatt.hang_next_disconnects = 1
    with patch.object(device_module, "DISCONNECT_CLEANUP_TIMEOUT", 0.2):
        await device._bounded_forced_disconnect()  # noqa: SLF001
    assert device._pending_disconnect is gatt  # noqa: SLF001

    gatt.connected = False
    device._disconnected(gatt)  # noqa: SLF001 - the proxy reports the link gone

    assert device._pending_disconnect is None  # noqa: SLF001


async def test_a_write_that_hangs_is_cut_so_the_retry_logic_takes_over(gatt, connects) -> None:
    device = _fast_device()
    await device.async_ensure_connected()
    gatt.hang_on_write = True

    started = time.monotonic()
    with (
        patch.object(device_module, "COMMAND_EXCHANGE_TIMEOUT", 0.2),
        pytest.raises(TimeoutError),
    ):
        await device._execute_command_locked("get_basic_info", b"\x57\x0f\x45\x01")  # noqa: SLF001

    assert time.monotonic() - started < 1.5
    await device.async_release()  # no idle timer left running after the test


# -- discovery stops before the platforms unload ------------------------------


async def test_data_arriving_while_the_platforms_unload_adds_nothing(hass, ble_env) -> None:
    entry = _new_entry(hass)
    await _setup_timed(hass, entry)
    coordinator = entry.runtime_data
    real_unload_platforms = hass.config_entries.async_unload_platforms
    reached = asyncio.Event()
    proceed = asyncio.Event()

    async def _slow_unload_platforms(*args, **kwargs):
        reached.set()
        await proceed.wait()
        return await real_unload_platforms(*args, **kwargs)

    with (
        patch.object(hass.config_entries, "async_unload_platforms", _slow_unload_platforms),
        _parsed(ble_env, position=40, battery=88, lightLevel=3),
    ):
        unloading = asyncio.create_task(hass.config_entries.async_unload(entry.entry_id))
        await reached.wait()

        coordinator._async_handle_bluetooth_event(  # noqa: SLF001 - first data, mid-unload
            _advertisement(ble_env), BluetoothChange.ADVERTISEMENT
        )
        await hass.async_block_till_done()
        assert not [u for u in _entities(hass, entry) if u.endswith(("_battery", "_lightLevel"))]

        proceed.set()
        assert await unloading
    await hass.async_block_till_done()

    assert not [u for u in _entities(hass, entry) if u.endswith(("_battery", "_lightLevel"))]
    assert not coordinator.discovery_open

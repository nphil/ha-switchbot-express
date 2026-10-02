"""Home Assistant shutdown releases the GATT link, once, for good.

Home Assistant runs shutdown jobs before it stops Bluetooth and the ESPHome
proxies. The job must drop a held, lingering or prewarmed link, latch the
device so nothing reconnects in this process, stay inside its time budget and
never raise.

These tests need Home Assistant, pySwitchbot and pytest-homeassistant-custom-
component, unlike ``test_policy.py``; they are skipped where those are absent
(the CI "policy tests" job installs only pytest). Run them with::

    PYTHONPATH=. python -m pytest tests/test_shutdown_release.py -o asyncio_mode=auto
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")
pytest.importorskip("switchbot")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bleak.backends.device import BLEDevice  # noqa: E402
from switchbot import SwitchbotOperationError  # noqa: E402

from homeassistant.const import CONF_ADDRESS, CONF_NAME  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.switchbot_express.const import CONF_DEVICE_TYPE, DOMAIN  # noqa: E402
from custom_components.switchbot_express.device import create_device  # noqa: E402
from custom_components.switchbot_express.policy import (  # noqa: E402
    ConnectionPolicy,
    PolicyOptions,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"


class FakeGatt:
    """The subset of a bleak client that pySwitchbot touches."""

    def __init__(self) -> None:
        self.connected = True
        self.services = MagicMock()
        self.disconnect_calls = 0
        self.hang_on_disconnect = False

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def start_notify(self, *args, **kwargs) -> None:
        return None

    async def clear_cache(self) -> None:
        return None

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
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
    """Count and script ``establish_connection`` as pySwitchbot calls it."""
    calls = {"n": 0}

    async def _establish(*args, **kwargs):
        calls["n"] += 1
        gatt.connected = True
        return gatt

    with patch("switchbot.devices.device.establish_connection", side_effect=_establish):
        yield calls


def _device(**options):
    ble_device = BLEDevice(ADDRESS, "WoCurtain", {})
    policy = ConnectionPolicy(PolicyOptions.from_mapping(options), core_disconnect_delay=8.5)
    return create_device("curtain", ble_device, policy)


# -- device: the latch -------------------------------------------------------


async def test_release_for_shutdown_drops_a_prewarmed_link_and_latches(gatt, connects) -> None:
    device = _device(prewarm_seconds=60)
    await device.async_prewarm()
    assert device.is_connected and connects["n"] == 1

    await device.async_release_for_shutdown()

    assert gatt.disconnect_calls == 1
    assert not device.is_connected
    assert device.closing

    # Every path that can open a link refuses, and none reaches the radio.
    with pytest.raises(SwitchbotOperationError):
        await device.async_ensure_connected()
    with pytest.raises(SwitchbotOperationError):
        await device.async_prewarm()
    with pytest.raises(SwitchbotOperationError):
        await device.open()
    assert connects["n"] == 1


async def test_release_for_shutdown_drops_a_lingering_link(gatt, connects) -> None:
    device = _device(linger_seconds=15)
    await device.async_ensure_connected()
    device.policy.note_user_command()
    device._reset_disconnect_timer()  # noqa: SLF001 - linger timer armed after a command
    assert device._disconnect_timer is not None  # noqa: SLF001

    await device.async_release_for_shutdown()

    assert not device.is_connected
    assert device._disconnect_timer is None  # noqa: SLF001
    assert gatt.disconnect_calls == 1


async def test_supervisor_does_not_reconnect_after_latch(gatt, connects) -> None:
    device = _device(hold_connection=True)
    await device.async_release_for_shutdown()

    # A holding supervisor started (or woken) after the latch exits instead of reconnecting.
    await asyncio.wait_for(device.async_supervise(), timeout=1)
    assert connects["n"] == 0


async def test_connect_in_flight_when_shutdown_begins_is_handed_back(gatt) -> None:
    device = _device()
    started = asyncio.Event()
    proceed = asyncio.Event()

    async def _slow_establish(*args, **kwargs):
        started.set()
        await proceed.wait()
        gatt.connected = True
        return gatt

    with patch("switchbot.devices.device.establish_connection", side_effect=_slow_establish):
        connecting = asyncio.create_task(device.async_ensure_connected())
        await started.wait()
        releasing = asyncio.create_task(device.async_release_for_shutdown())
        await asyncio.sleep(0)
        proceed.set()
        await releasing
        with pytest.raises(SwitchbotOperationError):
            await connecting

    assert not device.is_connected
    assert gatt.connected is False


# -- entry: the Home Assistant shutdown job ----------------------------------


@pytest.fixture
async def entry(hass, mock_bluetooth, gatt, connects):
    """A loaded curtain entry whose BLE layer is faked."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="Living Room Curtain",
        data={CONF_ADDRESS: ADDRESS, CONF_NAME: "Living Room Curtain", CONF_DEVICE_TYPE: "curtain"},
        options={"hold_connection": False, "linger_seconds": 15, "prewarm_seconds": 60},
    )
    entry.add_to_hass(hass)
    ble_device = BLEDevice(ADDRESS, "WoCurtain", {})
    with (
        patch(
            "custom_components.switchbot_express.bluetooth.async_ble_device_from_address",
            return_value=ble_device,
        ),
        patch("switchbot.close_stale_connections_by_address", new=AsyncMock()),
        patch(
            "custom_components.switchbot_express.coordinator.SwitchbotExpressCoordinator.async_wait_ready",
            new=AsyncMock(return_value=True),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry


def _release_jobs(hass) -> list:
    return [
        job_with_args
        for job_with_args in hass._shutdown_jobs  # noqa: SLF001 - no public accessor
        if "switchbot_express release BLE link" in str(job_with_args.job.name)
    ]


async def _run_release_job(hass) -> None:
    (job_with_args,) = _release_jobs(hass)
    await hass.async_run_hass_job(job_with_args.job, *job_with_args.args)


async def test_one_shutdown_job_per_entry_and_removed_on_unload(hass, entry) -> None:
    assert len(_release_jobs(hass)) == 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert _release_jobs(hass) == []


async def test_shutdown_job_releases_prewarmed_link_and_latches(
    hass, entry, gatt, connects, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = entry.runtime_data
    await coordinator.async_prewarm()
    assert coordinator.device.is_connected

    with caplog.at_level(logging.INFO, logger="custom_components.switchbot_express.coordinator"):
        await _run_release_job(hass)

    assert gatt.connected is False
    assert "Released BLE link to" in caplog.text
    assert entry.state.value == "loaded"  # released only; the entry is not unloaded
    with pytest.raises(SwitchbotOperationError):
        await coordinator.async_prewarm()
    assert connects["n"] == 1


async def test_hanging_disconnect_is_bounded_and_does_not_raise(
    hass, entry, gatt, connects, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = entry.runtime_data
    await coordinator.async_prewarm()
    gatt.hang_on_disconnect = True

    with (
        patch("custom_components.switchbot_express.coordinator.SHUTDOWN_RELEASE_TIMEOUT", 0.2),
        caplog.at_level(logging.WARNING, logger="custom_components.switchbot_express.coordinator"),
    ):
        started = time.monotonic()
        await _run_release_job(hass)  # must return, not raise
        elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert "Timed out" in caplog.text
    assert coordinator.device.closing  # latched even though the disconnect hung


async def test_failing_disconnect_does_not_raise(
    hass, entry, gatt, connects, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = entry.runtime_data
    with (
        patch.object(
            type(coordinator.device),
            "async_release_for_shutdown",
            AsyncMock(side_effect=RuntimeError("proxy went away")),
        ),
        caplog.at_level(logging.WARNING, logger="custom_components.switchbot_express.coordinator"),
    ):
        await _run_release_job(hass)

    assert "Could not release the BLE link" in caplog.text

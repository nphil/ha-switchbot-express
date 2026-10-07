"""Config flow for SwitchBot Express."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from switchbot import SwitchBotAdvertisement, parse_advertisement_data

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers import selector

from .const import (
    CONF_CONNECT_TIMEOUT,
    CONF_DEVICE_TYPE,
    CONF_HOLD_CONNECTION,
    CONF_LINGER_SECONDS,
    CONF_LOW_BATTERY_PERCENT,
    CONF_PREWARM_SECONDS,
    CONF_RETRY_COUNT,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_HOLD_CONNECTION,
    DEFAULT_LINGER_SECONDS,
    DEFAULT_LOW_BATTERY_PERCENT,
    DEFAULT_PREWARM_SECONDS,
    DEFAULT_RETRY_COUNT,
    DOMAIN,
    MAX_CONNECT_TIMEOUT,
    MAX_LINGER_SECONDS,
    MAX_LOW_BATTERY_PERCENT,
    MAX_PREWARM_SECONDS,
    MAX_RETRY_COUNT,
    MIN_CONNECT_TIMEOUT,
    MIN_LINGER_SECONDS,
    MIN_LOW_BATTERY_PERCENT,
    MIN_PREWARM_SECONDS,
    MIN_RETRY_COUNT,
)
from .device import MODEL_TO_TYPE

_LOGGER = logging.getLogger(__name__)


def format_unique_id(address: str) -> str:
    """Format the unique id for a SwitchBot address."""
    return address.replace(":", "").lower()


def short_address(address: str) -> str:
    """Return the last four hex digits of an address."""
    results = address.replace("-", ":").split(":")
    return f"{results[-2].upper()}{results[-1].upper()}"[-4:]


def name_from_discovery(discovery: SwitchBotAdvertisement) -> str:
    """Name a device from its advertisement, exactly as core does."""
    return f"{discovery.data['modelFriendlyName']} {short_address(discovery.address)}"


def default_options() -> dict[str, Any]:
    """Return the options a new entry starts with."""
    return {
        CONF_LINGER_SECONDS: DEFAULT_LINGER_SECONDS,
        CONF_HOLD_CONNECTION: DEFAULT_HOLD_CONNECTION,
        CONF_PREWARM_SECONDS: DEFAULT_PREWARM_SECONDS,
        CONF_LOW_BATTERY_PERCENT: DEFAULT_LOW_BATTERY_PERCENT,
        CONF_RETRY_COUNT: DEFAULT_RETRY_COUNT,
        CONF_CONNECT_TIMEOUT: DEFAULT_CONNECT_TIMEOUT,
    }


def _supported_type(discovery: SwitchBotAdvertisement) -> str | None:
    """Return the device type key for an advertisement, if supported."""
    return MODEL_TO_TYPE.get(discovery.data.get("modelName"))


class SwitchbotExpressConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for SwitchBot Express."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the flow."""
        self._discovered_adv: SwitchBotAdvertisement | None = None
        self._discovered_advs: dict[str, SwitchBotAdvertisement] = {}

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> SwitchbotExpressOptionsFlow:
        """Return the options flow."""
        return SwitchbotExpressOptionsFlow()

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a device discovered over Bluetooth."""
        _LOGGER.debug("Discovered SwitchBot: %s", discovery_info.address)
        await self.async_set_unique_id(format_unique_id(discovery_info.address))
        self._abort_if_unique_id_configured()
        parsed = parse_advertisement_data(
            discovery_info.device, discovery_info.advertisement
        )
        if not parsed or not _supported_type(parsed):
            return self.async_abort(reason="not_supported")
        if not discovery_info.connectable:
            # Every model here is controlled over a connection; an
            # advertisement from a listen-only scanner is not enough.
            return self.async_abort(reason="not_connectable")
        self._discovered_adv = parsed
        self.context["title_placeholders"] = {
            "name": parsed.data["modelFriendlyName"],
            "address": short_address(discovery_info.address),
        }
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a single device and name it."""
        assert self._discovered_adv is not None
        suggested = name_from_discovery(self._discovered_adv)
        if user_input is not None:
            return self._async_create_entry(user_input.get(CONF_NAME) or suggested)
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema(
                {vol.Optional(CONF_NAME, default=suggested): str},
            ),
            description_placeholders={"name": suggested},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick one of the SwitchBots in range."""
        if user_input is not None:
            self._discovered_adv = self._discovered_advs[user_input[CONF_ADDRESS]]
            await self._async_set_device(self._discovered_adv)
            return await self.async_step_confirm()

        await bluetooth.async_request_active_scan(self.hass)
        self._async_discover_devices()
        if len(self._discovered_advs) == 1:
            self._discovered_adv = next(iter(self._discovered_advs.values()))
            await self._async_set_device(self._discovered_adv)
            return await self.async_step_confirm()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {
                            address: name_from_discovery(parsed)
                            for address, parsed in self._discovered_advs.items()
                        }
                    )
                }
            ),
        )

    @callback
    def _async_discover_devices(self) -> None:
        """Collect supported, connectable SwitchBots that are not set up."""
        current_addresses = self._async_current_ids(include_ignore=False)
        for discovery_info in async_discovered_service_info(self.hass, True):
            address = discovery_info.address
            if (
                format_unique_id(address) in current_addresses
                or address in self._discovered_advs
            ):
                continue
            parsed = parse_advertisement_data(
                discovery_info.device, discovery_info.advertisement
            )
            if not parsed or not _supported_type(parsed):
                continue
            self._discovered_advs[address] = parsed

        if not self._discovered_advs:
            raise AbortFlow("no_devices_found")

    async def _async_set_device(self, discovery: SwitchBotAdvertisement) -> None:
        """Lock the flow to one device."""
        await self.async_set_unique_id(
            format_unique_id(discovery.address), raise_on_progress=False
        )
        self._abort_if_unique_id_configured()

    @callback
    def _async_create_entry(self, name: str) -> ConfigFlowResult:
        """Create the entry for the discovered device."""
        assert self._discovered_adv is not None
        device_type = _supported_type(self._discovered_adv)
        assert device_type is not None
        return self.async_create_entry(
            title=name,
            data={
                CONF_ADDRESS: self._discovered_adv.address,
                CONF_NAME: name,
                CONF_DEVICE_TYPE: device_type,
            },
            options=default_options(),
        )


class SwitchbotExpressOptionsFlow(OptionsFlow):
    """Handle the connection policy options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_LINGER_SECONDS,
                        default=options.get(
                            CONF_LINGER_SECONDS, DEFAULT_LINGER_SECONDS
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_LINGER_SECONDS,
                            max=MAX_LINGER_SECONDS,
                            step=1,
                            unit_of_measurement="s",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(
                        CONF_HOLD_CONNECTION,
                        default=options.get(
                            CONF_HOLD_CONNECTION, DEFAULT_HOLD_CONNECTION
                        ),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_PREWARM_SECONDS,
                        default=options.get(
                            CONF_PREWARM_SECONDS, DEFAULT_PREWARM_SECONDS
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_PREWARM_SECONDS,
                            max=MAX_PREWARM_SECONDS,
                            step=1,
                            unit_of_measurement="s",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(
                        CONF_LOW_BATTERY_PERCENT,
                        default=options.get(
                            CONF_LOW_BATTERY_PERCENT, DEFAULT_LOW_BATTERY_PERCENT
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_LOW_BATTERY_PERCENT,
                            max=MAX_LOW_BATTERY_PERCENT,
                            step=1,
                            unit_of_measurement="%",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(
                        CONF_RETRY_COUNT,
                        default=options.get(CONF_RETRY_COUNT, DEFAULT_RETRY_COUNT),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_RETRY_COUNT,
                            max=MAX_RETRY_COUNT,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(
                        CONF_CONNECT_TIMEOUT,
                        default=options.get(
                            CONF_CONNECT_TIMEOUT, DEFAULT_CONNECT_TIMEOUT
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_CONNECT_TIMEOUT,
                            max=MAX_CONNECT_TIMEOUT,
                            step=1,
                            unit_of_measurement="s",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
        )

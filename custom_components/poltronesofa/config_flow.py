"""Add a seat when Home Assistant's Bluetooth sees it, or pick one by hand."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import BluetoothServiceInfoBleak, async_discovered_service_info
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from . import DOMAIN
from .sofa import MANUFACTURER_ID


def _title(address: str) -> str:
    return f"Sofa seat {address[-5:].replace(':', '')}"


class SeatConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._address: str | None = None

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._address = discovery_info.address
        self.context["title_placeholders"] = {"name": _title(discovery_info.address)}
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._address
        if user_input is not None:
            return self.async_create_entry(title=_title(self._address), data={})
        self._set_confirm_only()
        return self.async_show_form(step_id="confirm", description_placeholders={"name": _title(self._address)})

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=_title(address), data={})
        configured = self._async_current_ids()
        seats = {
            info.address: f"{_title(info.address)} ({info.address})"
            for info in async_discovered_service_info(self.hass)
            if MANUFACTURER_ID in info.manufacturer_data and info.address not in configured
        }
        if not seats:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(step_id="user", data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(seats)}))

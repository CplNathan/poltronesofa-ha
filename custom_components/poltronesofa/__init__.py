"""poltronesofà Bluetooth recliner seats."""

from __future__ import annotations

from bleak.exc import BleakError

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo

from .sofa import DEFAULT_TRAVEL_SECONDS, Seat

DOMAIN = "poltronesofa"
CONF_TRAVEL_SECONDS = "travel_seconds"
PLATFORMS = [Platform.COVER, Platform.BUTTON, Platform.SWITCH]

type SeatConfigEntry = ConfigEntry[Seat]


async def async_setup_entry(hass: HomeAssistant, entry: SeatConfigEntry) -> bool:
    address = entry.unique_id
    device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
    if device is None:
        raise ConfigEntryNotReady(f"Can't see sofa seat {address}")
    seat = Seat(device, entry.options.get(CONF_TRAVEL_SECONDS, DEFAULT_TRAVEL_SECONDS))
    entry.runtime_data = seat
    entry.async_on_unload(entry.add_update_listener(_reload))
    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            lambda info, _change: seat.set_device(info.device),
            bluetooth.BluetoothCallbackMatcher(address=address, connectable=True),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SeatConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.disconnect()
    return unloaded


async def _reload(hass: HomeAssistant, entry: SeatConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def device_info(entry: SeatConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        connections={(CONNECTION_BLUETOOTH, entry.unique_id)},
        name=entry.title,
        manufacturer="poltronesofà",
        model="Power recliner seat",
    )


async def reach(action) -> None:
    """Run a seat action, turning Bluetooth failures into a message the user can read."""
    try:
        await action
    except (BleakError, TimeoutError) as err:
        raise HomeAssistantError(f"Couldn't reach the sofa seat: {err}") from err

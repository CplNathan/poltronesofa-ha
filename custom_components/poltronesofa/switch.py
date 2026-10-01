"""Child lock, read from and set on the seat itself."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from bleak.exc import BleakError

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SeatConfigEntry, device_info, reach

# Each check briefly takes the seat's only connection, so keep it rare.
SCAN_INTERVAL = timedelta(minutes=15)


async def async_setup_entry(
    hass: HomeAssistant, entry: SeatConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([ChildLock(entry)], update_before_add=False)


class ChildLock(SwitchEntity):
    _attr_has_entity_name = True
    _attr_name = "Child lock"
    _attr_icon = "mdi:lock"

    def __init__(self, entry: SeatConfigEntry) -> None:
        self._seat = entry.runtime_data
        self._attr_unique_id = f"{entry.unique_id}_child_lock"
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._seat.subscribe(self.async_write_ha_state))

    @property
    def is_on(self) -> bool | None:
        return self._seat.locked

    async def async_turn_on(self, **kwargs: Any) -> None:
        await reach(self._seat.set_locked(True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await reach(self._seat.set_locked(False))

    async def async_update(self) -> None:
        try:
            await self._seat.refresh()
        except (BleakError, TimeoutError):
            # The phone app may hold the connection; keep the last known state.
            pass

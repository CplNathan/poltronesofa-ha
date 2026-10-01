"""Memory buttons: go to a saved position, or save the current one."""

from __future__ import annotations

import asyncio

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SeatConfigEntry, device_info, reach
from .sofa import GO_TO_MEMORY, SAVE_MEMORY, TRAVEL_SECONDS


async def async_setup_entry(
    hass: HomeAssistant, entry: SeatConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities(
        [GoToMemory(entry, slot) for slot in GO_TO_MEMORY] + [SaveMemory(entry, slot) for slot in SAVE_MEMORY]
    )


class MemoryButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, entry: SeatConfigEntry, slot: int, key: str) -> None:
        self._seat = entry.runtime_data
        self._slot = slot
        self._attr_unique_id = f"{entry.unique_id}_{key}_{slot}"
        self._attr_device_info = device_info(entry)


class GoToMemory(MemoryButton):
    def __init__(self, entry: SeatConfigEntry, slot: int) -> None:
        super().__init__(entry, slot, "memory")
        self._attr_name = f"Memory {slot}"
        self._attr_icon = f"mdi:numeric-{slot}-box"

    async def async_press(self) -> None:
        # The seat only moves while the command is "held": run for a full travel, then stop.
        # ponytail: the cover's position estimate isn't updated by memory moves; a full open or close resets it.
        await reach(self._seat.send(GO_TO_MEMORY[self._slot]))
        await asyncio.sleep(TRAVEL_SECONDS)
        await reach(self._seat.stop())


class SaveMemory(MemoryButton):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: SeatConfigEntry, slot: int) -> None:
        super().__init__(entry, slot, "save_memory")
        self._attr_name = f"Save memory {slot}"
        self._attr_icon = "mdi:content-save"

    async def async_press(self) -> None:
        await reach(self._seat.send(SAVE_MEMORY[self._slot]))

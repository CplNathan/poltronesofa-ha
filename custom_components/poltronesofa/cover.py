"""The seat as a cover: open reclines, close returns. Position is estimated from travel time."""

from __future__ import annotations

from time import monotonic
from typing import Any

from homeassistant.components.cover import ATTR_CURRENT_POSITION, ATTR_POSITION, CoverEntity, CoverEntityFeature
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.restore_state import RestoreEntity

from . import SeatConfigEntry, device_info, reach
from .sofa import CLOSE, OPEN, TRAVEL_SECONDS


async def async_setup_entry(
    hass: HomeAssistant, entry: SeatConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([SeatCover(entry)])


class SeatCover(CoverEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_assumed_state = True
    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP | CoverEntityFeature.SET_POSITION
    )

    def __init__(self, entry: SeatConfigEntry) -> None:
        self._seat = entry.runtime_data
        self._attr_unique_id = entry.unique_id
        self._attr_device_info = device_info(entry)
        self._position = 0.0
        self._direction = 0
        self._started = 0.0
        self._start_position = 0.0
        self._arrive: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last and last.attributes.get(ATTR_CURRENT_POSITION) is not None:
            self._position = float(last.attributes[ATTR_CURRENT_POSITION])

    @property
    def current_cover_position(self) -> int:
        return round(self._now())

    @property
    def is_closed(self) -> bool:
        return self._direction == 0 and self._position == 0

    @property
    def is_opening(self) -> bool:
        return self._direction > 0

    @property
    def is_closing(self) -> bool:
        return self._direction < 0

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._move_to(100)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._move_to(0)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        await self._move_to(kwargs[ATTR_POSITION])

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self._halt()

    def _now(self) -> float:
        if not self._direction:
            return self._position
        travelled = (monotonic() - self._started) / TRAVEL_SECONDS * 100
        return min(100.0, max(0.0, self._start_position + self._direction * travelled))

    async def _move_to(self, target: float) -> None:
        if self._direction:
            await self._halt()
        # A full open or close runs the whole travel time so the estimate resets at the end stop.
        at_end = target in (0, 100)
        if not at_end and abs(target - self._position) < 1:
            return
        direction = 1 if target > self._position or target == 100 else -1
        seconds = TRAVEL_SECONDS if at_end else abs(target - self._position) / 100 * TRAVEL_SECONDS
        await reach(self._seat.send(OPEN if direction > 0 else CLOSE))
        self._direction = direction
        self._start_position = self._position
        self._started = monotonic()
        self._arrive = async_call_later(self.hass, seconds, self._arrived)
        self.async_write_ha_state()

    async def _arrived(self, _now: Any) -> None:
        self._arrive = None
        await self._halt()

    async def _halt(self) -> None:
        if self._arrive:
            self._arrive()
            self._arrive = None
        self._position = self._now()
        self._direction = 0
        self.async_write_ha_state()
        await reach(self._seat.stop())

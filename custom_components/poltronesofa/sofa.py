"""Bluetooth link to one poltronesofà recliner seat.

Protocol from the poltronesofà app (com.ykd.poltrone, CommandUtils/SynToByte):
an 8-byte frame [counter] [group] [code, 2 bytes LE] [param, 4 bytes LE] written to WRITE,
replies on NOTIFY. Kept free of Home Assistant imports so it can be tested on its own.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
import logging

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

_LOGGER = logging.getLogger(__name__)

WRITE = "6e403588-b5a3-f393-e0a9-e50e24dcca9e"
NOTIFY = "6e403589-b5a3-f393-e0a9-e50e24dcca9e"
MANUFACTURER_ID = 0x045B

CTRL = 0x01
GET_STATE = 0x02
SET_PARA = 0x03

STOP = 0x0001
OPEN = 0x0110
CLOSE = 0x0111
SAVE_MEMORY = {1: 0x0310, 2: 0x0312}
GO_TO_MEMORY = {1: 0x0311, 2: 0x0313}
LOCK_TOGGLE = 0x60A3
CHILD_LOCK = 0x0337
PIN_IN = 0xF000

DEFAULT_TRAVEL_SECONDS = 12.0
# ponytail: fixed PIN; make it an option once someone sets one in the app.
PIN = 0


def frame(counter: int, group: int, code: int, param: int = 0) -> bytes:
    """Build one command frame."""
    return bytes([counter & 0xFF, group]) + code.to_bytes(2, "little") + param.to_bytes(4, "little")


def lock_state(reply: bytes) -> bool | None:
    """Read the child lock from a CHILD_LOCK state reply (82 37 03 LL), else None."""
    if len(reply) >= 5 and reply[1:4] == bytes([0x82, 0x37, 0x03]):
        return reply[4] == 0x01
    return None


class Seat:
    """One seat's motor controller, connected on demand."""

    def __init__(self, device: BLEDevice, travel_seconds: float) -> None:
        self._device = device
        self.travel_seconds = travel_seconds
        self._client: BleakClient | None = None
        self._counter = 0
        self._busy = asyncio.Lock()
        self._idle: asyncio.TimerHandle | None = None
        self._state_seen = asyncio.Event()
        self._listeners: list[Callable[[], None]] = []
        self.locked: bool | None = None

    def set_device(self, device: BLEDevice) -> None:
        self._device = device

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    async def send(self, code: int) -> None:
        async with self._connected():
            await self._write(CTRL, code)

    async def stop(self) -> None:
        """Stop the motors the way the app does: three stops, 20 ms apart."""
        async with self._connected():
            for _ in range(3):
                await self._write(CTRL, STOP)
                await asyncio.sleep(0.02)

    async def refresh(self) -> None:
        """Connect, which reads the lock state."""
        async with self._connected():
            pass

    async def set_locked(self, locked: bool) -> None:
        """The sofa only offers a toggle, so toggle only when the state differs."""
        async with self._connected():
            if self.locked is None:
                raise TimeoutError("The seat didn't say whether it's locked")
            if self.locked != locked:
                self._state_seen.clear()
                await self._write(CTRL, LOCK_TOGGLE)
                await asyncio.wait_for(self._state_seen.wait(), 2)

    async def disconnect(self) -> None:
        async with self._busy:
            await self._drop()

    @asynccontextmanager
    async def _connected(self) -> AsyncIterator[None]:
        """Run one action on a live link.

        Any failure drops the link, so the seat's only connection is never left half-open.
        """
        async with self._busy:
            try:
                await self._connect()
                yield
            except BaseException:
                await self._drop()
                raise
            self._keep_alive()

    async def _drop(self) -> None:
        if self._idle:
            self._idle.cancel()
            self._idle = None
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await client.disconnect()
        except BleakError as err:
            _LOGGER.debug("Disconnect from %s failed: %s", self._device.address, err)

    async def _connect(self) -> None:
        if self._client and self._client.is_connected:
            return
        self._client = await establish_connection(
            BleakClientWithServiceCache,
            self._device,
            self._device.name or self._device.address,
            disconnected_callback=self._on_disconnect,
        )
        self._state_seen.clear()
        await self._client.start_notify(NOTIFY, self._on_notify)
        await self._write(SET_PARA, PIN_IN, PIN)
        await self._write(GET_STATE, CHILD_LOCK)
        await asyncio.wait_for(self._state_seen.wait(), 2)

    async def _write(self, group: int, code: int, param: int = 0) -> None:
        assert self._client
        self._counter += 1
        await self._client.write_gatt_char(WRITE, frame(self._counter, group, code, param), response=False)

    def _keep_alive(self) -> None:
        if self._idle:
            self._idle.cancel()
        loop = asyncio.get_running_loop()
        # Stay connected past a full move so a stop never waits on a reconnect, then free the seat for the app.
        self._idle = loop.call_later(self.travel_seconds + 10, lambda: loop.create_task(self.disconnect()))

    def _on_disconnect(self, _client: BleakClient) -> None:
        self._client = None

    def _on_notify(self, _sender: object, data: bytearray) -> None:
        locked = lock_state(bytes(data))
        if locked is None:
            return
        self.locked = locked
        self._state_seen.set()
        for listener in list(self._listeners):
            listener()


if __name__ == "__main__":
    assert frame(0x42, CTRL, OPEN) == bytes.fromhex("4201100100000000")
    assert frame(0x100, SET_PARA, PIN_IN, 1234) == bytes.fromhex("000300f0d2040000")
    assert frame(7, CTRL, GO_TO_MEMORY[2]) == bytes.fromhex("0701130300000000")
    assert lock_state(bytes.fromhex("1182370301000000")) is True
    assert lock_state(bytes.fromhex("1282370300000000")) is False
    assert lock_state(bytes.fromhex("1181" "03f084610020")) is None
    print("frames ok")

    class _FailingClient:
        is_connected = True
        disconnected = False

        async def start_notify(self, *_args):
            raise BleakError("notify refused")

        async def disconnect(self):
            self.disconnected = True

    async def _half_open_is_dropped() -> None:
        client = _FailingClient()

        async def _fake_connect(*_args, **_kwargs):
            return client

        globals()["establish_connection"] = _fake_connect
        seat = Seat(BLEDevice("00:00:00:00:00:00", "test", None), 12.0)
        try:
            await seat.send(OPEN)
        except BleakError:
            pass
        assert client.disconnected and seat._client is None and seat._idle is None

    asyncio.run(_half_open_is_dropped())
    print("half-open link dropped ok")

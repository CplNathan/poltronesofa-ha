"""Bluetooth link to one poltronesofà recliner seat.

Protocol from the poltronesofà app (com.ykd.poltrone, CommandUtils/SynToByte):
an 8-byte frame [counter] [group] [code, 2 bytes LE] [param, 4 bytes LE] written to WRITE,
replies on NOTIFY. Kept free of Home Assistant imports so it can be tested on its own.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
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
RECONNECT_SECONDS = 5
# bleak-retry-connector allows 20 s per attempt and 4 attempts. A seat that's reachable connects in a few
# seconds, so give up sooner: a press fails fast and the adapter isn't tied up for over a minute.
CONNECT_SECONDS = 10
CONNECT_ATTEMPTS = 3
MAX_RECONNECT_SECONDS = 60
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
        self._holding = False
        self._link_lost = asyncio.Event()
        self._hold_task: asyncio.Task[None] | None = None

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
        """Read the lock state, connecting first if need be."""
        async with self._connected():
            await self._read_lock()

    async def set_locked(self, locked: bool) -> None:
        """The sofa only offers a toggle, so toggle only when the state differs."""
        async with self._connected():
            # Ask first: a held link never reconnects, so the last known state can be old.
            await self._read_lock()
            if self.locked != locked:
                self._state_seen.clear()
                await self._write(CTRL, LOCK_TOGGLE)
                await asyncio.wait_for(self._state_seen.wait(), 2)

    async def hold(self) -> None:
        """Keep the link open for good, reconnecting whenever it drops, until disconnect(). Blocks the phone app."""
        self._holding = True
        self._hold_task = asyncio.current_task()
        retry = RECONNECT_SECONDS
        while self._holding:
            if self._client is None or not self._client.is_connected:
                try:
                    await self.refresh()
                    retry = RECONNECT_SECONDS
                except (BleakError, TimeoutError) as err:
                    _LOGGER.debug("Reconnect to %s failed: %s", self._device.address, err)
                    retry = min(retry * 2, MAX_RECONNECT_SECONDS)
                except Exception:
                    # Anything unexpected must not end the loop, or the seat is never held again.
                    _LOGGER.exception("Unexpected error reconnecting to %s", self._device.address)
                    retry = MAX_RECONNECT_SECONDS
            # Cleared after the attempt, so a drop caused by a failed attempt doesn't skip the back-off.
            self._link_lost.clear()
            try:
                await asyncio.wait_for(self._link_lost.wait(), retry)
            except TimeoutError:
                pass

    async def disconnect(self) -> None:
        self._holding = False
        # A reconnect can sit in a Bluetooth connect, holding the lock, for a minute or more;
        # cancel it rather than wait, or unloading and shutdown hang.
        holder, self._hold_task = self._hold_task, None
        if holder and holder is not asyncio.current_task() and not holder.done():
            holder.cancel()
            with suppress(asyncio.CancelledError):
                await holder
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
        for attempt in range(2):
            async with asyncio.timeout(CONNECT_SECONDS):
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    self._device,
                    self._device.name or self._device.address,
                    disconnected_callback=self._on_disconnect,
                    max_attempts=CONNECT_ATTEMPTS,
                )
            self._client = client
            try:
                await self._start_session()
                return
            except BleakError:
                if attempt:
                    raise
                # BlueZ can reuse a stale service list whose characteristics no longer exist
                # ("StartNotify ... doesn't exist"); forget it and connect once more.
                _LOGGER.debug("Clearing stale services for %s", self._device.address)
                await client.clear_cache()
                await self._drop()

    async def _start_session(self) -> None:
        """Turn on replies, send the PIN and read the lock state, as the app does on connect."""
        assert self._client
        await self._client.start_notify(NOTIFY, self._on_notify)
        await self._write(SET_PARA, PIN_IN, PIN)
        await self._read_lock()

    async def _read_lock(self) -> None:
        """Ask for the lock state and wait for the reply; no reply means the link isn't really working."""
        self._state_seen.clear()
        await self._write(GET_STATE, CHILD_LOCK)
        await asyncio.wait_for(self._state_seen.wait(), 2)

    async def _write(self, group: int, code: int, param: int = 0) -> None:
        assert self._client
        self._counter += 1
        await self._client.write_gatt_char(WRITE, frame(self._counter, group, code, param), response=False)

    def _keep_alive(self) -> None:
        if self._idle:
            self._idle.cancel()
            self._idle = None
        if self._holding:
            return
        loop = asyncio.get_running_loop()
        # Stay connected past a full move so a stop never waits on a reconnect, then free the seat for the app.
        self._idle = loop.call_later(self.travel_seconds + 10, lambda: loop.create_task(self.disconnect()))

    def _on_disconnect(self, client: BleakClient) -> None:
        # A link we already dropped can report in late, after a new one is up; only the current link counts.
        if client is not self._client:
            return
        self._client = None
        self._link_lost.set()

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

        async def clear_cache(self):
            return True

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

    async def _holding_never_idles() -> None:
        seat = Seat(BLEDevice("00:00:00:00:00:00", "test", None), 12.0)
        seat._holding = True
        seat._keep_alive()
        assert seat._idle is None

    asyncio.run(_holding_never_idles())

    class _StaleThenGoodClient(_FailingClient):
        attempts = 0
        cleared = False

        async def start_notify(self, *_args):
            _StaleThenGoodClient.attempts += 1
            if _StaleThenGoodClient.attempts == 1:
                raise BleakError("StartNotify doesn't exist")

        async def clear_cache(self):
            _StaleThenGoodClient.cleared = True
            return True

        async def write_gatt_char(self, *_args, **_kwargs):
            pass

    async def _stale_services_are_retried() -> None:
        async def _fake_connect(*_args, **_kwargs):
            return _StaleThenGoodClient()

        globals()["establish_connection"] = _fake_connect
        seat = Seat(BLEDevice("00:00:00:00:00:00", "test", None), 12.0)
        connect = asyncio.create_task(seat.send(OPEN))
        await asyncio.sleep(0.1)
        seat._on_notify(None, bytearray.fromhex("0182370300000000"))
        await connect
        assert _StaleThenGoodClient.cleared and _StaleThenGoodClient.attempts == 2 and seat.locked is False
        await seat.disconnect()

    asyncio.run(_stale_services_are_retried())

    async def _hold_survives_any_error() -> None:
        globals()["RECONNECT_SECONDS"] = globals()["MAX_RECONNECT_SECONDS"] = 0.01
        seat = Seat(BLEDevice("00:00:00:00:00:00", "test", None), 12.0)
        calls = 0

        async def _flaky_refresh() -> None:
            nonlocal calls
            calls += 1
            raise [BleakError("gone"), TimeoutError(), RuntimeError("odd")][calls % 3]

        seat.refresh = _flaky_refresh
        holder = asyncio.create_task(seat.hold())
        await asyncio.sleep(0.3)
        assert not holder.done() and calls >= 6, calls
        await seat.disconnect()
        assert holder.done()

    logging.disable(logging.CRITICAL)
    asyncio.run(_hold_survives_any_error())

    async def _disconnect_never_waits_on_a_reconnect() -> None:
        async def _endless_connect(*_args, **_kwargs):
            await asyncio.sleep(3600)

        globals()["establish_connection"] = _endless_connect
        seat = Seat(BLEDevice("00:00:00:00:00:00", "test", None), 12.0)
        holder = asyncio.create_task(seat.hold())
        await asyncio.sleep(0.05)
        assert seat._busy.locked()
        await asyncio.wait_for(seat.disconnect(), 1)
        assert holder.done() and not seat._busy.locked()

    asyncio.run(_disconnect_never_waits_on_a_reconnect())

    async def _late_disconnect_is_ignored() -> None:
        seat = Seat(BLEDevice("00:00:00:00:00:00", "test", None), 12.0)
        old, live = _FailingClient(), _FailingClient()
        seat._client = live
        seat._on_disconnect(old)
        assert seat._client is live and not seat._link_lost.is_set()
        seat._on_disconnect(live)
        assert seat._client is None and seat._link_lost.is_set()

    asyncio.run(_late_disconnect_is_ignored())

    async def _connect_gives_up_in_time() -> None:
        async def _endless_connect(*_args, **_kwargs):
            await asyncio.sleep(3600)

        globals()["establish_connection"] = _endless_connect
        globals()["CONNECT_SECONDS"] = 0.05
        seat = Seat(BLEDevice("00:00:00:00:00:00", "test", None), 12.0)
        started = asyncio.get_running_loop().time()
        try:
            await seat.send(OPEN)
            raise AssertionError("connect should time out")
        except TimeoutError:
            pass
        assert asyncio.get_running_loop().time() - started < 0.5
        assert not seat._busy.locked() and seat._client is None

    asyncio.run(_connect_gives_up_in_time())
    print("connection checks ok")

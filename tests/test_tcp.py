"""End-to-end Shelly -> translator -> SDM630 client using actual TCP frames."""

import asyncio
import struct
import time
from dataclasses import replace

import pytest
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.constants import ExcCodes
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

from custom_components.h3x_modbus_translator.model import MeterState, Settings, float_words
from custom_components.h3x_modbus_translator.runtime import Runtime
from custom_components.h3x_modbus_translator.server import MeterServer
from custom_components.h3x_modbus_translator.source import ShellySource

from .test_model import phase_words, snapshot


def source_registers():
    registers = [0] * 1300
    stamp = int(time.time())
    registers[1000:1002] = [stamp & 0xFFFF, stamp >> 16]
    registers[1160:1162] = [stamp & 0xFFFF, stamp >> 16]
    for offset, value in ((1011, 15), (1013, -3000), (1015, 3300), (1162, 123456), (1164, 7890)):
        hi, lo = float_words(value)
        registers[offset : offset + 2] = [lo, hi]
    for address in (1020, 1040, 1060):
        registers[address : address + 15] = phase_words(-1000)
    return registers


@pytest.fixture
async def shelly():
    data = source_registers()

    async def access(function, start, address, count, registers, values):
        if function != 4 or values is not None:
            return ExcCodes.ILLEGAL_FUNCTION
        registers[address : address + count] = data[address : address + count]
        return None

    server = ModbusTcpServer(
        SimDevice(1, SimData(0, count=1300, datatype=DataType.REGISTERS), action=access),
        address=("127.0.0.1", 0),
    )
    await server.serve_forever(background=True)
    port = server.transport.sockets[0].getsockname()[1]
    try:
        yield port, data
    finally:
        await server.shutdown()


@pytest.fixture
async def meter(free_port):
    state = MeterState(Settings("localhost", listen_host="127.0.0.1", listen_port=free_port))
    state.publish(snapshot(now=time.monotonic()))
    server = MeterServer(state)
    await server.serve_forever(background=True)
    client = AsyncModbusTcpClient("127.0.0.1", port=free_port, timeout=0.3, retries=0)
    assert await client.connect()
    try:
        yield state, client, free_port
    finally:
        client.close()
        await server.shutdown()


async def test_source_to_sdm630_tcp(shelly, free_port):
    port, _ = shelly
    state = MeterState(
        Settings("localhost", source_port=port, listen_host="127.0.0.1", listen_port=free_port)
    )
    source = ShellySource(state)
    server = MeterServer(state)
    client = AsyncModbusTcpClient("127.0.0.1", port=free_port, retries=0)
    try:
        state.publish(await source.poll())
        assert state.snapshot.power == -3000
        assert state.snapshot.import_kwh == 123.456
        await server.serve_forever(background=True)
        assert await client.connect()
        response = await client.read_input_registers(52, count=2, device_id=1)
        assert not response.isError()
        assert struct.unpack(">f", struct.pack(">HH", *response.registers))[0] == -3000
        energy = await client.read_input_registers(72, count=4, device_id=1)
        assert len(energy.registers) == 4
        assert state.requests == 2
    finally:
        client.close()
        source.close()
        await server.shutdown()


async def test_reverse_source(shelly):
    port, _ = shelly
    source = ShellySource(MeterState(Settings("localhost", source_port=port, reverse_power=True)))
    try:
        snap = await source.poll()
        assert snap.power == 3000
        assert snap.import_kwh == 7.89
        assert snap.export_kwh == 123.456
        assert all(p.factor > 0 for p in snap.phases)
    finally:
        source.close()


@pytest.mark.parametrize("address,value", [(1002, 1), (1030, 1), (1013, 0)])
async def test_source_alarm_and_mismatch_rejected(shelly, address, value):
    port, data = shelly
    data[address] = value  # 1013 low-word corruption changes total coherency.
    if address == 1013:
        data[1014] = 0
    source = ShellySource(MeterState(Settings("localhost", source_port=port)))
    try:
        with pytest.raises(ValueError):
            await source.poll()
    finally:
        source.close()


@pytest.mark.parametrize(
    "address,count,code",
    [
        (24, 2, 2),
        (0, 80, 2),
        (200, 2, 2),
        (1, 2, 3),
        (52, 1, 3),
        (0, 82, 3),
    ],
)
async def test_unsupported_reads(meter, address, count, code):
    _, client, _ = meter
    response = await client.read_input_registers(address, count=count, device_id=1)
    assert response.isError()
    assert response.exception_code == code


async def test_reject_write_and_holding_and_coils(meter):
    state, client, _ = meter
    before = dict(state.words)
    responses = [
        await client.write_register(52, 0, device_id=1),
        await client.write_registers(52, [0, 0], device_id=1),
        await client.read_holding_registers(52, count=2, device_id=1),
        await client.read_coils(0, count=1, device_id=1),
    ]
    assert all(r.isError() and r.exception_code == 1 for r in responses)
    assert dict(state.words) == before


async def test_stale_then_recovery(meter):
    state, client, _ = meter
    state.fail("offline")
    response = await client.read_input_registers(52, count=2, device_id=1)
    assert response.exception_code == 4
    state.publish(snapshot(now=time.monotonic(), stamp=124))
    assert not (await client.read_input_registers(52, count=2, device_id=1)).isError()
    state.snapshot = replace(state.snapshot, received_at=time.monotonic() - 20)
    assert (await client.read_input_registers(52, count=2, device_id=1)).exception_code == 4


async def test_fragmented_frame_and_transaction_ids(meter):
    _, _, port = meter
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        for tid in range(100, 140):
            frame = struct.pack(">HHHBBHH", tid, 0, 6, 1, 4, 52, 2)
            writer.write(frame[:5])
            await writer.drain()
            await asyncio.sleep(0.001)
            writer.write(frame[5:])
            await writer.drain()
            header = await asyncio.wait_for(reader.readexactly(7), 1)
            got_tid, protocol, length, unit = struct.unpack(">HHHB", header)
            data = await reader.readexactly(length - 1)
            assert (got_tid, protocol, unit) == (tid, 0, 1)
            assert data[:2] == b"\x04\x04"
    finally:
        writer.close()
        await writer.wait_closed()


async def test_concurrent_clients(meter):
    state, _, port = meter

    async def read_many():
        client = AsyncModbusTcpClient("127.0.0.1", port=port, retries=0)
        try:
            assert await client.connect()
            for _ in range(20):
                assert not (await client.read_input_registers(52, count=2, device_id=1)).isError()
        finally:
            client.close()

    await asyncio.gather(*(read_many() for _ in range(4)))
    assert state.requests == 80


async def test_acl_denies_before_register_access(free_port):
    state = MeterState(
        Settings(
            "localhost", listen_host="127.0.0.1", listen_port=free_port, allowed_clients="192.0.2.4"
        )
    )
    server = MeterServer(state)
    await server.serve_forever(background=True)
    reader, writer = await asyncio.open_connection("127.0.0.1", free_port)
    try:
        assert await asyncio.wait_for(reader.read(1), 1) == b""
        assert state.requests == 0
        assert state.rejected == 1
    finally:
        writer.close()
        await writer.wait_closed()
        await server.shutdown()


async def test_runtime_starts_offline_and_releases_port(shelly, free_port):
    port, _ = shelly
    runtime = Runtime(
        Settings(
            "localhost",
            source_port=port,
            listen_host="127.0.0.1",
            listen_port=free_port,
            poll_seconds=1,
        )
    )
    try:
        await runtime.start()
        for _ in range(30):
            if runtime.state.valid:
                break
            await asyncio.sleep(0.05)
        assert runtime.state.fresh(time.monotonic())
        assert runtime.state.listening
    finally:
        await runtime.close()
    assert runtime.task is None
    assert not runtime.source.client.connected
    assert not runtime.state.valid
    rebound = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", free_port)
    rebound.close()
    await rebound.wait_closed()


async def test_runtime_invalidates_on_alarm_and_recovers(shelly, free_port):
    port, data = shelly
    changed = asyncio.Event()
    runtime = Runtime(
        Settings(
            "localhost",
            source_port=port,
            listen_host="127.0.0.1",
            listen_port=free_port,
            poll_seconds=1,
        ),
        changed.set,
    )
    client = AsyncModbusTcpClient("127.0.0.1", port=free_port, retries=0)

    async def wait_until(predicate):
        async with asyncio.timeout(6):
            while not predicate():
                changed.clear()
                await changed.wait()

    try:
        await runtime.start()
        await wait_until(lambda: runtime.state.valid)
        assert await client.connect()
        data[1002] = 1
        await wait_until(lambda: runtime.state.poll_errors > 0)
        assert not runtime.state.valid
        assert (await client.read_input_registers(52, count=2, device_id=1)).exception_code == 4
        data[1002] = 0
        data[1000] += 1
        await wait_until(lambda: runtime.state.valid)
        assert not (await client.read_input_registers(52, count=2, device_id=1)).isError()
    finally:
        client.close()
        await runtime.close()


async def test_ninth_client_is_refused(meter):
    state, _, port = meter
    sockets = []
    try:
        for _ in range(7):  # The fixture already owns one connection.
            sockets.append(await asyncio.open_connection("127.0.0.1", port))
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        sockets.append((reader, writer))
        assert await asyncio.wait_for(reader.read(1), 1) == b""
        assert state.rejected == 1
    finally:
        for _, writer in sockets:
            writer.close()
            await writer.wait_closed()


async def test_unknown_unit_is_not_served(meter):
    state, _, port = meter
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(struct.pack(">HHHBBHH", 501, 0, 6, 9, 4, 52, 2))
        await writer.drain()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(reader.read(1), 0.1)
        assert state.requests == 0
    finally:
        writer.close()
        await writer.wait_closed()

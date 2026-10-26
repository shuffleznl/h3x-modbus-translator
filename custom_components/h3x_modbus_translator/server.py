"""Read-only SDM630 subset using PyModbus framing, transactions and server engine."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from pymodbus.constants import ExcCodes
from pymodbus.pdu.device import ModbusDeviceIdentification
from pymodbus.server import ModbusTcpServer
from pymodbus.server.requesthandler import ServerRequestHandler
from pymodbus.simulator import DataType, SimData, SimDevice

from .model import MeterState


class MeterRegisters:
    def __init__(self, state: MeterState):
        self.state = state

    async def access(self, function, start, address, count, registers, values):
        state = self.state
        now = time.monotonic()
        state.requests += 1
        state.last_request = now
        state.last_request_utc = datetime.now(UTC).isoformat()
        state.last_requested_address, state.last_requested_count = address, count
        if function != 4 or values is not None:
            state.rejected += 1
            return ExcCodes.ILLEGAL_FUNCTION
        if count < 2 or count > 80 or count % 2 or address % 2:
            state.rejected += 1
            return ExcCodes.ILLEGAL_VALUE
        if not state.fresh(now):
            state.rejected += 1
            return ExcCodes.DEVICE_FAILURE
        words = state.words
        if words is None or any(a not in words for a in range(address, address + count)):
            state.rejected += 1
            return ExcCodes.ILLEGAL_ADDRESS
        registers[address - start : address - start + count] = [
            words[a] for a in range(address, address + count)
        ]
        return None

    def device(self) -> SimDevice:
        # A shared backing block allows our access policy to reject every write/function.
        return SimDevice(
            id=self.state.settings.meter_unit,
            simdata=SimData(0, count=400, datatype=DataType.REGISTERS),
            action=self.access,
        )


class RestrictedHandler(ServerRequestHandler):
    """Reject unapproved peers before allowing their first request through."""

    def connection_made(self, transport):
        super().connection_made(transport)
        self.idle_timer = None
        peer = transport.get_extra_info("peername")
        self.permitted = bool(
            peer
            and self.server.state.settings.permits(peer[0])
            and len(self.server.active_connections) <= 8
        )
        if not self.permitted:
            self.server.state.rejected += 1
            transport.close()
        else:
            self._refresh_idle_timer()

    def _refresh_idle_timer(self):
        if self.idle_timer:
            self.idle_timer.cancel()
        self.idle_timer = self.loop.call_later(120, self.close)

    def close(self, reconnect=False):
        if getattr(self, "idle_timer", None):
            self.idle_timer.cancel()
            self.idle_timer = None
        super().close(reconnect=reconnect)

    def connection_lost(self, exc):
        if getattr(self, "idle_timer", None):
            self.idle_timer.cancel()
            self.idle_timer = None
        super().connection_lost(exc)

    def callback_data(self, data, addr=None):
        if not getattr(self, "permitted", False):
            return len(data)
        self._refresh_idle_timer()
        return super().callback_data(data, addr)

    async def handle_request(self):
        # PyModbus SimCore falls back to device 0 for unknown IDs; do not let an
        # unknown/broadcast ID reach that fallback (or any read/device-ID function).
        if self.last_pdu and self.last_pdu.dev_id != self.server.state.settings.meter_unit:
            self.server.state.rejected += 1
            return
        await super().handle_request()


class MeterServer(ModbusTcpServer):
    def __init__(self, state: MeterState):
        self.state = state
        self.registers = MeterRegisters(state)
        identity = ModbusDeviceIdentification()
        identity.VendorName = "H3X Modbus Translator"
        identity.ProductCode = "SDM630-SUBSET"
        identity.ProductName = "Shelly meter translator (not an Eastron meter)"
        super().__init__(
            self.registers.device(),
            identity=identity,
            address=(state.settings.listen_host, state.settings.listen_port),
            broadcast_enable=False,
            ignore_missing_devices=True,
        )

    def callback_new_connection(self):
        return RestrictedHandler(self, self.trace_packet, self.trace_pdu, self.trace_connect)

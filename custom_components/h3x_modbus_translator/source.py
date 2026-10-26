"""One serialized, read-only Modbus TCP session to a Shelly Pro 3EM."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time

from pymodbus.client import AsyncModbusTcpClient

from .model import MeterState, Phase, Snapshot, bounded, source_float, source_timestamp


class ShellySource:
    def __init__(self, state: MeterState):
        self.state = state
        self.client = self._client(state.settings.host)

    def _client(self, host):
        return AsyncModbusTcpClient(
            host, port=self.state.settings.source_port, timeout=2, retries=0, reconnect_delay=0
        )

    async def _connect(self):
        settings = self.state.settings
        try:
            ipaddress.ip_address(settings.host)
            addresses = [settings.host]
        except ValueError:
            # Async DNS; prefer IPv4 for these meters, then try every resolved address.
            resolved = await asyncio.get_running_loop().getaddrinfo(
                settings.host, settings.source_port, type=socket.SOCK_STREAM
            )
            resolved.sort(key=lambda item: item[0] != socket.AF_INET)
            addresses = list(dict.fromkeys(item[4][0] for item in resolved))
        for address in addresses:
            self.client.close()
            self.client = self._client(address)
            if await self.client.connect():
                return
        raise ConnectionError("Shelly Modbus TCP connection failed")

    async def read(self, address: int, count: int) -> list[int]:
        response = await self.client.read_input_registers(
            address, count=count, device_id=self.state.settings.source_unit
        )
        if response.isError() or len(response.registers) != count:
            raise ValueError(f"Shelly FC04 read rejected at {address}")
        return response.registers

    async def poll(self) -> Snapshot:
        """Read all three phases, checking source time and cross-phase consistency."""
        async with asyncio.timeout(8):
            if not self.client.connected:
                await self._connect()
            for _ in range(2):
                header = await self.read(1000, 17)
                if any(header[2:7]):
                    raise ValueError("Shelly meter error or phase sequence alarm")
                phase_data = []
                for address in (1020, 1040, 1060):
                    phase_data.append(
                        Phase.decode(
                            await self.read(address, 15), self.state.settings.reverse_power
                        )
                    )
                phases = tuple(phase_data)
                after = await self.read(1000, 2)
                if source_timestamp(after) == source_timestamp(header):
                    break
            else:
                raise ValueError("Shelly changed sample during both snapshot attempts")
            energy = await self.read(1160, 6)
            sign = -1 if self.state.settings.reverse_power else 1
            power = bounded(sign * source_float(header, 13), -500000, 500000)
            apparent = bounded(source_float(header, 15), 0, 1500000)
            if abs(sum(p.power for p in phases) - power) > max(50, abs(power) * 0.01):
                raise ValueError("Shelly total and phase powers do not form a coherent snapshot")
            if abs(sum(p.apparent for p in phases) - apparent) > max(50, apparent * 0.01):
                raise ValueError("Shelly total and phase apparent powers are inconsistent")
            if not any(p.voltage >= 80 for p in phases):
                raise ValueError("No valid phase voltage; check Shelly three-phase configuration")
            imported = bounded(source_float(energy, 2) / 1000, 0, 1e12)
            exported = bounded(source_float(energy, 4) / 1000, 0, 1e12)
            if sign < 0:
                imported, exported = exported, imported
            return Snapshot(
                phases,
                power,
                apparent,
                imported,
                exported,
                source_timestamp(header),
                source_timestamp(energy),
                time.monotonic(),
            )

    def close(self) -> None:
        self.client.close()

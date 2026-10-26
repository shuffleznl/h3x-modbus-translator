"""Validated Shelly measurements and explicit SDM630 register translation.

Shelly input addresses remove 30000 (31020 -> 1020), NOT 30001.
Shelly uses low-word-first values; SDM630's default is high-word-first.
"""

from __future__ import annotations

import ipaddress
import math
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class Settings:
    host: str
    source_port: int = 502
    source_unit: int = 1
    listen_host: str = "0.0.0.0"
    listen_port: int = 5020
    meter_unit: int = 1
    allowed_clients: str = "127.0.0.1/32"
    poll_seconds: float = 2.0
    stale_seconds: float = 10.0
    reverse_power: bool = False

    def __post_init__(self):
        host = self.host.strip()
        if not host or any(c.isspace() for c in host) or any(c in host for c in "/\\@?#"):
            raise ValueError("Use a hostname or IP address, without a URL or port")
        if ":" in host:
            ipaddress.IPv6Address(host)
        object.__setattr__(self, "host", host)
        ipaddress.ip_address(self.listen_host)
        if not 1 <= self.source_port <= 65535 or not 1024 <= self.listen_port <= 65535:
            raise ValueError("Invalid TCP port (listener must be >= 1024)")
        if not 1 <= self.source_unit <= 247 or not 1 <= self.meter_unit <= 247:
            raise ValueError("Unit IDs must be 1..247")
        if (
            not 1 <= self.poll_seconds <= 30
            or not 3 * self.poll_seconds <= self.stale_seconds <= 120
        ):
            raise ValueError("Stale timeout must be >= 3 polling intervals and <= 120 seconds")
        if not self.networks:
            raise ValueError("At least one allowed client IP/CIDR is required")

    @property
    def networks(self):
        return tuple(
            ipaddress.ip_network(s.strip(), strict=False)
            for s in self.allowed_clients.split(",")
            if s.strip()
        )

    def permits(self, address: str) -> bool:
        ip = ipaddress.ip_address(address.split("%")[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return any(ip in network for network in self.networks)


def source_float(words: list[int], offset: int = 0) -> float:
    """Decode Shelly's CDAB float32 (big-endian bytes within each word)."""
    return struct.unpack(">f", struct.pack(">HH", words[offset + 1], words[offset]))[0]


def source_timestamp(words: list[int]) -> int:
    return words[0] | words[1] << 16


def float_words(value: float) -> tuple[int, int]:
    """Encode default SDM630 ABCD float32."""
    if not math.isfinite(value):
        raise ValueError("Cannot expose a non-finite measurement")
    return struct.unpack(">HH", struct.pack(">f", value))


def bounded(value: float, low: float, high: float) -> float:
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError("Shelly measurement outside the supported range")
    return value


@dataclass(frozen=True)
class Phase:
    voltage: float
    current: float
    power: float
    apparent: float
    factor: float
    frequency: float

    @classmethod
    def decode(cls, words: list[int], reverse: bool = False) -> Phase:
        if len(words) != 15 or any(words[10:13]):
            raise ValueError("Shelly phase alarm or incomplete response")
        v, i, p, s, pf = (source_float(words, x) for x in (0, 2, 4, 6, 8))
        p *= -1 if reverse else 1
        if abs(p) > s + max(10, 0.02 * s):
            raise ValueError("Shelly phase active power exceeds apparent power")
        return cls(
            bounded(v, 0, 300),
            bounded(i, 0, 1000),
            bounded(p, -500000, 500000),
            bounded(s, 0, 500000),
            math.copysign(min(1, bounded(abs(pf), 0, 1.05)), p),
            bounded(source_float(words, 13), 40, 70),
        )


@dataclass(frozen=True)
class Snapshot:
    phases: tuple[Phase, Phase, Phase]
    power: float
    apparent: float
    import_kwh: float
    export_kwh: float
    source_timestamp: int
    energy_timestamp: int
    received_at: float

    def registers(self) -> Mapping[int, int]:
        values: dict[int, float] = {}
        for n, phase in enumerate(self.phases):
            values.update(
                {
                    2 * n: phase.voltage,
                    6 + 2 * n: phase.current,
                    12 + 2 * n: phase.power,
                    18 + 2 * n: phase.apparent,
                    30 + 2 * n: phase.factor,
                }
            )
        values.update(
            {
                42: sum(p.voltage for p in self.phases) / 3,
                46: sum(p.current for p in self.phases) / 3,
                48: sum(p.current for p in self.phases),
                52: self.power,
                56: self.apparent,
                62: max(-1, min(1, self.power / self.apparent)) if self.apparent else 0,
                70: sum(p.frequency for p in self.phases) / 3,
                72: self.import_kwh,
                74: self.export_kwh,
                342: self.import_kwh + self.export_kwh,
            }
        )
        result: dict[int, int] = {}
        for address, value in values.items():
            hi, lo = float_words(value)
            result[address], result[address + 1] = hi, lo
        return MappingProxyType(result)


@dataclass
class MeterState:
    """One immutable register map per successful poll; no partial updates."""

    settings: Settings
    snapshot: Snapshot | None = None
    words: Mapping[int, int] | None = None
    valid: bool = False
    enabled: bool = True
    listening: bool = False
    last_error: str = "Waiting for Shelly data"
    requests: int = 0
    rejected: int = 0
    poll_errors: int = 0
    last_request: float | None = None
    last_request_utc: str | None = None
    last_source_progress: float | None = None
    last_energy_progress: float | None = None
    last_requested_address: int | None = None
    last_requested_count: int | None = None

    def fresh(self, now: float) -> bool:
        return bool(
            self.enabled
            and self.valid
            and self.snapshot
            and now - self.snapshot.received_at <= self.settings.stale_seconds
            and self.last_source_progress is not None
            and now - self.last_source_progress <= self.settings.stale_seconds
            and self.last_energy_progress is not None
            and now - self.last_energy_progress <= 180
        )

    def publish(self, snapshot: Snapshot) -> None:
        words = snapshot.registers()
        if not self.snapshot or snapshot.source_timestamp != self.snapshot.source_timestamp:
            self.last_source_progress = snapshot.received_at
        if not self.snapshot or snapshot.energy_timestamp != self.snapshot.energy_timestamp:
            self.last_energy_progress = snapshot.received_at
        self.snapshot, self.words = snapshot, words
        self.valid = True
        self.last_error = ""

    def fail(self, reason: str) -> None:
        self.valid = False
        self.poll_errors += 1
        self.last_error = reason

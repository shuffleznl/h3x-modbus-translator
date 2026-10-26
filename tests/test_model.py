"""Sign, byte order, ranges and fail-closed freshness."""

import math
import struct
from dataclasses import replace

import pytest

from custom_components.h3x_modbus_translator.model import (
    MeterState,
    Phase,
    Settings,
    Snapshot,
    float_words,
    source_float,
    source_timestamp,
)


def phase_words(power=1000.0):
    words = []
    for value in (230.0, 5.0, power, abs(power) + 100.0, 0.9):
        hi, lo = float_words(value)
        words.extend([lo, hi])
    words.extend([0, 0, 0])
    hi, lo = float_words(50.0)
    return words + [lo, hi]


def snapshot(now=100.0, power=3000.0, stamp=123):
    phases = tuple(Phase.decode(phase_words(power / 3)) for _ in range(3))
    return Snapshot(phases, power, 3300.0, 123.456, 7.89, stamp, stamp, now)


def unpack(words, address):
    return struct.unpack(">f", struct.pack(">HH", words[address], words[address + 1]))[0]


@pytest.mark.parametrize("value", [-13800.0, -123.75, 0.0, 230.25, 17500.0])
def test_byte_order(value):
    hi, lo = float_words(value)
    assert source_float([lo, hi]) == value
    assert struct.unpack(">f", struct.pack(">HH", hi, lo))[0] == value


def test_known_wire_bytes_and_timestamp():
    assert float_words(230.0) == (0x4366, 0)
    assert source_float([0, 0x4366]) == 230.0
    assert source_timestamp([0xB3CD, 0x6380]) == 1669379021


@pytest.mark.parametrize("power", [-3000.0, 0.0, 3000.0])
def test_registers_and_sign(power):
    s = snapshot(power=power)
    registers = s.registers()
    assert unpack(registers, 52) == power
    assert unpack(registers, 0) == 230
    assert unpack(registers, 70) == 50
    assert unpack(registers, 72) == pytest.approx(123.456)
    assert unpack(registers, 74) == pytest.approx(7.89)
    assert unpack(registers, 342) == pytest.approx(131.346)
    assert 24 not in registers  # Signed reactive power cannot be inferred from W/VA.
    assert 200 not in registers  # Do not assume balanced 120-degree phase angles.
    with pytest.raises(TypeError):
        registers[52] = 0


@pytest.mark.parametrize("power", [-1000.0, 1000.0])
def test_reversed_power_factor_tracks_reversed_watts(power):
    p = Phase.decode(phase_words(power), reverse=True)
    assert p.power == -power
    assert math.copysign(1, p.factor) == math.copysign(1, -power)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_reject_nonfinite(bad):
    with pytest.raises(ValueError):
        float_words(bad)


def test_alarm_and_out_of_range():
    data = phase_words()
    data[11] = 1
    with pytest.raises(ValueError):
        Phase.decode(data)
    with pytest.raises(ValueError):
        Phase.decode(phase_words(900000))


def test_stale_and_frozen_are_not_refreshed_by_repeated_reads():
    state = MeterState(Settings("localhost"))
    state.publish(snapshot())
    assert state.fresh(109)
    assert not state.fresh(111)
    state.publish(replace(snapshot(), received_at=109))
    assert state.fresh(109)
    assert not state.fresh(111)  # Same timestamp cannot prolong validity.
    state.publish(snapshot(now=112, stamp=124))
    assert state.fresh(112)
    state.fail("Disconnected")
    assert not state.fresh(112)


def test_energy_timestamp_has_its_own_progress_guard():
    state = MeterState(Settings("localhost"))
    state.publish(snapshot())
    state.publish(replace(snapshot(now=281, stamp=125), energy_timestamp=123))
    assert not state.fresh(281)


@pytest.mark.parametrize(
    "changes",
    [
        {"host": "http://shelly"},
        {"host": "shelly:502"},
        {"host": ""},
        {"host": "bad host"},
        {"source_port": 0},
        {"listen_port": 502},
        {"meter_unit": 0},
        {"allowed_clients": ""},
        {"allowed_clients": "not-an-ip"},
        {"stale_seconds": 3},
        {"poll_seconds": 31},
        {"listen_host": "hostname"},
    ],
)
def test_invalid_settings(changes):
    with pytest.raises(ValueError):
        Settings(**({"host": "shelly.local"} | changes))


def test_acl_and_hostname():
    s = Settings("shelly-pro3em.local", allowed_clients="127.0.0.1, 192.0.2.4/32")
    assert s.permits("127.0.0.1")
    assert s.permits("::ffff:127.0.0.1")
    assert s.permits("192.0.2.4")
    assert not s.permits("192.0.2.5")

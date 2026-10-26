"""Actual source measurements and translator diagnostics; no claimed inverter acceptance."""

from __future__ import annotations

import time
from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.helpers.entity import EntityCategory

from .const import SENSOR_KEYS
from .entity import MeterEntity

POWER_KEYS = ("grid_power", "grid_import", "grid_export")
ENERGY_KEYS = ("import_energy", "export_energy")


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(MeterSensor(entry, key) for key in SENSOR_KEYS)


class MeterSensor(MeterEntity, SensorEntity):
    def __init__(self, entry, key):
        super().__init__(entry, key, "sensor")
        if key in POWER_KEYS:
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_native_unit_of_measurement = "W"
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_suggested_display_precision = 0
        elif key in ENERGY_KEYS:
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_native_unit_of_measurement = "kWh"
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
            self._attr_suggested_display_precision = 3
        else:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        if key == "data_age":
            self._attr_device_class = SensorDeviceClass.DURATION
            self._attr_native_unit_of_measurement = "s"
            self._attr_suggested_display_precision = 1
        if key == "last_client_request":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def available(self):
        return self.key not in POWER_KEYS + ENERGY_KEYS or self.runtime.state.fresh(
            time.monotonic()
        )

    @property
    def native_value(self):
        state = self.runtime.state
        now = time.monotonic()
        snap = state.snapshot
        match self.key:
            case "profile":
                return "SDM630"
            case "status":
                if not state.listening:
                    return "listener_stopped"
                if not state.fresh(now):
                    return "source_unavailable"
                if not state.last_request or now - state.last_request > 30:
                    return "ready_no_client"
                return "client_requesting"
            case "grid_power":
                return snap.power if snap else None
            case "grid_import":
                return max(0, snap.power) if snap else None
            case "grid_export":
                return max(0, -snap.power) if snap else None
            case "import_energy":
                return snap.import_kwh if snap else None
            case "export_energy":
                return snap.export_kwh if snap else None
            case "data_age":
                if snap and state.last_source_progress is not None:
                    return round(now - min(snap.received_at, state.last_source_progress), 1)
                return None
            case "requests":
                return state.requests
            case "rejected_requests":
                return state.rejected
            case "poll_errors":
                return state.poll_errors
            case "last_client_request":
                return (
                    datetime.fromisoformat(state.last_request_utc)
                    if state.last_request_utc
                    else None
                )
            case "endpoint":
                return f"{state.settings.listen_host}:{state.settings.listen_port}"
            case "last_error":
                if state.last_error:
                    return state.last_error
                if state.fresh(now):
                    return "none"
                if (
                    state.last_energy_progress is not None
                    and now - state.last_energy_progress > 180
                ):
                    return "Energy timestamp stopped advancing"
                return "Power data stale or timestamp stopped advancing"
        return None

    @property
    def extra_state_attributes(self):
        if self.key == "profile":
            return {
                "function": "FC04",
                "float_order": "ABCD",
                "meter_unit": self.runtime.state.settings.meter_unit,
                "native_h3x_tcp_meter_client": False,
                "physical_h3x_meter_connection": "RS485 gateway required",
                "inverter_acceptance_verified": False,
                "unsupported_measurements": (
                    "reactive power, phase angle, THD, demand, line-line voltage"
                ),
            }
        if self.key == "requests":
            state = self.runtime.state
            return {
                "last_wire_address": state.last_requested_address,
                "last_register_count": state.last_requested_count,
            }
        return None

"""Communication health, deliberately distinct from physical inverter acceptance."""

import time

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.helpers.entity import EntityCategory

from .const import BINARY_KEYS
from .entity import MeterEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(MeterBinarySensor(entry, key) for key in BINARY_KEYS)


class MeterBinarySensor(MeterEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry, key):
        super().__init__(entry, key, "binary_sensor")

    @property
    def is_on(self):
        state = self.runtime.state
        now = time.monotonic()
        if self.key == "source_online":
            return state.fresh(now)
        if self.key == "server_listening":
            return state.listening
        return bool(state.last_request is not None and now - state.last_request <= 30)

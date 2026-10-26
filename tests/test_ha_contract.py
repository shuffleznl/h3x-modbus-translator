"""Actual Home Assistant schemas/registry, not guessed dashboard entity slugs."""

import json
import logging
import time
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

pytest.importorskip("homeassistant", reason="HA contract tests run with HA 2026.10.4 in CI")

from homeassistant.config_entries import ConfigEntries, ConfigEntry  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import device_registry as dr  # noqa: E402
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import frame
from homeassistant.helpers.entity_platform import EntityPlatform, _async_derive_object_ids  # noqa: E402

from custom_components.h3x_modbus_translator import (  # noqa: E402
    async_setup_entry,
    async_unload_entry,
)
from custom_components.h3x_modbus_translator.binary_sensor import MeterBinarySensor  # noqa: E402
from custom_components.h3x_modbus_translator.config_flow import ConfigFlow, OptionsFlow, schema  # noqa: E402
from custom_components.h3x_modbus_translator.const import (  # noqa: E402
    BINARY_KEYS,
    DOMAIN,
    SENSOR_KEYS,
)
from custom_components.h3x_modbus_translator.model import MeterState, Settings  # noqa: E402
from custom_components.h3x_modbus_translator.sensor import MeterSensor  # noqa: E402

from .test_model import snapshot  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / DOMAIN


def entity_references(value):
    if isinstance(value, list):
        return set().union(*(entity_references(v) for v in value))
    if not isinstance(value, dict):
        return set()
    result = {value["entity"]} if isinstance(value.get("entity"), str) else set()
    for child in value.values():
        result |= entity_references(child)
    return result


@pytest.fixture
async def ha(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    frame.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    entry = ConfigEntry(
        data=asdict(Settings("shelly.local")),
        domain=DOMAIN,
        options={},
        title="A renamed meter",
        version=1,
        minor_version=1,
        source="user",
        unique_id=DOMAIN,
        discovery_keys=MappingProxyType({}),
        subentries_data=(),
    )
    hass.config_entries._entries[entry.entry_id] = entry
    hass.config_entries._initialized.set()
    state = MeterState(Settings("shelly.local"))
    state.publish(snapshot(now=time.monotonic()))
    entry.runtime_data = SimpleNamespace(state=state)
    hass.data[dr.DATA_REGISTRY] = dr.DeviceRegistry(hass)
    await dr.async_load(hass, load_empty=True)
    await er.async_load(hass, load_empty=True)
    try:
        yield hass, entry
    finally:
        await hass.async_stop()


async def test_dashboard_entities_match_real_registry_even_with_renamed_device(ha):
    hass, entry = ha
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name="A renamed meter",
    )
    registered = {}
    for platform_name, keys, cls in (
        ("sensor", SENSOR_KEYS, MeterSensor),
        ("binary_sensor", BINARY_KEYS, MeterBinarySensor),
    ):
        platform = EntityPlatform(
            hass=hass,
            logger=logging.getLogger(__name__),
            domain=platform_name,
            platform_name=DOMAIN,
            platform=None,
            scan_interval=timedelta(seconds=10),
            entity_namespace=None,
        )
        for key in keys:
            entity = cls(entry, key)
            entity.internal_integration_suggested_object_id = entity.entity_id.split(".", 1)[1]
            suggested, base = _async_derive_object_ids(entity, platform)
            record = er.async_get(hass).async_get_or_create(
                platform_name,
                DOMAIN,
                entity.unique_id,
                config_entry=entry,
                device_id=device.id,
                suggested_object_id=suggested,
                object_id_base=base,
                original_name=entity.name,
                has_entity_name=True,
            )
            assert record.entity_id == f"{platform_name}.{DOMAIN}_{key}"
            registered[record.entity_id] = entity
    dashboard = yaml.safe_load((COMPONENT / "dashboards/h3x-modbus-translator.yaml").read_text())
    assert entity_references(dashboard) <= registered.keys()

    def check_attributes(value):
        if isinstance(value, list):
            for child in value:
                check_attributes(child)
        if isinstance(value, dict):
            if value.get("type") == "attribute":
                assert value["attribute"] in registered[value["entity"]].extra_state_attributes
            for child in value.values():
                check_attributes(child)

    check_attributes(dashboard)
    assert not any(e.entity_registry_enabled_default is False for e in registered.values())
    predictive = ROOT.parent / "h3x-predictive-dispatch"
    if not predictive.exists():
        predictive = ROOT / "predictive-contract"
    if predictive.exists():
        for path in (
            predictive / "dashboards/h3x-predictive-dispatch.yaml",
            predictive
            / "custom_components/h3x_predictive_dispatch/dashboards"
            / "h3x-predictive-dispatch.yaml",
        ):
            doc = yaml.safe_load(path.read_text())
            references = {r for r in entity_references(doc) if ".h3x_modbus_translator_" in r}
            assert references
            assert references <= registered.keys()

            def translator_attributes(value):
                if isinstance(value, list):
                    for child in value:
                        translator_attributes(child)
                if isinstance(value, dict):
                    if value.get("type") == "attribute" and value.get("entity", "") in registered:
                        assert (
                            value["attribute"] in registered[value["entity"]].extra_state_attributes
                        )
                    for child in value.values():
                        translator_attributes(child)

            translator_attributes(doc)


async def test_entities_go_unavailable_instead_of_zero(ha):
    _, entry = ha
    sensor = MeterSensor(entry, "grid_power")
    assert sensor.available
    assert sensor.native_value == 3000
    entry.runtime_data.state.fail("Disconnected")
    assert not sensor.available
    assert not MeterBinarySensor(entry, "source_online").is_on
    assert MeterSensor(entry, "status").available
    assert MeterSensor(entry, "import_energy").native_unit_of_measurement == "kWh"
    assert MeterSensor(entry, "import_energy").state_class == "total_increasing"


async def test_options_flow_persists_endpoints_and_timeout(ha):
    hass, entry = ha
    options = OptionsFlow()
    options.hass = hass
    options.handler = entry.entry_id
    values = asdict(
        Settings(
            "new-shelly.local",
            source_port=1502,
            listen_port=5502,
            meter_unit=7,
            allowed_clients="192.0.2.4",
            stale_seconds=30,
        )
    )
    result = await options.async_step_init(schema()(values))
    assert result["type"] == "create_entry"
    assert result["data"] == values
    assert not result["data"]["reverse_power"]


async def test_invalid_settings_return_form_not_500(ha):
    hass, _ = ha
    flow = ConfigFlow()
    flow.hass = hass
    result = await flow.async_step_user(
        asdict(Settings("shelly.local")) | {"allowed_clients": "bad"}
    )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_settings"}


async def test_entry_setup_and_unload_close_listener(ha, free_port, monkeypatch):
    hass, entry = ha
    entry._data = MappingProxyType(
        asdict(
            Settings(
                "127.0.0.1", source_port=free_port, listen_host="127.0.0.1", listen_port=free_port
            )
        )
    )
    # Mock only platform forwarding; use the real integration/socket/poll-task lifetime.
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True))
    assert await async_setup_entry(hass, entry)
    assert entry.runtime_data.state.listening
    assert await async_unload_entry(hass, entry)
    assert not entry.runtime_data.state.listening
    assert entry.runtime_data.task is None


def test_hacs_package_has_only_its_own_domain():
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    assert manifest["domain"] == DOMAIN
    assert manifest["config_flow"]
    assert manifest["version"] == "0.1.0"
    assert manifest["requirements"] == ["pymodbus==3.13.1"]
    assert json.loads((ROOT / "hacs.json").read_text())["homeassistant"] == "2026.10.4"
    assert (COMPONENT / "translations/en.json").exists()
    assert (COMPONENT / "brand/icon.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

"""H3X meter translation, independent of inverter/battery command ownership."""

from __future__ import annotations

from .const import DOMAIN, PLATFORMS


async def async_setup_entry(hass, entry):
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP
    from homeassistant.exceptions import ConfigEntryNotReady
    from homeassistant.helpers.dispatcher import async_dispatcher_send

    from .model import Settings
    from .runtime import Runtime

    settings = Settings(**{**entry.data, **entry.options})
    runtime = Runtime(settings, lambda: async_dispatcher_send(hass, f"{DOMAIN}_{entry.entry_id}"))
    try:
        await runtime.start()
    except (OSError, RuntimeError) as err:
        raise ConfigEntryNotReady("Cannot bind translator listener; check interface/port") from err
    entry.runtime_data = runtime

    async def stop(_event):
        await runtime.close()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop))
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await runtime.close()
        raise
    return True


async def async_reload_entry(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.close()
        return True
    return False

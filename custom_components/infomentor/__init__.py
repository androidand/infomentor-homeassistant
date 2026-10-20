"""InfoMentor-integration för Home Assistant."""

from __future__ import annotations

from dataclasses import dataclass

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import InfomentorCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.CALENDAR]


@dataclass(slots=True)
class InfomentorRuntimeData:
    """Det som hänger på config-posten i minnet."""

    coordinator: InfomentorCoordinator
    session: aiohttp.ClientSession


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Sätt upp en config-post."""
    # Egen session med egen cookie-jar (inloggningen sätter cookies per domän).
    session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    coordinator = InfomentorCoordinator(hass, entry, session)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = InfomentorRuntimeData(coordinator=coordinator, session=session)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stäng ner en config-post."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.session.close()
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Ladda om när inställningarna ändras."""
    await hass.config_entries.async_reload(entry.entry_id)

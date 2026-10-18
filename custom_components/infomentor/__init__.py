"""InfoMentor-integration för Home Assistant."""

from __future__ import annotations

from dataclasses import dataclass

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_AUTH_MODE, CONF_REFRESH_TOKEN
from .coordinator import InfomentorCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Uppgradera v1 (username/password) till v2 (auth_mode + credentials).

    Fyll i auth_mode så att inloggningen vet vilken väg den ska använda.
    """
    new_data = dict(entry.data)
    new_data.setdefault(CONF_AUTH_MODE, "password")
    hass.config_entries.async_update_entry(entry, data=new_data, version=2)
    return True


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
    """Ladda om när inställningarna ändras.

    En roterande refresh-token uppdateras ofta (per update) och ska *inte* starta om
    integrationen — det stänger den session vi bara byggt. Hoppar över när enda
    skillnaden mot den senast kända posten är refresh-token-värdet; alla andra
    ändringar (intervall, skolmat, namn, inloggningsuppgifter) ladda däremot om.
    """
    prev = getattr(entry, "_last_snapshot_data", None)
    # Jämför utan token — är enda skillnaden refresh-token, behåll sessionen.
    without_token = {k: v for k, v in entry.data.items() if k != CONF_REFRESH_TOKEN}
    entry._last_snapshot_data = dict(entry.data)
    if prev is not None:
        prev_without_token = {k: v for k, v in prev.items() if k != CONF_REFRESH_TOKEN}
        if prev_without_token == without_token:
            return
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)


@dataclass(slots=True)
class InfomentorRuntimeData:
    """Det som hänger på config-posten i minnet."""

    coordinator: InfomentorCoordinator
    session: aiohttp.ClientSession

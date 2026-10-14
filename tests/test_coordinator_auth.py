"""Tester för koordinatorns token-rotation (körs i CI)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("homeassistant")

from homeassistant.config_entries import ConfigEntry  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.exceptions import ConfigEntryAuthFailed  # noqa: E402
from homeassistant.helpers.update_coordinator import UpdateFailed  # noqa: E402

from custom_components.infomentor.api import CannotConnect, RevokedAuth  # noqa: E402
from custom_components.infomentor.const import DOMAIN  # noqa: E402


def _make_coordinator(hass: HomeAssistant, entry: ConfigEntry):
    """Bygg en koordinator med mockad api (utan aiohttp-session)."""
    from custom_components.infomentor.coordinator import InfomentorCoordinator

    coord = InfomentorCoordinator.__new__(InfomentorCoordinator)
    coord.entry = entry
    coord._lock = None  # sätts nedan
    coord.api = MagicMock()
    coord._ok_calls = 0
    coord._auth_skips = 0
    coord._refresh_inflight = None
    coord._plan_details = {}
    coord._plan_ids_seen = set()
    coord.hass = hass
    return coord


@pytest.fixture
def entry(hass: HomeAssistant) -> ConfigEntry:
    """En config-post med en refresh-token."""
    return ConfigEntry(
        created_at=None,
        data={"refresh_token": "old-refresh-token"},
        disabled_by=None,
        discovery_keys={},
        domain=DOMAIN,
        entry_id="test-entry",
        minor_version=2,
        modified_at=None,
        options={},
        source="user",
        state="not_loaded",
        subentries_data=[],
        title="InfoMentor",
        unique_id="test",
        version=2,
    )


async def test_revoked_token_triggers_reauth(hass, entry: ConfigEntry) -> None:
    """En REVOKED-token höjs som ConfigEntryAuthFailed, aldrig UpdateFailed."""
    coord = _make_coordinator(hass, entry)
    coord._lock = __import__("asyncio").Lock()
    coord.api.async_login_with_token = AsyncMock(side_effect=RevokedAuth("ogiltig"))

    with pytest.raises(ConfigEntryAuthFailed):
        await coord._login()


async def test_blocked_not_revoked(hass, entry: ConfigEntry) -> None:
    """Ett BLOCKED-svar höjs som UpdateFailed (försök igen), aldrig reauth."""
    coord = _make_coordinator(hass, entry)
    coord._lock = __import__("asyncio").Lock()
    coord.api.async_login_with_token = AsyncMock(side_effect=CannotConnect("nere"))

    with pytest.raises(UpdateFailed):
        await coord._login()


async def test_rotated_token_written_before_bridge(hass, entry: ConfigEntry) -> None:
    """Efter en lyckad refresh skriver koordinatorn tillbaka den roterade token."""
    coord = _make_coordinator(hass, entry)
    coord._lock = __import__("asyncio").Lock()

    captured: dict[str, Any] = {}

    async def fake_login(refresh_token: str, on_rotated=None) -> list[dict[str, Any]]:  # type: ignore[valid-type]  # noqa: PGH003
        if on_rotated:
            on_rotated("rotated-token-xyz")
        return [{"pupil_id": "1"}]

    coord.api.async_login_with_token = AsyncMock(side_effect=fake_login)

    def fake_update(e, data):
        captured["data"] = data
        return True

    with patch.object(hass.config_entries, "async_update_entry", side_effect=fake_update):
        await coord._login()

    assert captured["data"]["refresh_token"] == "rotated-token-xyz"


async def test_no_refresh_token_reauth(hass, entry: ConfigEntry) -> None:
    """Utan refresh-token i posten startas reauth."""
    coord = _make_coordinator(hass, entry)
    coord._lock = __import__("asyncio").Lock()
    coord.api.async_login_with_token = AsyncMock(return_value=[])

    entry.__dict__["data"] = {}

    with pytest.raises(ConfigEntryAuthFailed):
        await coord._login()


async def test_two_updates_race_make_one_request(hass, entry: ConfigEntry) -> None:
    """Två parallella uppdateringar gör exakt ett token-anrop och båda får samma token."""
    import asyncio

    coord = _make_coordinator(hass, entry)
    coord._lock = asyncio.Lock()

    call_count = {"n": 0}
    received: set[str] = set()

    async def fake_login(refresh_token: str, on_rotated=None) -> list[dict[str, Any]]:  # type: ignore[valid-type]  # noqa: PGH003
        call_count["n"] += 1
        if on_rotated:
            on_rotated("rotated-token-xyz")
        await asyncio.sleep(0.01)
        received.add("rotated-token-xyz")
        return [{"pupil_id": "1"}]

    coord.api.async_login_with_token = AsyncMock(side_effect=fake_login)

    with patch.object(hass.config_entries, "async_update_entry", return_value=True):
        results = await asyncio.gather(coord._login(), coord._login())

    assert all(pupils for pupils in results)
    assert call_count["n"] == 1
    assert received == {"rotated-token-xyz"}

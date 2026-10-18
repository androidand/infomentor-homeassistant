"""The coordinator's login: which failures mean reauth, and one holder for the rotating token."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("homeassistant")

from homeassistant.exceptions import ConfigEntryAuthFailed  # noqa: E402
from homeassistant.helpers.update_coordinator import UpdateFailed  # noqa: E402

from custom_components.infomentor.api import CannotConnect, InvalidAuth, RevokedAuth  # noqa: E402
from custom_components.infomentor.coordinator import InfomentorCoordinator  # noqa: E402


def make_coordinator(hass, data):
    """A coordinator with a mocked api and a plain entry, without Home Assistant's setup."""
    coord = InfomentorCoordinator.__new__(InfomentorCoordinator)
    coord.hass = hass
    coord.entry = SimpleNamespace(data=data)
    coord.api = MagicMock()
    coord._lock = asyncio.Lock()
    return coord


TOKEN_ENTRY = {"auth_mode": "token", "refresh_token": "old"}


@pytest.mark.parametrize(
    ("data", "failure", "expected"),
    [
        (TOKEN_ENTRY, RevokedAuth("dead"), ConfigEntryAuthFailed),
        (TOKEN_ENTRY, CannotConnect("down or blocked"), UpdateFailed),  # never reauth
        ({"auth_mode": "password"}, InvalidAuth("wrong password"), ConfigEntryAuthFailed),
        ({"auth_mode": "password"}, CannotConnect("down"), UpdateFailed),
    ],
    ids=["token-revoked", "token-blocked", "password-wrong", "password-down"],
)
async def test_a_failed_login_is_reauth_only_when_the_credential_is_dead(
    hass, data, failure, expected
) -> None:
    coord = make_coordinator(hass, data)
    coord.api.async_login_with_token = AsyncMock(side_effect=failure)
    coord.api.async_login = AsyncMock(side_effect=failure)
    with pytest.raises(expected):
        await coord._login()


async def test_password_mode_never_touches_the_token_flow(hass) -> None:
    coord = make_coordinator(hass, {"auth_mode": "password"})
    coord.api.async_login = AsyncMock(return_value=[{"id": "1"}])
    await coord._login()
    coord.api.async_login.assert_awaited_once()
    coord.api.async_login_with_token.assert_not_called()


async def test_an_entry_without_a_refresh_token_asks_for_reauth(hass) -> None:
    coord = make_coordinator(hass, {"auth_mode": "token"})
    with pytest.raises(ConfigEntryAuthFailed):
        await coord._login()


def _rotating_api(coord):
    """An api whose token is single-use: it rejects a token it has already rotated."""
    used: list[str] = []
    alive = {"old"}

    async def login(refresh_token, on_rotated=None):
        await asyncio.sleep(0.01)  # a slow network: the other login gets its chance to read the token
        used.append(refresh_token)
        if refresh_token not in alive:
            raise RevokedAuth("consumed")
        alive.discard(refresh_token)
        new = f"rotated-{len(used)}"
        alive.add(new)
        on_rotated(new)
        return [{"id": "1"}]

    coord.api.async_login_with_token = login
    coord.hass.config_entries.async_update_entry = lambda entry, data: setattr(entry, "data", data)
    return used


async def test_the_rotated_token_is_stored_before_the_login_continues(hass) -> None:
    coord = make_coordinator(hass, dict(TOKEN_ENTRY))
    stored_during_login = []

    async def login(refresh_token, on_rotated=None):
        on_rotated("rotated")
        stored_during_login.append(coord.entry.data["refresh_token"])
        raise CannotConnect("fails after the rotation")

    coord.api.async_login_with_token = login
    coord.hass.config_entries.async_update_entry = lambda entry, data: setattr(entry, "data", data)
    with pytest.raises(UpdateFailed):
        await coord._login()
    assert stored_during_login == ["rotated"]
    assert coord.entry.data["refresh_token"] == "rotated"  # the next try uses the live one


async def test_two_overlapping_logins_never_use_the_same_token_twice(hass) -> None:
    coord = make_coordinator(hass, dict(TOKEN_ENTRY))
    used = _rotating_api(coord)
    await asyncio.gather(coord._login(), coord._login())
    assert used == ["old", "rotated-1"]

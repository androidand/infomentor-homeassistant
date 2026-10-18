"""Det som gick fel vid första uppsättningen på en riktig HA (loggen 2026-10-18)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("homeassistant")

from homeassistant import data_entry_flow  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers.update_coordinator import UpdateFailed  # noqa: E402,F401
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.infomentor.api import ApiError, InvalidAuth  # noqa: E402
from custom_components.infomentor.const import DOMAIN  # noqa: E402


def _coordinator(hass: HomeAssistant):
    from custom_components.infomentor.coordinator import InfomentorCoordinator

    coord = InfomentorCoordinator.__new__(InfomentorCoordinator)
    coord._ok_calls = 0
    coord._auth_skips = 0
    coord._not_permitted = set()
    coord.hass = hass
    return coord


async def _rejected():
    raise InvalidAuth("/learnlog/learnlog/appData omdirigerade till HandleUnauthorizedRequest")


async def _ok():
    return ["data"]


async def test_a_rejected_section_is_skipped_when_other_calls_got_through(hass) -> None:
    """Alla barn och skolor har inte alla funktioner. Gick andra anrop igenom lever
    sessionen, och en ny inloggning hjälper inte (den avvisades dessutom direkt efter)."""
    coord = _coordinator(hass)
    assert await coord._safe("schema", _ok(), [], strict=True) == ["data"]
    assert await coord._safe("lärlogg", _rejected(), {"default": 1}, strict=True) == {"default": 1}
    assert coord._not_permitted == {"lärlogg"}


async def test_a_rejected_first_call_still_means_a_dead_session(hass) -> None:
    """Har inget gått igenom kan sessionen vara död: då loggar vi in igen."""
    coord = _coordinator(hass)
    with pytest.raises(InvalidAuth):
        await coord._safe("schema", _rejected(), [], strict=True)


async def test_the_not_permitted_warning_is_logged_once(hass, caplog) -> None:
    import logging

    coord = _coordinator(hass)
    await coord._safe("schema", _ok(), [], strict=True)
    with caplog.at_level(logging.DEBUG, logger="custom_components.infomentor.coordinator"):
        await coord._safe("lärlogg", _rejected(), {}, strict=True)
        await coord._safe("lärlogg", _rejected(), {}, strict=True)
    levels = [r.levelno for r in caplog.records if "lärlogg" in r.getMessage()]
    assert levels == [logging.WARNING, logging.DEBUG]


async def test_other_errors_are_still_skipped_not_fatal(hass) -> None:
    async def broken():
        raise ApiError("kaputt")

    coord = _coordinator(hass)
    assert await coord._safe("x", broken(), "dflt", strict=True) == "dflt"


# ---------------------------------------------------------------- update-lyssnaren
def _listener_entry(data, snapshot):
    return SimpleNamespace(
        data=data, entry_id="e1", runtime_data=SimpleNamespace(snapshot=snapshot)
    )


async def test_a_token_rotation_does_not_reload_the_entry(hass) -> None:
    from custom_components.infomentor import _async_update_listener, _without_token

    base = {"auth_mode": "token", "refresh_token": "t1"}
    entry = _listener_entry({**base, "refresh_token": "t2"}, _without_token(base))
    hass.config_entries.async_reload = AsyncMock()
    await _async_update_listener(hass, entry)
    hass.config_entries.async_reload.assert_not_called()


async def test_a_real_change_reloads_once_and_only_once(hass) -> None:
    from custom_components.infomentor import _async_update_listener, _without_token

    base = {"auth_mode": "token", "refresh_token": "t1"}
    entry = _listener_entry({**base, "idp": "other"}, _without_token(base))
    hass.config_entries.async_reload = AsyncMock()
    await _async_update_listener(hass, entry)
    await _async_update_listener(hass, entry)  # samma ändring igen: inget nytt att ladda om
    hass.config_entries.async_reload.assert_awaited_once_with("e1")


# ---------------------------------------------------------------- om-auth
@pytest.fixture
def token_entry(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, data={"auth_mode": "token", "refresh_token": "dead"}
    )
    entry.add_to_hass(hass)
    return entry


async def test_reauth_of_a_token_entry_offers_bankid_or_a_pasted_token(
    hass: HomeAssistant, token_entry
) -> None:
    result = await token_entry.start_reauth_flow(hass)
    assert result["type"] == data_entry_flow.FlowResultType.MENU
    assert result["menu_options"] == ["bankid", "reauth_token"]


async def test_reauth_token_step_exists_and_updates_the_entry(
    hass: HomeAssistant, token_entry
) -> None:
    """Loggen: "Handler InfomentorConfigFlow doesn't support step reauth_token"."""
    result = await token_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reauth_token"}
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "reauth_token"

    with (
        patch(
            "custom_components.infomentor.config_flow._validate_refresh_token",
            AsyncMock(return_value=([{"id": "1"}], "fresh")),
        ),
        patch("custom_components.infomentor.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"refresh_token": "pasted"}
        )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert token_entry.data["refresh_token"] == "fresh"


# ---------------------------------------------------------------- inloggningssessionen
async def test_the_bridge_session_is_closed_after_a_successful_login() -> None:
    """Loggen: "Unclosed client session" / "Unclosed connector" efter varje inloggning."""
    import aiohttp

    from custom_components.infomentor import api as api_module
    from custom_components.infomentor.token_client import CookieJar

    api = api_module.InfomentorApi(MagicMock())
    api._session.cookie_jar = MagicMock()
    api._set_cookie_jar = AsyncMock()
    api._follow = AsyncMock(return_value=("u", "selectedPupilName"))
    with (
        patch.object(api_module, "app_refresh_token", AsyncMock(return_value={
            "access_token": "a", "refresh_token": "r2", "token_type": "bearer", "expires_in": 600, "scope": "s"})),
        patch.object(api_module, "bridge_web_session", AsyncMock(return_value=CookieJar())),
        patch.object(api_module, "extract_pupils", return_value=[{"id": "1"}]),
    ):
        await api.async_login_with_token("r1")
    assert api._api_session is None
    assert aiohttp  # importerad för att tydliggöra vad som annars läcker

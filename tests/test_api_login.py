"""InfomentorApis token- och BankID-inloggningar: felmappning, rotation, sessioner och cookies."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from custom_components.infomentor import api as api_module
from custom_components.infomentor.api import (
    ApiError,
    CannotConnect,
    InfomentorApi,
    InvalidAuth,
    RevokedAuth,
)
from custom_components.infomentor.token_client import Blocked, TokenError, TokenRevoked
from custom_components.infomentor.web import CookieJar

PAGE = "<script>selectedPupilName:'Anna'</script>"
TOKEN = {"access_token": "a", "refresh_token": "r2", "token_type": "bearer", "expires_in": 600}


@pytest.fixture
async def api():
    session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    yield InfomentorApi(session)
    await session.close()


def _jar() -> CookieJar:
    jar = CookieJar()
    jar.set_from_header("IMHome=hub-session; Path=/", "https://hub.infomentor.se/")
    jar.set_from_header("Auth=x; Domain=.infomentor.se; Path=/", "https://infomentor.se/")
    return jar


def _login(api, *, refresh=None, bridge=None, page=PAGE):
    """Ersätt de tre nätverkstegen i en token-inloggning."""
    api._follow = AsyncMock(return_value=("https://hub.infomentor.se/", page))
    return (
        patch.object(api_module, "app_refresh_token", refresh or AsyncMock(return_value=TOKEN)),
        patch.object(api_module, "bridge_web_session", bridge or AsyncMock(return_value=_jar())),
        patch.object(api_module, "extract_pupils", return_value=[{"id": "1"}]),
    )


async def test_the_hub_session_gets_the_bridge_cookies_for_the_right_hosts(api) -> None:
    patches = _login(api)
    with patches[0], patches[1], patches[2]:
        await api.async_login_with_token("r1")
    sent = api._session.cookie_jar.filter_cookies(api_module.URL("https://hub.infomentor.se/"))
    assert {"IMHome", "Auth"} <= set(sent)


async def test_the_rotated_token_is_reported_before_the_hub_is_touched(api) -> None:
    seen = []
    patches = _login(api)
    api._follow = AsyncMock(side_effect=lambda url: seen.append("hub") or ("u", PAGE))
    with patches[0], patches[1], patches[2]:
        await api.async_login_with_token("r1", on_rotated=lambda token: seen.append(token))
    assert seen == ["r2", "hub"]


async def test_the_bridge_session_is_closed_after_a_login(api) -> None:
    sessions = []
    real = api_module.new_raw_cookie_session
    patches = _login(api)
    with (
        patches[0],
        patches[1],
        patches[2],
        patch.object(
            api_module, "new_raw_cookie_session", lambda: sessions.append(real()) or sessions[-1]
        ),
    ):
        await api.async_login_with_token("r1")
    assert sessions
    assert all(session.closed for session in sessions)


@pytest.mark.parametrize(
    ("refresh", "bridge", "page", "raised"),
    [
        (AsyncMock(side_effect=TokenRevoked("dead")), None, PAGE, RevokedAuth),
        (AsyncMock(side_effect=Blocked("F5")), None, PAGE, CannotConnect),  # aldrig en om-auth
        (None, AsyncMock(side_effect=TokenError("handoff")), PAGE, ApiError),
        (None, None, "<html>login</html>", RevokedAuth),  # hubben godtog inte sessionen
    ],
    ids=["revoked", "blocked", "bridge-failed", "hub-refused"],
)
async def test_each_failure_maps_to_the_error_the_coordinator_expects(
    api, refresh, bridge, page, raised
) -> None:
    patches = _login(api, refresh=refresh, bridge=bridge, page=page)
    with patches[0], patches[1], patches[2], pytest.raises(raised):
        await api.async_login_with_token("r1")


async def test_a_bankid_login_returns_the_children_and_the_paired_token(api) -> None:
    driver = AsyncMock()
    driver.run.return_value = type("R", (), {"saml_response": "S", "jar": CookieJar()})()
    driver.post_saml_response.return_value = _jar()
    api._follow = AsyncMock(return_value=("u", PAGE))
    with (
        patch.object(api_module, "BankIDDriver", return_value=driver),
        patch.object(api_module, "pair_app_token", AsyncMock(return_value=TOKEN)) as pair,
        patch.object(api_module, "extract_pupils", return_value=[{"id": "1"}]),
    ):
        pupils, refresh_token = await api.async_login_with_bankid()
    assert (pupils, refresh_token) == ([{"id": "1"}], "r2")
    assert len(pair.await_args.kwargs["device_identifier"]) == 16


@pytest.mark.parametrize("step", ["post_saml_response", "pair"])
async def test_a_bankid_hop_that_fails_is_reported_as_a_refused_login(api, step) -> None:
    driver = AsyncMock()
    driver.run.return_value = type("R", (), {"saml_response": "S", "jar": CookieJar()})()
    driver.post_saml_response.return_value = _jar()
    api._follow = AsyncMock(return_value=("u", PAGE))
    if step == "post_saml_response":
        driver.post_saml_response.side_effect = TokenError("login.ashx")
    pair = (
        AsyncMock(side_effect=TokenError("pairing"))
        if step == "pair"
        else AsyncMock(return_value=TOKEN)
    )
    with (
        patch.object(api_module, "BankIDDriver", return_value=driver),
        patch.object(api_module, "pair_app_token", pair),
        patch.object(api_module, "extract_pupils", return_value=[]),
        pytest.raises(InvalidAuth),
    ):
        await api.async_login_with_bankid()

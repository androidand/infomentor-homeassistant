"""Tester för config- och optionsflödet (körs i CI)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("homeassistant")

from homeassistant import config_entries, data_entry_flow  # noqa: E402
from homeassistant.config_entries import ConfigEntry  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402

from custom_components.infomentor.api import (  # noqa: E402
    CannotConnect,
    InvalidAuth,
    RevokedAuth,
)
from custom_components.infomentor.config_flow import InfomentorConfigFlow  # noqa: E402
from custom_components.infomentor.const import DOMAIN  # noqa: E402

USER_INPUT = {"username": "foralder@example.com", "password": "hemligt"}  # noqa: S105
VALIDATE = "custom_components.infomentor.config_flow._validate_credentials"

TOKEN_INPUT = {"refresh_token": "paste-refresh-token-abc123"}
VALIDATE_TOKEN = "custom_components.infomentor.config_flow._validate_refresh_token"


async def _start_password_flow(hass: HomeAssistant) -> dict:
    """Starta user-flödet och välja användarnamn/lösenord från menyn."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"method": "password"}
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "password"
    return result


async def test_user_flow_shows_menu(hass: HomeAssistant) -> None:
    """Första steget är en meny med app-token och lösenord."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_menu_routes_to_token(hass: HomeAssistant) -> None:
    """Att välja app-token i menyn leder till token-steget."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"method": "user_token"}
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user_token"


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    """Ett lyckat inloggningsförsök skapar en config-post."""
    result = await _start_password_flow(hass)
    with patch(VALIDATE, AsyncMock(return_value=[{"id": "1"}, {"id": "2"}])):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"]["username"] == USER_INPUT["username"]
    assert result["result"].unique_id == USER_INPUT["username"]


async def test_user_flow_invalid_auth(hass: HomeAssistant) -> None:
    """Fel uppgifter visar ett fel och skapar ingen post."""
    result = await _start_password_flow(hass)
    with patch(VALIDATE, AsyncMock(side_effect=InvalidAuth("nope"))):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == "invalid_auth"


async def test_user_flow_cannot_connect(hass: HomeAssistant) -> None:
    """Nätverksfel visas som cannot_connect."""
    result = await _start_password_flow(hass)
    with patch(VALIDATE, AsyncMock(side_effect=CannotConnect("nere"))):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["errors"]["base"] == "cannot_connect"


async def test_already_configured(hass: HomeAssistant) -> None:
    """Samma konto kan inte läggas till två gånger."""
    with patch(VALIDATE, AsyncMock(return_value=[{"id": "1"}])):
        first = await _start_password_flow(hass)
        first = await hass.config_entries.flow.async_configure(first["flow_id"], USER_INPUT)
        assert first["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY

        second = await _start_password_flow(hass)
        second = await hass.config_entries.flow.async_configure(second["flow_id"], USER_INPUT)

    assert second["type"] == data_entry_flow.FlowResultType.ABORT
    assert second["reason"] == "already_configured"


async def test_token_flow_creates_entry(hass: HomeAssistant) -> None:
    """En pastad refresh-token valideras och posten sparar den ROTERADE tokenen."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user_token"}
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user_token"

    with patch(
        VALIDATE_TOKEN,
        AsyncMock(return_value=([{"id": "1"}, {"id": "2"}], "rotated-token-xyz")),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], TOKEN_INPUT)

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"]["refresh_token"] == "rotated-token-xyz"
    assert result["data"]["auth_mode"] == "token"


async def test_token_flow_invalid_pasted_token(hass: HomeAssistant) -> None:
    """En token servern avvisar som REVOKED visar invalid_auth och skapar inget."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user_token"}
    )
    with patch(VALIDATE_TOKEN, AsyncMock(side_effect=RevokedAuth("ogiltig"))):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], TOKEN_INPUT)

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == "invalid_auth"


async def test_migrate_v1_to_v2(hass: HomeAssistant) -> None:
    """V1-post (användarnamn/lösenord) migreras till v2 med auth_mode=token."""
    entry = ConfigEntry(
        data={"username": "user@example.com", "password": "hemligt"},
        discovery_keys={},
        domain=DOMAIN,
        minor_version=1,
        options={},
        source="user",
        subentries_data=None,
        title="InfoMentor",
        unique_id="user@example.com",
        version=1,
    )
    hass.config_entries._entries[entry.entry_id] = entry

    flow = InfomentorConfigFlow()
    flow.hass = hass
    assert await flow.async_migrate_entry(entry) is True
    assert entry.version == 2
    assert "password" not in entry.data
    assert "username" not in entry.data
    assert entry.data["auth_mode"] == "token"


@pytest.fixture
def bankid_validate():
    """Byter ut driver:n för hela testet. `box["fn"]` sätts av testet.

    Måste gälla hela testet: bakgrundsuppgiften startar först efter ett await, så ett
    patch som bara omsluter anropet som skapar uppgiften skulle släppa igenom det
    riktiga nätverksanropet.
    """
    box: dict = {}

    async def _call(hass, idp, progress):
        return await box["fn"](hass, idp, progress)

    with (
        patch("custom_components.infomentor.config_flow._validate_bankid", _call),
        patch("custom_components.infomentor.async_setup_entry", return_value=True),
    ):
        yield box


async def _finish_bankid(hass: HomeAssistant, flow_id: str):
    """Låt bakgrundsuppgiften bli klar. HA går vidare av sig självt när den är det, så
    flödet kan redan vara avslutat; då finns resultatet i config-posterna."""
    await hass.async_block_till_done()
    try:
        return await hass.config_entries.flow.async_configure(flow_id)
    except data_entry_flow.UnknownFlow:
        return None


async def _start_bankid(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"method": "bankid"})


async def test_bankid_flow_shows_progress_then_creates_entry(
    hass: HomeAssistant, bankid_validate
) -> None:
    """Menyval -> progress medan BankID väntar -> färdig -> post."""
    from custom_components.infomentor.bankid import BankIDStep

    release = asyncio.Event()

    async def fake_validate(hass, idp, progress):
        await progress(BankIDStep("Skanna QR", qr_data="q", autostart_url="bankid:///?x=1"))
        await release.wait()
        return [{"id": "1"}], "rt-new"

    bankid_validate["fn"] = fake_validate
    result = await _start_bankid(hass)
    assert result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "bankid"
    assert result["description_placeholders"]["autostart_url"] == "bankid:///?x=1"

    release.set()
    await _finish_bankid(hass, result["flow_id"])
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    # BankID är vägen till appens refresh-token; det är den som sparas.
    assert entry.data == {"refresh_token": "rt-new", "auth_mode": "token"}
    assert entry.title == "InfoMentor (1 barn)"


async def test_bankid_failure_shows_error_and_retry_starts_a_new_order(
    hass: HomeAssistant, bankid_validate
) -> None:
    """Ett BankID-fel visar felet; Skicka startar en ny order i stället för att fastna."""
    from custom_components.infomentor.bankid import BankIDDenied

    attempts = 0

    async def fake_validate(hass, idp, progress):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise BankIDDenied("avbröt")
        return [{"id": "1"}], "rt-new"

    bankid_validate["fn"] = fake_validate
    result = await _start_bankid(hass)
    result = await _finish_bankid(hass, result["flow_id"])
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "bankid_retry"
    assert result["errors"] == {"base": "bankid_denied"}

    # En omrendering (user_input=None) får inte starta en ny order; bara Skicka gör det.
    again = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert again["step_id"] == "bankid_retry"
    assert attempts == 1

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await _finish_bankid(hass, result["flow_id"])
    assert attempts == 2
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_bankid_task_is_cancelled_when_the_dialog_is_closed(
    hass: HomeAssistant, bankid_validate
) -> None:
    # HA avbryter progress_task själv när flödet avbryts; testet låser det beteendet,
    # så att ingen egen städning behövs och ingen läcker en väntande BankID-order.
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_validate(hass, idp, progress):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    bankid_validate["fn"] = fake_validate
    result = await _start_bankid(hass)
    await started.wait()
    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()
    assert cancelled.is_set()

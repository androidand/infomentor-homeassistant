"""Tester för config- och optionsflödet (körs i CI)."""

from __future__ import annotations

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


async def test_bankid_flow_shows_idp_step(hass: HomeAssistant) -> None:
    """BankID-flödet visar ett IDP-val först."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"method": "bankid"}
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "bankid"


async def test_bankid_qr_step_shows_qr_selector(hass: HomeAssistant) -> None:
    """BankID-qr-steget visar en QrCodeSelector med QR-data."""
    from custom_components.infomentor.bankid import BankIDStep

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"method": "bankid"}
    )

    qr_data = "bankid:///?autostarttoken=abc123&redirect=null"

    async def _fake_validate(hass, idp, progress):
        await progress(BankIDStep("Väntar på BankID…", qr_data=qr_data, autostart_url=qr_data))
        return [{"id": "1"}]

    with patch(
        "custom_components.infomentor.config_flow._validate_bankid",
        _fake_validate,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"idp": "stockholm_par"}
        )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "bankid_qr"
    schema = result["data_schema"]
    assert schema is not None
    assert "qr" in schema.schema

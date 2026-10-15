"""Config- och optionsflöde för InfoMentor."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.helpers import selector

from .api import (
    CannotConnect,
    InfomentorApi,
    InvalidAuth,
    RevokedAuth,
)
from .bankid import BankIDDenied, BankIDError, BankIDPageChanged, BankIDTimeout
from .const import (
    CONF_ENABLE_LUNCH,
    CONF_MATEO_UNIT,
    CONF_NAMES,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_MIN,
    DOMAIN,
    IDP_STOCKHOLM_PAR,
    MAX_SCAN_INTERVAL_MIN,
    MIN_SCAN_INTERVAL_MIN,
)

_LOGGER = logging.getLogger(__name__)

CONF_USERNAME = "username"
CONF_PASSWORD = "password"  # noqa: S105
CONF_REFRESH_TOKEN = "refresh_token"  # noqa: S105
CONF_AUTH_MODE = "auth_mode"  # "password" eller "token"

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL)
        ),
        vol.Required(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)

STEP_TOKEN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_REFRESH_TOKEN): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)


async def _make_session() -> aiohttp.ClientSession:
    """Skapa en tillfällig session med egen cookie-jar (isolerad från HAs)."""
    return aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))


async def _validate_credentials(hass, username: str, password: str) -> list[dict[str, Any]]:
    """Logga in med en egen, tillfällig session och returnera barnen.

    Vi skapar en egen aiohttp-session (egna cookies för inloggningen) i stället
    för HAs delade — då är det korrekt att stänga den, och HAs session rörs inte.
    """
    session = await _make_session()
    try:
        api = InfomentorApi(session, username, password)
        return await api.async_login()
    finally:
        await session.close()


async def _validate_bankid(
    hass, idp: str, show_progress
) -> list[dict[str, Any]]:
    """Logga in med interaktiv BankID via driver:n.

    Kör driver:n med async_show_progress (QR/autostart + uppgift som pollar).
    Använder en egen session som stängs efteråt. Kastar BankIDError-typer,
    InvalidAuth (bridgen avvisades) eller CannotConnect (nere).
    """
    session = await _make_session()
    try:
        api = InfomentorApi(session)

        async def _progress(step):
            await show_progress(
                progress_action=step.action,
                progress_task=step.task,
                description=step.message,
                item_number=0,
            )

        return await api.async_login_with_bankid(idp=idp, progress=_progress)
    finally:
        await session.close()


async def _validate_refresh_token(hass, refresh_token: str) -> tuple[list[dict[str, Any]], str]:
    """Validera en pastad refresh-token: friska, bridge, plocka barnen.

    Använder en egen session som stängs efteråt. Returnerar (barn, roterad_token)
    på framgång, RevokedAuth på ogiltig token, CannotConnect på nere/F5. Roteringen
    fångas via on_rotated så att vi sparar den NYA tokenen — den pastade är förbrukad
    så snart refreshen gått (persist-before-use).
    """
    session = await _make_session()
    try:
        api = InfomentorApi(session)
        captured: list[str] = []
        pupils = await api.async_login_with_token(refresh_token, on_rotated=captured.append)
        return pupils, (captured[0] if captured else refresh_token)
    finally:
        await session.close()


class InfomentorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Sätt upp integrationen via UI."""

    VERSION = 2

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Meny: välj app-token (rekommenderat) eller användarnamn/lösenord."""
        if user_input is not None:
            if user_input["method"] == "user_token":
                return await self.async_step_user_token()
            if user_input["method"] == "bankid":
                return await self.async_step_bankid()
            return await self.async_step_password()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required("method"): vol.In(["user_token", "password", "bankid"])}
            ),
        )

    async def async_step_bankid(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Interaktiv BankID-inloggning (Phase B: Stockholm Stad).

        Visar en aktiv step med async_show_progress: QR-kod/autostart-länk och en
        uppgift som pollar IdP:t tills BankID-appen godkänner. Kör driver:n och
        konvergerar vid hubbens LoginCallback.

        Endast Stockholm Stad (SiteMinder) stöds idag; andra skolor får token-klistring.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            idp = user_input.get("idp", IDP_STOCKHOLM_PAR)
            try:
                pupils = await _validate_bankid(self.hass, idp, self.async_show_progress)
            except BankIDDenied:
                errors["base"] = "bankid_denied"
            except BankIDTimeout:
                errors["base"] = "bankid_timeout"
            except (BankIDPageChanged, BankIDError):
                errors["base"] = "bankid_page_changed"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Oväntat fel vid BankID-inloggning")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(f"bankid-{idp}")
                self._abort_if_unique_id_configured()
                title = "InfoMentor (BankID)"
                if pupils:
                    title = f"InfoMentor ({len(pupils)} barn) (BankID)"
                return self.async_create_entry(
                    title=title,
                    data={
                        CONF_AUTH_MODE: "bankid",
                        "idp": idp,
                    },
                )
        return self.async_show_form(
            step_id="bankid",
            data_schema=vol.Schema(
                {
                    vol.Required("idp", default=IDP_STOCKHOLM_PAR): selector.TextSelector(),
                }
            ),
            errors=errors,
        )

    async def async_step_password(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            try:
                pupils = await _validate_credentials(self.hass, username, user_input[CONF_PASSWORD])
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Oväntat fel vid inloggning")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(username.lower())
                self._abort_if_unique_id_configured()
                title = "InfoMentor"
                if pupils:
                    title = f"InfoMentor ({len(pupils)} barn)"
                return self.async_create_entry(
                    title=title,
                    data={
                        CONF_USERNAME: username,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_AUTH_MODE: "password",
                    },
                )
        return self.async_show_form(step_id="password", data_schema=STEP_USER_SCHEMA, errors=errors)

    async def async_migrate_entry(self, entry: ConfigEntry) -> bool:
        """Migrera från v1 (användarnamn/lösenord) till v2 (hållbar token).

        Vi tar bort lösenordet och sätter auth_mode=token; utan en sparad token
        gör coordinatoren ConfigEntryAuthFailed -> om-auth, där användaren klistrar
        in en frisk app-token.
        """
        if entry.version >= 2:
            return True
        _LOGGER.debug("Migrerar %s från version %s till 2", entry.title, entry.version)
        new_data = dict(entry.data)
        new_data.pop(CONF_PASSWORD, None)
        new_data.pop(CONF_USERNAME, None)
        new_data[CONF_AUTH_MODE] = "token"
        self.hass.config_entries.async_update_entry(entry, version=2, data=new_data)
        return True

    async def async_step_user_token(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Inloggning med en pastad refresh-token (BankID-givna)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            refresh_token = user_input[CONF_REFRESH_TOKEN].strip()
            try:
                pupils, rotated = await _validate_refresh_token(self.hass, refresh_token)
            except RevokedAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Oväntat fel vid token-inloggning")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(refresh_token[:12])
                self._abort_if_unique_id_configured()
                title = "InfoMentor"
                if pupils:
                    title = f"InfoMentor ({len(pupils)} barn)"
                return self.async_create_entry(
                    title=title,
                    data={
                        CONF_REFRESH_TOKEN: rotated,
                        CONF_AUTH_MODE: "token",
                    },
                )
        return self.async_show_form(
            step_id="user_token", data_schema=STEP_TOKEN_SCHEMA, errors=errors
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Starta om-auth när uppgifterna slutat gälla."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        auth_mode = entry.data.get(CONF_AUTH_MODE, "password")
        if user_input is not None:
            data_updates = dict(user_input)
            try:
                if auth_mode == "token":
                    refresh_token = user_input[CONF_REFRESH_TOKEN].strip()
                    _pupils, rotated = await _validate_refresh_token(self.hass, refresh_token)
                    data_updates[CONF_REFRESH_TOKEN] = rotated
                else:
                    username = user_input[CONF_USERNAME].strip()
                    await _validate_credentials(self.hass, username, user_input[CONF_PASSWORD])
            except (InvalidAuth, RevokedAuth):
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Oväntat fel vid om-auth")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates=data_updates,
                )
        if auth_mode == "token":
            return self.async_show_form(
                step_id="reauth_token",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_REFRESH_TOKEN): selector.TextSelector(
                            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                        ),
                    }
                ),
                errors=errors,
            )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=entry.data.get(CONF_USERNAME, "")): str,
                    vol.Required(CONF_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return InfomentorOptionsFlow()


class InfomentorOptionsFlow(OptionsFlow):
    """Inställningar: intervall, skolmat och smeknamn."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MIN),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL_MIN,
                        max=MAX_SCAN_INTERVAL_MIN,
                        step=1,
                        unit_of_measurement="min",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_ENABLE_LUNCH, default=options.get(CONF_ENABLE_LUNCH, False)
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_MATEO_UNIT, default=options.get(CONF_MATEO_UNIT, "")
                ): selector.TextSelector(),
                vol.Optional(
                    CONF_NAMES, default=options.get(CONF_NAMES, "")
                ): selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

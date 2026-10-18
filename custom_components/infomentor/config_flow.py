"""Config- och optionsflöde för InfoMentor."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .api import (
    CannotConnect,
    InfomentorApi,
    InvalidAuth,
    RevokedAuth,
)
from .bankid_session import BankIdLogin
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
    hass, idp: str, progress
) -> tuple[list[dict[str, Any]], str]:
    """Logga in med interaktiv BankID via driver:n.

    Kör driver:n med en progress-callback som uppdaterar UI:t.
    Använder en egen session som stängs efteråt. Kastar BankIDError-typer,
    InvalidAuth (bridgen avvisades) eller CannotConnect (nere).
    """
    session = await _make_session()
    try:
        api = InfomentorApi(session)
        return await api.async_login_with_bankid(idp=idp, progress=progress)
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

    def __init__(self) -> None:
        """Initiera flödet med BankID-bakgrundsuppgift."""
        super().__init__()
        self._bankid: BankIdLogin | None = None
        self._bankid_error = "unknown"

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
        """Interaktiv BankID-inloggning (Stockholm Stad) som ett progress-steg.

        Driver:n körs i bakgrunden (`BankIdLogin`). Varje ny QR-bild gör den lilla
        väntan klar, HA kör steget igen, och steget svarar med en ny väntan och den nya
        QR-koden; det är så en roterande QR kan visas i en dialog. Endast Stockholm
        Stad stöds idag; andra skolor får token-klistring.
        """
        if self._bankid is None:
            self._bankid = BankIdLogin(
                self.hass,
                lambda progress: _validate_bankid(self.hass, IDP_STOCKHOLM_PAR, progress),
            )
        login = self._bankid
        if login.done:
            return self.async_show_progress_done(next_step_id="bankid_done")
        return self.async_show_progress(
            step_id="bankid",
            progress_action=login.progress_action,
            progress_task=login.wait_for_change(),
            description_placeholders=login.placeholders(),
        )

    async def async_step_bankid_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Slutsteget: skapa posten, eller visa felet och låt användaren försöka igen.

        BankID är bara sättet att få appens refresh-token (webbsessionen räcker ~40 min),
        så posten sparas som `token`, det flöde koordinatorn och reauth redan kan.
        """
        login, self._bankid = self._bankid, None
        if login is None or login.error or login.outcome is None:
            self._bankid_error = (login.error if login else None) or "unknown"
            return await self.async_step_bankid_retry()
        pupils, refresh_token = login.outcome
        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(),
                data_updates={CONF_REFRESH_TOKEN: refresh_token, CONF_AUTH_MODE: "token"},
            )
        await self.async_set_unique_id(refresh_token[:12])
        self._abort_if_unique_id_configured()
        title = f"InfoMentor ({len(pupils)} barn)" if pupils else "InfoMentor"
        return self.async_create_entry(
            title=title,
            data={CONF_REFRESH_TOKEN: refresh_token, CONF_AUTH_MODE: "token"},
        )

    async def async_step_bankid_retry(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Visar felet. Först när användaren trycker Skicka startas en ny BankID-order.

        HA kan rendera om ett formsteg med user_input=None; det får inte starta en order.
        """
        if user_input is not None:
            return await self.async_step_bankid()
        return self.async_show_form(
            step_id="bankid_retry",
            data_schema=vol.Schema({}),
            errors={"base": self._bankid_error},
        )

    @callback
    def async_remove(self) -> None:
        """Avbryt BankID-inloggningen om dialogen stängs. HA avbryter bara den korta
        väntan, inte driver:n, så den måste stoppas här."""
        if self._bankid is not None:
            self._bankid.cancel()

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
        if auth_mode == "bankid":
            self._bankid = None
            return await self.async_step_bankid()
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

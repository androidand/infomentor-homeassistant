"""Config- och optionsflöde för InfoMentor."""

from __future__ import annotations

import asyncio
import contextlib
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
from .bankid import (
    BankIDDenied,
    BankIDError,
    BankIDPageChanged,
    BankIDTimeout,
)
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
) -> list[dict[str, Any]]:
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
        self._bankid_task: asyncio.Task | None = None
        self._bankid_idp: str = IDP_STOCKHOLM_PAR
        self._bankid_state: dict[str, Any] = {}

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

        Steg 1: användaren väljer IDP.
        Steg 2 (bankid_qr): visar QR-kod (QrCodeSelector) + autostart-länk.
        En bakgrundsuppgift pollar driver:n; när BankID godkänner skapas entry:t.

        Endast Stockholm Stad (SiteMinder) stöds idag; andra skolor får token-klistring.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            idp = user_input.get("idp", IDP_STOCKHOLM_PAR)
            self._bankid_idp = idp
            return await self.async_step_bankid_qr()
        return self.async_show_form(
            step_id="bankid",
            data_schema=vol.Schema(
                {
                    vol.Required("idp", default=IDP_STOCKHOLM_PAR): selector.TextSelector(),
                }
            ),
            errors=errors,
        )

    async def async_step_bankid_qr(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Visar QR-kod + autostart-länk och pollar BankID i bakgrunden.

        Kör driver:n som en bakgrundsuppgift. QR-koden visas via QrCodeSelector.
        När BankID godkänner skapas entry:t; vid fel visas felmeddelande.
        """
        from .bankid import BankIDStep

        idp = self._bankid_idp
        state = self._bankid_state

        # Första anropet: initiera tillstånd och starta driver:n.
        if not state:
            state.update(
                {
                    "qr_data": None,
                    "autostart_url": None,
                    "message": "Startar BankID…",
                    "done": False,
                    "pupils": None,
                    "error": None,
                }
            )

            async def _progress(step: BankIDStep) -> None:
                state["message"] = step.message
                if step.qr_data:
                    state["qr_data"] = step.qr_data
                if step.autostart_url:
                    state["autostart_url"] = step.autostart_url
                if step.state == "OK":
                    state["done"] = True

            async def _run_driver() -> None:
                try:
                    pupils = await _validate_bankid(self.hass, idp, _progress)
                    state["pupils"] = pupils
                except BankIDDenied:
                    state["error"] = "bankid_denied"
                except BankIDTimeout:
                    state["error"] = "bankid_timeout"
                except (BankIDPageChanged, BankIDError):
                    state["error"] = "bankid_page_changed"
                except InvalidAuth:
                    state["error"] = "invalid_auth"
                except CannotConnect:
                    state["error"] = "cannot_connect"
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Oväntat fel vid BankID-inloggning")
                    state["error"] = "unknown"
                finally:
                    state["done"] = True

            self._bankid_task = self.hass.async_create_task(_run_driver())

        task = self._bankid_task

        # Bygg beskrivningstext.
        description = state["message"]
        if state["autostart_url"]:
            description += f"\n\nAutostart: {state['autostart_url']}"

        # Användaren har skickat in formuläret — kontrollera driver:n.
        if user_input is not None and state["done"]:
            if state["error"] and task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            if state["error"]:
                return self.async_show_form(
                    step_id="bankid",
                    data_schema=vol.Schema(
                        {
                            vol.Required("idp", default=idp): selector.TextSelector(),
                        }
                    ),
                    errors={"base": state["error"]},
                )
                pupils = state["pupils"]
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

        # Visa formen med QR-kod (om tillgänglig) + status.
        if state["qr_data"]:
            schema: vol.Schema = vol.Schema(
                {
                    vol.Required("qr"): selector.QrCodeSelector(
                        selector.QrCodeSelectorConfig(data=state["qr_data"])
                    ),
                }
            )
        else:
            schema = vol.Schema({})

        return self.async_show_form(
            step_id="bankid_qr",
            data_schema=schema,
            description_placeholders={"message": description},
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
                elif auth_mode == "bankid":
                    from .bankid import BankIDStep

                    async def _reauth_progress(step: BankIDStep) -> None:
                        description = step.message
                        parts = []
                        if step.autostart_url:
                            parts.append(f"Autostart: {step.autostart_url}")
                        if description:
                            parts.append(description)
                        await self.async_show_progress(
                            progress_action=step.action,
                            progress_task=step.task,
                            description="\n".join(p for p in parts if p),
                            item_number=0,
                        )

                    _pupils = await _validate_bankid(
                        self.hass,
                        user_input.get("idp", IDP_STOCKHOLM_PAR),
                        _reauth_progress,
                    )
                else:
                    username = user_input[CONF_USERNAME].strip()
                    await _validate_credentials(self.hass, username, user_input[CONF_PASSWORD])
            except (InvalidAuth, RevokedAuth):
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except BankIDError as err:
                errors["base"] = str(err)
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
            return self.async_show_form(
                step_id="bankid",
                data_schema=vol.Schema(
                    {
                        vol.Required("idp", default=entry.data.get("idp", IDP_STOCKHOLM_PAR)): str,
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

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


async def test_migrate_v1_keeps_the_password_login_working(hass: HomeAssistant) -> None:
    """HA kör migreringen i __init__.py. En v1-post med lösenord måste behålla sina
    uppgifter och få auth_mode=password, annars slutar en fungerande inloggning gälla."""
    from custom_components.infomentor import async_migrate_entry

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

    assert await async_migrate_entry(hass, entry) is True
    assert entry.version == 2
    assert entry.data["username"] == "user@example.com"
    assert entry.data["password"] == "hemligt"
    assert entry.data["auth_mode"] == "password"


def test_config_flow_has_no_unused_migration() -> None:
    """ConfigFlow.async_migrate_entry anropas aldrig av HA men raderade lösenordet.
    Den låg kvar som en fälla för den som kopplar in den; migreringen hör hemma i
    __init__.py."""
    assert not hasattr(InfomentorConfigFlow, "async_migrate_entry")


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


async def _settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


async def test_bankid_progress_shows_a_refreshing_qr_and_the_same_device_link(
    hass: HomeAssistant, bankid_validate
) -> None:
    """Varje ny QR-bild ritar om steget (ny väntan + nya placeholders); sist skapas posten."""
    from custom_components.infomentor.bankid import BankIDStep

    frames: asyncio.Queue = asyncio.Queue()

    async def fake_validate(hass, idp, progress):
        while (qr := await frames.get()) is not None:
            await progress(BankIDStep("Skanna", qr_data=qr, autostart_token="tok1"))
        return [{"id": "1"}], "rt-new"

    bankid_validate["fn"] = fake_validate
    result = await _start_bankid(hass)
    flow_id = result["flow_id"]
    assert result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "bankid_starting"

    # Frontend ritar om steget först när HA skickar denna händelse.
    refetch_events = []
    hass.bus.async_listen("data_entry_flow_progressed", refetch_events.append)

    seen = []
    for qr in ("bankid.a.0.h1", "bankid.a.1.h2", "bankid.a.2.h3"):
        await frames.put(qr)
        await _settle()
        # Samma anrop som frontend gör när den får händelsen.
        step = await hass.config_entries.flow.async_configure(flow_id)
        assert step["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS
        assert step["progress_action"] == "bankid"
        seen.append(step["description_placeholders"])

    from custom_components.infomentor.qr import qr_markdown_image

    assert [p["qr_code"] for p in seen] == [
        qr_markdown_image(qr) for qr in ("bankid.a.0.h1", "bankid.a.1.h2", "bankid.a.2.h3")
    ]
    assert len(refetch_events) >= 3
    assert seen[0]["autostart_link"] == "https://app.bankid.com/?autostarttoken=tok1&redirect=null"

    await frames.put(None)
    await _finish_bankid(hass, flow_id)
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


async def test_bankid_login_is_cancelled_when_the_dialog_is_closed(
    hass: HomeAssistant, bankid_validate
) -> None:
    """HA avbryter bara den korta väntan; driver:n måste stoppas av flödet själv."""
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
    await _settle()
    assert cancelled.is_set()


def test_unsafe_qr_data_and_tokens_never_reach_the_description() -> None:
    """qrData och token hamnar i ett HTML-attribut och en URL i markdown."""
    from unittest.mock import MagicMock

    from custom_components.infomentor.bankid import BankIDStep
    from custom_components.infomentor.bankid_session import BankIdLogin

    hass = MagicMock()
    hass.async_create_task = lambda coro: (coro.close(), MagicMock())[1]
    login = BankIdLogin(hass, lambda progress: None)
    asyncio.run(login._progress(BankIDStep("x", qr_data='a"><script>', autostart_token="t&x=1")))
    assert login.placeholders() == {"message": "x", "qr_code": "", "autostart_link": ""}
    assert login.progress_action == "bankid_starting"


async def test_one_pending_wait_is_shared_until_something_changes() -> None:
    """Varje omhämtning av steget anropar wait_for_change. Skulle den skapa en ny väntan
    varje gång läcker de gamla, och varje färdig väntan kör flödet en gång till."""
    from unittest.mock import MagicMock

    from custom_components.infomentor.bankid import BankIDStep
    from custom_components.infomentor.bankid_session import BankIdLogin

    hass = MagicMock()
    hass.async_create_task = asyncio.ensure_future
    release = asyncio.Event()

    async def run(progress):
        await release.wait()

    login = BankIdLogin(hass, run)
    first = login.wait_for_change()
    assert login.wait_for_change() is first

    await login._progress(BankIDStep("x", qr_data="q1", autostart_token="t"))
    await first
    second = login.wait_for_change()
    assert second is not first
    login.cancel()


def test_translation_strings_contain_no_html_tags() -> None:
    """HA:s frontend tolkar <tagg> i en översättning som ICU-tagg och visar
    "Translation error: INVALID_TAG" (sett i en riktig HA 2026.2). HTML hör hemma i
    placeholder-värden."""
    import json
    from pathlib import Path

    root = Path(__file__).parent.parent / "custom_components" / "infomentor"
    for name in ("strings.json", "translations/en.json", "translations/sv.json"):
        text = json.dumps(json.loads((root / name).read_text()))
        assert "<" not in text and ">" not in text, name


@pytest.mark.parametrize(
    ("raised", "key"),
    [
        ("BankIDDenied", "bankid_denied"),
        ("BankIDTimeout", "bankid_timeout"),
        ("BankIDPageChanged", "bankid_page_changed"),
        ("BankIDError", "bankid_failed"),
    ],
)
async def test_each_bankid_failure_gets_its_own_message(raised: str, key: str) -> None:
    """Ett sidfel och en oskannad order får inte visa samma text."""
    from unittest.mock import MagicMock

    from custom_components.infomentor import bankid
    from custom_components.infomentor.bankid_session import BankIdLogin

    hass = MagicMock()
    hass.async_create_task = asyncio.ensure_future

    async def run(progress):
        raise getattr(bankid, raised)("x")

    login = BankIdLogin(hass, run)
    await login._task
    assert login.error == key


async def test_the_failure_detail_says_what_the_order_did_and_how_it_ended() -> None:
    """Utan detaljen går det inte att skilja "ingen skannade" från "skannade men fick fel"."""
    from unittest.mock import MagicMock

    from custom_components.infomentor.bankid import BankIDError, BankIDStep
    from custom_components.infomentor.bankid_session import BankIdLogin

    hass = MagicMock()
    hass.async_create_task = asyncio.ensure_future

    async def run(progress):
        await progress(BankIDStep("a", qr_data="q1", state="PENDING"))
        await progress(BankIDStep("a", qr_data="q2", state="PENDING"))
        await progress(BankIDStep("b", qr_data="q3", state="USER_SIGN"))
        raise BankIDError("BankID slutade i läget ERROR efter https://x.example/a?b=secret")

    login = BankIdLogin(hass, run)
    await login._task
    assert login.error == "bankid_failed"
    assert login.detail == "Sett: PENDING → USER_SIGN. Slut: BankIDError: BankID slutade i läget ERROR efter <url>"


async def test_the_retry_form_shows_the_detail(hass: HomeAssistant, bankid_validate) -> None:
    from custom_components.infomentor.bankid import BankIDError

    async def fake_validate(hass, idp, progress):
        raise BankIDError("slut")

    bankid_validate["fn"] = fake_validate
    result = await _start_bankid(hass)
    result = await _finish_bankid(hass, result["flow_id"])
    assert result["step_id"] == "bankid_retry"
    assert result["description_placeholders"]["detail"] == "Sett: inget. Slut: BankIDError: slut"

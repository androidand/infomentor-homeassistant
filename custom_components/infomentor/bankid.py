"""BankID-inloggning för InfoMentor över vanlig HTTP (Stockholm Stad).

Kedjan (verifierad live; se infomentor/docs/auth-research.md i androidand/api-clients,
"The BankID layer"): sso.infomentor.se/login.ashx, SiteMinder, metodvalets länk, BankID-sidan
(vars URL med SiteMinders query bär sessionen), `initialize=bankid` ger {token, order, qrData},
`verifyorder` pollas tills `OK`, GET sidans URL ger SAML-intyget, `post_saml_response`
postar det till login.ashx och landar i hubben.

Det här är Stockholm Stads sidor, inte BankIDs eget API: en ändrad sida ska felklassas
tydligt så att användaren kan falla tillbaka på token-klistring.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

from .const import IDP_STOCKHOLM_PAR, SSO_LOGIN_PATH
from .token_client import TokenError, submit_oauth_token
from .util import hidden_inputs
from .web import CookieJar, fetch, follow, form_action, where

_LOGGER = logging.getLogger(__name__)

SSO_BASE = "https://sso.infomentor.se"
MAX_POLL_SECONDS = 180.0
POLL_INTERVAL_SECONDS = 1.0  # QR-koden byter varje sekund; en äldre bild hinner inte skannas
MAX_RESTARTS = 3  # en oskannad QR-order dör efter ca 30 s
WAITING_STATES = frozenset({"PENDING", "STARTED", "USER_SIGN"})
BANKID_LINK_RE = re.compile(
    r"href=[\"']([^\"']*/NECSadc/mbid/b64startpage\.jsp\?startpage=[^\"']*)[\"']", re.IGNORECASE
)
NECSADCMBID_RE = re.compile(r"NECSadcmbid/authenticate/NECSadcmbid", re.IGNORECASE)
_JSON_XHR = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}


class BankIDError(Exception):
    """Basfel för BankID-inloggningen."""


class BankIDTimeout(BankIDError):
    """Tiden gick ut innan BankID-appen godkännde. Be användaren försöka igen."""


class BankIDDenied(BankIDError):
    """Användaren avbröt BankID (avböjde) i stället för att godkänna."""


class BankIDPageChanged(BankIDError):
    """En IdP-sida såg annorlunda ut än förväntat. Felaklassa så användaren
    kan falla tillbaka på token-klistring."""


@dataclass
class BankIDStep:
    """En framskridande step att visa i config-flödet."""

    message: str
    qr_data: str | None = None
    autostart_url: str | None = None
    autostart_token: str | None = None
    state: str = "PENDING"
    action: str = "bankid"  # progress_action
    task: str = "bankid"  # progress_task (HA >= 2024.8 kräver detta)


@dataclass
class BankIDResult:
    """Resultatet av BankID-inloggningen."""

    saml_response: str | None = None  # när vi körde interaktivt
    jar: CookieJar | None = None  # cookies från IdP-hoppen


class BankIDDriver:
    """Kör Stockholm Stads BankID-inloggning (QR) och postar intyget till InfoMentor.

    `do_request(method, url, body, headers)` ger ett aiohttp-svar och följer inga omdirigeringar;
    `fetch` och `follow` (web.py) gör det och samlar cookies i en jar över hela kedjan.
    """

    def __init__(self, *, poll_interval: float = POLL_INTERVAL_SECONDS) -> None:
        self._poll_interval = poll_interval

    async def _open(self, do_request: Any, jar: CookieJar, idp: str) -> str:
        """Till BankID-sidan; returnerar dess URL med SiteMinders query, byte för byte."""
        chooser = await follow(
            do_request, jar, f"{SSO_BASE}{SSO_LOGIN_PATH}?idp={idp}", error=BankIDPageChanged
        )
        link = BANKID_LINK_RE.search(chooser.text)
        if chooser.status != 200 or not link:
            raise BankIDPageChanged(
                f"metodvalet svarade {chooser.status} utan BankID-länk: sidan ser annorlunda ut"
            )
        href = link.group(1).replace("&amp;", "&")
        page = await follow(do_request, jar, urljoin(chooser.url, href), error=BankIDPageChanged)
        if page.status != 200 or not NECSADCMBID_RE.search(page.url):
            raise BankIDPageChanged(
                f"väntade NECSadcmbid-sidan, hamnade på {page.status} {where(page.url)}"
            )
        return page.url

    async def _get_json(
        self, do_request: Any, jar: CookieJar, auth_url: str, param: str, what: str
    ) -> dict[str, Any]:
        sep = "&" if "?" in auth_url else "?"
        url = f"{auth_url}{sep}{param}&_={int(time.time() * 1000)}"
        page = await fetch(do_request, jar, "GET", url, headers={**_JSON_XHR, "Referer": auth_url})
        # SERVER QUIRK: PhenixID svarar på verifyorder med JSON märkt text/html (initialize
        # säger application/json); sidans egen kod gör JSON.parse på texten. Content-type
        # är därför ingen signal, bara en kropp som blir ett objekt. Status måste vara 200:
        # efter OK svarar servern 302 och efter CANCELLED 500, aldrig "godkänt".
        try:
            data = json.loads(page.text.strip())
        except ValueError:
            data = None
        if page.status != 200 or not isinstance(data, dict):
            raise BankIDPageChanged(f"{what}: status {page.status}, ingen JSON")
        return data

    async def _initialize(
        self, do_request: Any, jar: CookieJar, auth_url: str
    ) -> tuple[str, str, str]:
        data = await self._get_json(do_request, jar, auth_url, "initialize=bankid", "initialize")
        order, token, qr = data.get("order"), data.get("token"), data.get("qrData")
        if not order or not token or not isinstance(qr, str):
            raise BankIDPageChanged("initialize gav ingen order/token/qrData")
        return str(order), str(token), qr

    async def run(
        self,
        do_request: Any,
        *,
        idp: str = IDP_STOCKHOLM_PAR,
        progress: Any | None = None,
        poll_interval: float | None = None,
        max_restarts: int = MAX_RESTARTS,
        timeout: float = MAX_POLL_SECONDS,
    ) -> BankIDResult:
        """Kör kedjan till SAMLResponse. Kastar BankIDDenied/-Timeout/-PageChanged/-Error."""
        interval = self._poll_interval if poll_interval is None else poll_interval
        jar = CookieJar()
        auth_url = await self._open(do_request, jar, idp)
        order, token, qr_data = await self._initialize(do_request, jar, auth_url)
        autostart = f"bankid:///?autostarttoken={token}&redirect=null"

        deadline = time.monotonic() + timeout
        restarts = 0
        scanned = False
        state = "PENDING"
        last = ""
        last_logged = ""
        while True:
            key = f"{state}|{qr_data}"
            if key != last and state in WAITING_STATES:
                last = key
                if progress:
                    await progress(
                        BankIDStep(
                            "Skanna QR-koden i BankID-appen",
                            qr_data=qr_data,
                            autostart_url=autostart,
                            autostart_token=token,
                            state=state,
                        )
                    )
            if time.monotonic() > deadline:
                raise BankIDTimeout(f"Tiden gick ut efter {int(timeout)} s utan godkännande")
            await asyncio.sleep(interval)
            answer = await self._get_json(
                do_request, jar, auth_url, f"verifyorder={order}", "verifyorder"
            )
            state = answer.get("state")
            if not isinstance(state, str):
                raise BankIDPageChanged("verifyorder gav inget state")
            qr_data = answer.get("qrData") or qr_data
            if state not in ("PENDING", "ERROR"):
                scanned = True
            if state != last_logged:
                _LOGGER.debug("BankID-order %s: %s", order[:8], state)
                last_logged = state
            if state == "ERROR" and not scanned and restarts >= max_restarts:
                # Ingen skannade någon av ordrarna: det är en tidsgräns, inte ett sidfel.
                raise BankIDTimeout(
                    f"ingen skannade QR-koden i {restarts + 1} order (var och en ca 30 s)"
                )
            if state == "ERROR" and not scanned and restarts < max_restarts:
                # En ickeskannad QR-order dör efter ca 30 s med ERROR: starta en ny.
                restarts += 1
                _LOGGER.debug(
                    "BankID-ordern gick ut oskannad, startar en ny (%d/%d)", restarts, max_restarts
                )
                order, token, qr_data = await self._initialize(do_request, jar, auth_url)
                autostart = f"bankid:///?autostarttoken={token}&redirect=null"
                state = "PENDING"
                continue
            if state == "OK":
                break
            if state == "CANCELLED":
                raise BankIDDenied("BankID-appen avbröt inloggningen")
            if state not in WAITING_STATES:
                raise BankIDError(f"BankID slutade i läget {state}")

        if progress:
            await progress(BankIDStep("Godkänt — slutför inloggning…", state="OK"))
        return await self._collect_assertion(do_request, jar, auth_url)

    async def _collect_assertion(
        self, do_request: Any, jar: CookieJar, auth_url: str
    ) -> BankIDResult:
        """Efter OK: GET sidans URL, 302 till SiteMinderAuthADC (SMSESSION), sedan en
        saml2sso-sida med en auto-post-form till sso.infomentor.se."""
        page = await follow(do_request, jar, auth_url, error=BankIDPageChanged)
        saml = hidden_inputs(page.text).get("SAMLResponse")
        action = form_action(page.text, page.url)
        if page.status != 200 or not saml or not action:
            raise BankIDPageChanged(
                f"ingen SAMLResponse-form efter godkännande ({page.status} {where(page.url)})"
            )
        if urlsplit(action).hostname != "sso.infomentor.se":
            # Bara parten som litar på intyget får ta emot det.
            raise BankIDPageChanged(f"SAMLResponse-formen pekar på {urlsplit(action).hostname}")
        return BankIDResult(saml_response=saml, jar=jar)

    async def post_saml_response(
        self,
        do_request: Any,
        *,
        saml_response: str,
        jar: CookieJar | None = None,
        idp: str = IDP_STOCKHOLM_PAR,
    ) -> CookieJar:
        """Posta intyget till login.ashx och följ till hubbens request-token-sida och IMHome.

        `jar` är den som `run` fyllde: login.ashx behöver cookies från det första hoppet.
        """
        jar = jar if jar is not None else CookieJar()
        post_url = f"{SSO_BASE}{SSO_LOGIN_PATH}?idp={idp}"
        page = await fetch(
            do_request,
            jar,
            "POST",
            post_url,
            body=urllib.parse.urlencode({"SAMLResponse": saml_response}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": SSO_BASE},
        )
        location = page.headers.get("location")
        if not location:
            raise TokenError(f"POST SAMLResponse till login.ashx svarade {page.status}")
        page = await follow(do_request, jar, urljoin(post_url, location), error=TokenError)
        if page.status != 200 or "oauth_token" not in page.text:
            raise TokenError(f"nådde inte request-token-sidan (status {page.status})")
        return await submit_oauth_token(do_request, jar, page.url)

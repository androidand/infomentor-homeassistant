"""BankID-inloggning för InfoMentor över vanlig HTTP (Stockholm Stad).

Kedjan (verifierad live, se infomentor/docs/auth-research.md i
androidand/api-clients, avsnittet "The BankID layer"):

    sso.infomentor.se/login.ashx?idp=stockholm_par
      -> SiteMinder saml2sso -> metodvalet (länk till /NECSadc/mbid/b64startpage.jsp)
      -> login003 NECSadcmbid (BankID-sidan; dess URL med SiteMinders query bär sessionen)
      -> initialize=bankid  -> {token, order, qrData}
      -> verifyorder=<order> pollas ~2 s -> {state, qrData?}
      -> efter OK: GET sidans URL -> 302 SiteMinderAuthADC (SMSESSION) -> saml2sso
         med en auto-post-form (SAMLResponse) till sso.infomentor.se
      -> `post_saml_response`: POST -> Login -> Default.aspx -> hubben -> IMHome

Det här är Stockholm Stads sidor, inte BankIDs eget RP-API. Det måste vara
användarstyrt och lågfrekvent, och en ändrad sida ska felklassas tydligt så
att användaren kan falla tillbaka på token-klistring.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

from yarl import URL

from .const import IDP_STOCKHOLM_PAR, SSO_LOGIN_PATH
from .token_client import CookieJar

_LOGGER = logging.getLogger(__name__)

PHENIXID_BASE = "https://login003.stockholm.se"
SITEMINDER_BASE = "https://login001.stockholm.se"
SSO_BASE = "https://sso.infomentor.se"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)

# verifyorder-pollens gränser. BankID-appen svarar inte omedelbart; vi kan
# inte försöka automatiskt i oändligt (det skulle spä på IdP:s sida), så vi
# stoppar efter en tid och ber användaren om hjälp.
MAX_POLL_SECONDS = 180.0
POLL_INTERVAL_SECONDS = 1.0  # QR-koden byter varje sekund; en äldre bild hinner inte skannas
MAX_HOPS = 20
MAX_RESTARTS = 3  # en ickeskannad QR-order dör efter ca 30 s
WAITING_STATES = frozenset({"PENDING", "STARTED", "USER_SIGN"})
BANKID_LINK_RE = re.compile(
    r"href=[\"']([^\"']*/NECSadc/mbid/b64startpage\.jsp\?startpage=[^\"']*)[\"']", re.IGNORECASE
)
NECSADCMBID_RE = re.compile(r"NECSadcmbid/authenticate/NECSadcmbid", re.IGNORECASE)

# Feltyper — ärsta så config-flödet kan visa rätt meddelande.
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
    """Kör Stockholm Stads BankID-inloggning (QR) och returnerar SAMLResponse.

    `do_request` är async callable(method, url, body, headers) -> aiohttp:svar,
    bundet till en session i api.py. Inga omdirigeringar följs av do_request;
    driver:n följer dem själv så att varje hopps cookies samlas i en jar.
    Kedjan och dess fallgropar: se infomentor/docs/auth-research.md i
    androidand/api-clients, avsnittet "The BankID layer".
    """

    def __init__(self, *, poll_interval: float = POLL_INTERVAL_SECONDS) -> None:
        self._poll_interval = poll_interval

    # ------------------------------------------------------------------ HTTP
    @staticmethod
    def _headers(cookie: str = "", **extra: str) -> dict[str, str]:
        out = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
        }
        if cookie:
            out["Cookie"] = cookie
        out.update(extra)
        return out

    async def _send(
        self,
        do_request: Any,
        jar: CookieJar,
        method: str,
        url: str,
        *,
        body: str | None = None,
        extra: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], str]:
        """Ett anrop. Cookies ackumuleras i `jar` (aldrig ersätts, annars tappas
        SiteMinders sessionscookies mellan hoppen)."""
        headers = self._headers(jar.cookie_header(url), **(extra or {}))
        # encoded=True: aiohttp/yarl annars normaliserar t.ex. %2D till "-" i SiteMinders
        # query, och den ska gå iväg byte för byte som servern skrev den.
        response = await do_request(method, URL(url, encoded=True), body, headers)
        try:
            status = response.status
            resp_headers = {k.lower(): v for k, v in response.headers.items()}
            text = await response.text()
        finally:
            response.release()
        for chunk in response.headers.getall("Set-Cookie", []):
            jar.set_from_header(chunk, url)
        return status, resp_headers, text

    async def _follow(
        self, do_request: Any, jar: CookieJar, url: str, step: str
    ) -> tuple[int, str, str]:
        """GET url och följ 3xx som GET. Returnerar (status, slutlig url, body)."""
        for _ in range(MAX_HOPS):
            status, headers, text = await self._send(do_request, jar, "GET", url)
            if not 300 <= status < 400:
                return status, url, text
            location = headers.get("location")
            if not location:
                raise BankIDPageChanged(f"{step}: {status} utan Location vid {url}")
            url = urljoin(url, location)
        raise BankIDPageChanged(f"{step}: fler än {MAX_HOPS} omdirigeringar")

    # ------------------------------------------------------------------ kedjan
    async def _open(self, do_request: Any, jar: CookieJar, idp: str) -> str:
        """Steg 1-2: in i SiteMinder, välj BankID, landa på BankID-sidan.

        Returnerar sidans URL inklusive SiteMinders query. Den ska behållas
        byte för byte: varje API-anrop är denna URL plus en parameter.
        """
        start = f"{SSO_BASE}{SSO_LOGIN_PATH}?idp={idp}"
        status, url, html = await self._follow(do_request, jar, start, "login-start")
        if status != 200:
            raise BankIDPageChanged(f"metodvalet svarade {status}")
        link = BANKID_LINK_RE.search(html)
        if not link:
            raise BankIDPageChanged("metodvalet har ingen BankID-länk — sidan ser annorlunda ut")
        href = link.group(1).replace("&amp;", "&")
        status, page_url, _ = await self._follow(
            do_request, jar, urljoin(url, href), "bankid-sida"
        )
        if status != 200 or not NECSADCMBID_RE.search(page_url):
            raise BankIDPageChanged(f"väntade NECSadcmbid-sidan, hamnade på {status} {page_url}")
        return page_url

    @staticmethod
    def _api_url(auth_url: str, param: str) -> str:
        sep = "&" if "?" in auth_url else "?"
        return f"{auth_url}{sep}{param}&_={int(time.time() * 1000)}"

    async def _get_json(
        self, do_request: Any, jar: CookieJar, auth_url: str, param: str, what: str
    ) -> dict[str, Any]:
        url = self._api_url(auth_url, param)
        status, _, text = await self._send(
            do_request,
            jar,
            "GET",
            url,
            extra={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": auth_url,
            },
        )
        # SERVER QUIRK: PhenixID svarar på verifyorder med JSON märkt text/html
        # (initialize säger korrekt application/json). Sidans egen kod gör
        # JSON.parse på texten. Vi litar därför inte på content-type; bara en
        # kropp som tolkas till ett objekt räknas. Status måste vara 200: efter
        # OK svarar servern 302 och efter CANCELLED 500, aldrig "godkänt".
        data = self._as_json(text)
        if status != 200 or not isinstance(data, dict):
            raise BankIDPageChanged(f"{what}: status {status}, ingen JSON")
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
            await self._sleep(interval)
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
        """Efter OK: GET sidans URL utan extra parameter -> 302 SiteMinderAuthADC
        (sätter SMSESSION) -> saml2sso 200 med auto-post-form till sso.infomentor.se."""
        status, url, html = await self._follow(do_request, jar, auth_url, "assertion")
        saml_response = self._extract_saml_response_form(html, url)
        if status != 200 or saml_response is None:
            raise BankIDPageChanged(f"ingen SAMLResponse-form efter godkännande ({status} {url})")
        action = re.search(r'<form\b[^>]*\baction=["\']([^"\']*)["\']', html, re.IGNORECASE)
        host = urlsplit(urljoin(url, action.group(1))).hostname if action else None
        if host != "sso.infomentor.se":
            # Bara parten som litar på intyget får ta emot det.
            raise BankIDPageChanged(
                f"SAMLResponse-formen pekar på {host}, väntade sso.infomentor.se"
            )
        return BankIDResult(saml_response=saml_response, jar=jar)

    @staticmethod
    def _urlencode_saml_response(saml_response: str) -> str:
        """Kodar SAMLResponse som application/x-www-form-urlencoded.

        En Base64-kodad SAMLResponse kan innehålla +, / och = — dessa måste
        percent-erkodas annars tolkas kroppen fel av mottagaren.
        """
        import urllib.parse

        return urllib.parse.urlencode({"SAMLResponse": saml_response})

    async def post_saml_response(
        self,
        do_request: Any,
        *,
        saml_response: str,
        jar: CookieJar | None = None,
        idp: str = IDP_STOCKHOLM_PAR,
    ) -> CookieJar:
        """POST:ar SAMLResponse och konvergerar vid IMHome.

        login.ashx -> sso/Login -> Default.aspx -> hubbens request-token-sida (dold
        oauth_token) -> POST -> LoginCallback (IMHome). De två sista hoppen delas med
        token-bridgen via `_oauth_submit_to_callback`. `jar` är den som `run` fyllde:
        login.ashx kan kräva cookies som sattes redan i första hoppet. En jar utan
        IMHome är ett fel.
        """
        from .token_client import TokenError, _oauth_submit_to_callback

        jar = jar if jar is not None else CookieJar()
        post_url = f"{SSO_BASE}{SSO_LOGIN_PATH}?idp={idp}"
        status, headers, _ = await self._send(
            do_request,
            jar,
            "POST",
            post_url,
            body=self._urlencode_saml_response(saml_response),
            extra={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": SSO_BASE,
            },
        )
        location = headers.get("location")
        if not location:
            raise TokenError(f"POST SAMLResponse till login.ashx svarade {status}")

        status, page_url, body = await self._follow(
            do_request, jar, urljoin(post_url, location), "saml-post"
        )
        if status != 200 or "oauth_token" not in body:
            raise TokenError(f"kunde inte nå request-token-sidan (status {status} vid {page_url})")

        def headers_for(url: str) -> dict[str, str]:
            return self._headers(jar.cookie_header(url))

        return await _oauth_submit_to_callback(
            do_request, request_token_url=page_url, headers_for=headers_for, jar=jar
        )

    # --------------------------------------------------------------- form-parsing
    @staticmethod
    def _extract_saml_response_form(html: str, page_url: str) -> str | None:
        """Hämtar SAMLResponse-värdet ur SiteMinder-formen som POST:ar till login.ashx.

        Returnerar None om ingen sådan form finns (dvs BankID-vägen fortsätter).
        """
        if not html or "login.ashx" not in html:
            return None
        # Första <form action="...login.ashx">.
        form = re.search(r"<form\b[^>]*>", html, re.IGNORECASE)
        if not form:
            return None
        action = re.search(r'action=["\']([^"\']*)["\']', form.group(0), re.IGNORECASE)
        if not action or "login.ashx" not in action.group(1):
            return None
        # SAMLResponse kan vara dolt <input> eller ett formfält; hämta värdet.
        value = re.search(
            r'name=["\']SAMLResponse["\'][^>]*value=["\']([^"\']*)["\']',
            html,
            re.IGNORECASE,
        )
        if value:
            return value.group(1)
        # Samma fält med attributen i omvänd ordning (value före name).
        reverse = re.search(
            r'value=["\']([^"\']*)["\'][^>]*name=["\']SAMLResponse["\']',
            html,
            re.IGNORECASE,
        )
        if reverse:
            return reverse.group(1)
        # Annars: en form vars action är saml2sso med en SAMLResponse-query.
        saml = re.search(r"SAMLResponse=([^&\"]+)", html)
        if saml:
            return saml.group(1)
        return None


    @staticmethod
    def _as_json(body: str) -> Any:
        import json

        trimmed = (body or "").strip()
        if not trimmed:
            return None
        try:
            return json.loads(trimmed)
        except json.JSONDecodeError:
            return None

    @staticmethod
    async def _sleep(seconds: float) -> None:
        await asyncio.sleep(seconds)

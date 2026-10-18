"""BankID-inloggningsföraren för InfoMentor (Phase B: Stockholm Stad).

Driver Stockholms stads SiteMinder + PhenixID så att en användare kan logga in
med BankID *inuti* Home Assistant — utan att först gå till en mobilapp. Detta är
inte BankID:s egen RP-API (`auth`/`collect`/`cancel`, QR-från `qrStartToken`); det
finns inte för oss. Vi kör istället de sidor som Stockholm Stad erbjuder:

    sso.infomentor.se/login.ashx?idp=stockholm_par
      -> login001.stockholm.se/affwebservices/public/saml2sso  (SAMLRequest)
      -> .../siteminderagent/forms/amedborgare.jsp             ("Hur vill du logga in?")
      -> login003.stockholm.se (PhenixID BankID-lager)   <-- QR/autostart/collect
      -> SAMLResponse-form -> POST sso.infomentor.se/login.ashx -> ... -> IMHome

Kedjan har två grenar som möts igen:

- TYST: finns en giltig SMSESSION (Stockholms stads SSO) kan vi hoppa över BankID
  helt. SiteMinder svarar direkt med SAMLResponse-formen; vi POST:ar den till
  login.ashx och konvergerar.
- INTERAKTIV: SMSESSION är förbrukad → SiteMinder visar medborgare.jsp, vi styr till
  login003, initierar BankID-ordern, visar QR/autostart, pollar verifyorder tills
  BankID-appen godkänner, hämtar SAMLResponse och POST:ar den.

Både den tysta och den interaktiva grenen konvergerar i `async_post_samlresponse`
(login.ashx -> LoginCallback -> IMHome), som redan hanteras av
`token_client.bridge_web_session`. Denna modul returnerar alltså SAMLResponse eller
SMSESSION och låter bridgen göra resten.

Verifierat 2026-10-15 mot en live-inloggning (138 HTTP-hopp, se
openspec/changes/app-token-auth/): initialize-svaret bär
`{"token","order","qrData"}` och sätter `PhxBankIdDeviceId`; `verifyorder` pollas
GET (tomt HTML-svar medan BankID-appen håller på); `SiteMinderAuthADC` sätter
`SMSESSION` och är gränsen för "godkänt". Källor: auth-research.md + den fångade
kedjan /tmp/infomentor-login-shapes/.

Risk: vi skripar en tredjeparts inloggningsida. Den måste vara användarstyrd,
låg takt, aldrig auto-försöka, och en ändrad sida måste felaklassas tydligt
("använd token-klistring").
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

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
POLL_INTERVAL_SECONDS = 2.0
MAX_POLL_ATTEMPTS = int(MAX_POLL_SECONDS / POLL_INTERVAL_SECONDS) + 1

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
    state: str = "PENDING"
    action: str = "bankid"  # progress_action
    task: str = "bankid"  # progress_task (HA >= 2024.8 kräver detta)


@dataclass
class BankIDResult:
    """Resultatet av BankID-inloggningen."""

    saml_response: str | None = None  # när vi körde interaktivt
    smsession: str | None = None  # när vi körde tyst (giltig SSO)
    jar: CookieJar | None = None  # cookies från IdP-hoppen


class BankIDDriver:
    """Kör Stockholm Stads BankID-inloggning och konvergerar vid login.ashx.

    `do_request` är async callable(method, url, body, headers) -> aiohttp:svar,
    binder vi till en session i api.py. All logik är testbar mot en mock.
    """

    def __init__(self, *, poll_interval: float = POLL_INTERVAL_SECONDS) -> None:
        self._poll_interval = poll_interval

    async def _once(
        self,
        do_request: Any,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: str | None = None,
    ) -> tuple[int, dict[str, str], str, CookieJar]:
        """Ett anrop: (status, headers, body, jar_updates). Ingen omdirigering följs."""
        response = await do_request(method, url, body, headers)
        try:
            status = response.status
            resp_headers: dict[str, str] = {
                k.lower(): v for k, v in response.headers.items()
            }
            text = await response.text()
        finally:
            response.release()
        jar = CookieJar()
        for chunk in response.headers.getall("Set-Cookie", []):
            jar.set_from_header(chunk, url)
        return status, resp_headers, text, jar

    @staticmethod
    def _headers(url: str, *, cookie: str = "") -> dict[str, str]:
        out = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
        }
        if cookie:
            out["Cookie"] = cookie
        return out

    async def run(
        self,
        do_request: Any,
        *,
        idp: str = IDP_STOCKHOLM_PAR,
        smsession: str | None = None,
        device_id: str | None = None,
        progress: Any | None = None,
        poll_interval: float | None = None,
    ) -> BankIDResult:
        """Kör hela kedjan och returnerar SAMLResponse/SMSESSION.

        `progress` är ett optional async callable(step: BankIDStep) som config-flödet
        använder för async_show_progress. `smsession` gör en tyst körning om den är
        giltig; annars interaktiv BankID.
        """
        interval = poll_interval or self._poll_interval

        # Hopp 1 — starta SSO:n från InfoMentor.
        start_url = f"{SSO_BASE}{SSO_LOGIN_PATH}?idp={idp}"
        status, headers, _, jar = await self._once(do_request, "GET", start_url)
        location = headers.get("location") or ""
        if not location:
            raise BankIDPageChanged(f"login.ashx omdirigade inte (status {status})")
        saml2sso_url = str(URL(start_url).join(URL(location)))

        # Hopp 2 — saml2sso: antingen SAMLResponse-formen (tyst) eller BankID-valören (interaktiv).
        if smsession:
            jar.set_from_header(f"SMSESSION={smsession}", SITEMINDER_BASE)

        saml2_headers = self._headers(saml2sso_url, cookie=jar.cookie_header(saml2sso_url))
        status, headers, html, jar = await self._once(
            do_request, "GET", saml2sso_url, headers=saml2_headers
        )
        saml_response = self._extract_saml_response_form(html, saml2sso_url)
        if saml_response is not None:
            # Tyst: SiteMinder gav SAMLResponse direkt → giltig SSO.
            return BankIDResult(saml_response=saml_response, jar=jar)

        # Interaktiv: SiteMinder visar medborgare.jsp (BankID-val). Styr dit.
        chooser_url = self._siteMinder_chooser(html, saml2sso_url)
        if not chooser_url:
            raise BankIDPageChanged(
                "siteMinder svarade inte med SAMLResponse eller BankID-val — "
                "sidan ser annorlunda ut"
            )

        # Hopp 3 — SiteMinder-medborgare.jsp: välj BankID. POST:ar tillbaka.
        chooser_headers = self._headers(chooser_url, cookie=jar.cookie_header(chooser_url))
        status, headers, chooser_html, jar = await self._once(
            do_request, "GET", chooser_url, headers=chooser_headers
        )
        bankid_start_url = self._siteMinder_bankid_form(chooser_html, chooser_url)
        if not bankid_start_url:
            raise BankIDPageChanged(
                "medborgare.jsp hade inga BankID-startfält — sidan ser annorlunda ut"
            )
        status, headers, _, jar = await self._once(
            do_request,
            "POST",
            bankid_start_url,
            headers=self._headers(bankid_start_url, cookie=jar.cookie_header(bankid_start_url)),
            body=self._siteMinder_bankid_post_fields(chooser_html),
        )
        # Om SiteMinder redirectar till login003 direkt har vi målet.
        login003_auth_url = headers.get("location")
        joined = str(URL(chooser_url).join(URL(login003_auth_url))) if login003_auth_url else ""
        if login003_auth_url and PHENIXID_BASE in joined:
            phenixid_base = joined
        else:
            phenixid_base = self._find_phenixid_base(chooser_html, chooser_url)
            if not phenixid_base:
                raise BankIDPageChanged("kunde inte hitta PhenixID-entrypunkten på medborgare.jsp")

        # Hopp 4 — PhenixID: initiera BankID-ordern.
        if progress:
            await progress(BankIDStep("Öppnar BankID…", action="bankid", task="bankid"))
        init_url = f"{phenixid_base}/NECSadcmbid/authenticate/NECSadcmbid"
        init_qs = self._phenixid_common_query(init_url, headers)
        init_url = f"{init_url}?{init_qs}&initialize="
        init_headers = self._headers(init_url, cookie=jar.cookie_header(init_url))
        status, headers, init_body, jar = await self._once(
            do_request, "GET", init_url, headers=init_headers
        )
        if status != 200:
            raise BankIDPageChanged(f"PhenixID initialize svarade {status}")
        init_json = self._as_json(init_body)
        if not isinstance(init_json, dict) or "order" not in init_json:
            raise BankIDPageChanged(f"PhenixID initialize gav oförbart svar: {init_body[:120]!r}")
        order_id = init_json["order"]
        token = init_json.get("token", "")
        qr_data = init_json.get("qrData")

        # Hopp 5 — polla verifyorder tills BankID-appen godkänner (eller tiden går ut).
        return await self._poll_verifyorder(
            do_request, phenixid_base, headers, order_id, token, qr_data, jar, progress, interval
        )

    async def _poll_verifyorder(
        self,
        do_request: Any,
        phenixid_base: str,
        common_headers: dict[str, str],
        order_id: str,
        token: str,
        qr_data: str | None,
        jar: CookieJar,
        progress: Any | None,
        interval: float,
    ) -> BankIDResult:
        verify_url = f"{phenixid_base}/NECSadcmbid/authenticate/NECSadcmbid"
        autostart = f"bankid:///?autostarttoken={token}&redirect=null" if token else None
        poll_count = 0
        started = time.monotonic()
        last_qr = ""
        while poll_count < MAX_POLL_ATTEMPTS:
            poll_count += 1
            await self._sleep(interval)
            qs = (
                f"{self._phenixid_common_query(verify_url, common_headers)}"
                f"&verifyorder={order_id}&_={int(time.time() * 1000)}"
            )
            check_url = f"{verify_url}?{qs}"
            check_headers = self._headers(check_url, cookie=jar.cookie_header(check_url))
            status, headers, body, jar = await self._once(
                do_request, "GET", check_url, headers=check_headers
            )
            poll_json = self._as_json(body)
            if isinstance(poll_json, dict) and "state" in poll_json:
                state = poll_json["state"]
                qr_data = poll_json.get("qrData") or qr_data
                if state == "OK":
                    if progress:
                        await progress(
                            BankIDStep("Godkänt — slutför inloggning…", state="OK")
                        )
                    return await self._finish_after_ok(
                        do_request, phenixid_base, common_headers, jar
                    )
                if state == "CANCELLED":
                    raise BankIDDenied("BankID-appen avböjde inloggningen")
                if state == "ERROR":
                    if poll_count > 1:
                        raise BankIDError("BankID-ordern gick ut — försök igen")
                    continue
            else:
                approved = self._verifyorder_approved(body, status)
                if approved == "approved":
                    if progress:
                        await progress(
                            BankIDStep("Godkänt — slutför inloggning…", state="OK")
                        )
                    return await self._finish_after_ok(
                        do_request, phenixid_base, common_headers, jar
                    )
                if approved == "denied":
                    raise BankIDDenied("BankID-appen avböjde inloggningen")
            if qr_data and qr_data != last_qr:
                last_qr = qr_data
                if progress:
                    await progress(
                        BankIDStep(
                            "Skanna QR-koden i BankID-appen",
                            qr_data=qr_data,
                            autostart_url=autostart,
                            state="PENDING",
                        )
                    )
            if time.monotonic() - started > MAX_POLL_SECONDS:
                raise BankIDTimeout(
                    f"Tiden gick ut efter {int(time.monotonic() - started)} s utan godkännande"
                )
        raise BankIDTimeout(f"Tiden gick ut efter {MAX_POLL_ATTEMPTS} polls utan godkännande")

    async def _finish_after_ok(
        self,
        do_request: Any,
        phenixid_base: str,
        common_headers: dict[str, str],
        jar: CookieJar,
    ) -> BankIDResult:
        """Hämtar SMSESSION/SAMLResponse efter BankID-godkännande."""
        verify_url = f"{phenixid_base}/NECSadcmbid/authenticate/NECSadcmbid"
        adc_url = f"{verify_url}/SiteMinderAuthADC"
        adc_qs = f"{self._phenixid_common_query(adc_url, common_headers)}"
        adc_full = f"{adc_url}?{adc_qs}"
        status, adc_headers, _, jar = await self._once(
            do_request,
            "GET",
            adc_full,
            headers=self._headers(adc_full, cookie=jar.cookie_header(adc_full)),
        )
        smsession = self._smsession_from_jar(jar)
        location = adc_headers.get("location") or ""
        if location:
            return BankIDResult(smsession=smsession or None, jar=jar)
        if smsession:
            return BankIDResult(smsession=smsession, jar=jar)
        saml2sso = f"{SITEMINDER_BASE}/affwebservices/public/saml2sso"
        status, _, html, jar = await self._once(
            do_request,
            "GET",
            saml2sso,
            headers=self._headers(saml2sso, cookie=jar.cookie_header(saml2sso)),
        )
        saml_response = self._extract_saml_response_form(html, saml2sso)
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
        saml_response: str | None = None,
        smsession: str | None = None,
        idp: str = IDP_STOCKHOLM_PAR,
    ) -> CookieJar:
        """POST:S:ar SAMLResponse/SMSESSION och konvergerar vid IMHome.

        Kör den gemensamma delen efter att BankID godkänts: login.ashx -> sso/Login ->
        Default.aspx -> hub login?forceOAuth -> request-token (dold oauth_token) -> submit
        -> LoginCallback (IMHome). Återanvänder token_client._oauth_submit_to_callback för
        de två sista hoppen. `smsession` ger en tyst inloggning (ingen SAMLResponse).

        Varje hopp status- *och* form-kontrollerar; en jar utan IMHome är ett fel.
        """
        from .token_client import _oauth_submit_to_callback

        jar = CookieJar()

        def headers_for(url: str) -> dict[str, str]:
            cookie = jar.cookie_header(url)
            out = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
                "User-Agent": USER_AGENT,
            }
            if cookie:
                out["Cookie"] = cookie
            if smsession:
                out.setdefault("Cookie", "")
                out["Cookie"] = f"{out['Cookie']}; SMSESSION={smsession}".strip("; ")
            return out

        # Hopp 1 — POST SAMLResponse till login.ashx, eller starta om kedjan om tyst.
        # Både saml_post_url (svars-URL för SAMLResponse) och start_url (ny kedja) pekar
        # på samma login.ashx; de gemensamma hoppen 2-3 omdirigerar relativt den URL:n
        # vi faktiskt skickade. Håll dem isär så att saml_response-grenen inte använder
        # start_url (som bara sätts i tyst-grenen) — annars UnboundLocalError.
        saml_post_url = f"{SSO_BASE}{SSO_LOGIN_PATH}?idp={idp}"
        start_url = saml_post_url
        if saml_response:
            status, headers, _, jar2 = await self._once(
                do_request,
                "POST",
                saml_post_url,
            headers={
                **headers_for(saml_post_url),
                "Content-Type": "application/x-www-form-urlencoded",
            },
             body=self._urlencode_saml_response(saml_response),
        )
            jar = jar2
            location = headers.get("location") or ""
            if not location:
                from .token_client import TokenError

                raise TokenError(f"POST SAMLResponse till login.ashx svarade {status}")
            # SAMLResponse-grenen fortsätter relativt svars-URL:n.
            start_url = str(URL(saml_post_url).join(URL(location)))
        elif smsession:
            # Tyst: starta kedjan med SMSESSION; saml2sso ger SAMLResponse-formen direkt.
            status, headers, _, jar2 = await self._once(
                do_request, "GET", start_url, headers=headers_for(start_url)
            )
            jar = jar2
            location = headers.get("location") or ""
            if not location:
                from .token_client import TokenError

                raise TokenError(f"login.ashx omdirigade inte (status {status})")
        else:
            from .token_client import TokenError

            raise TokenError("post_saml_response kräver saml_response eller smsession")

        # Hopp 2-3 — sso/Login -> Default.aspx -> hub login?forceOAuth (manuell omdirigering).
        target = str(URL(start_url).join(URL(location)))
        for _ in range(5):
            status, headers, body, jar = await self._once(
                do_request, "GET", target, headers=headers_for(target)
            )
            loc = headers.get("location")
            if not loc:
                # Sista sidan: request-token-sidan bär oauth_token.
                if "oauth_token" not in body and "forceOAuth" not in target:
                    from .token_client import TokenError

                    raise TokenError(f"kunde inte nå request-token-sidan (status {status})")
                break
            target = str(URL(target).join(URL(loc)))

        # Hopp 4 — request-token-sidan -> submit -> LoginCallback (IMHome).
        return await _oauth_submit_to_callback(
            do_request, request_token_url=target, headers_for=headers_for
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
        # Annars: en form vars action är saml2sso med en SAMLResponse-query.
        saml = re.search(r"SAMLResponse=([^&\"]+)", html)
        if saml:
            return saml.group(1)
        return None

    @staticmethod
    def _siteMinder_chooser(html: str, page_url: str) -> str | None:
        """medborgare.jsp: länken/formen som välj BankID (till login003)."""
        # SiteMinder redirectar ofta; leta efter en action mot NECSadc/mbid.
        for m in re.finditer(r'action=["\']([^"\']*)["\']', html, re.IGNORECASE):
            candidate = m.group(1)
            if "NECSadc/mbid" in candidate or "mbid" in candidate:
                try:
                    return str(URL(page_url).join(URL(candidate)))
                except Exception:  # noqa: BLE001
                    continue
        return None

    @staticmethod
    def _siteMinder_bankid_form(html: str, page_url: str) -> str | None:
        """Formuläret som välj BankID och POST:ar tillbaka till SiteMinder."""
        for m in re.finditer(r"<form\b[^>]*>", html, re.IGNORECASE):
            action = re.search(r'action=["\']([^"\']*)["\']', m.group(0), re.IGNORECASE)
            if not action:
                continue
            # Ett BankID-val: action som pekar på NECSadc/mbid eller en mbid-endpoint.
            if "mbid" in action.group(1).lower():
                try:
                    return str(URL(page_url).join(URL(action.group(1))))
                except Exception:  # noqa: BLE001
                    continue
        return None

    @staticmethod
    def _siteMinder_bankid_post_fields(html: str) -> str:
        """Dolda fält som SiteMinder kräver för BankID-valet (SMENC, SMLOCALE, target …)."""
        fields: dict[str, str] = {}
        for tag in re.finditer(r"<input\b[^>]*>", html, re.IGNORECASE):
            name = re.search(r'name=["\']([^"\']*)["\']', tag.group(0), re.IGNORECASE)
            type_match = re.search(r'type=["\']([^"\']*)["\']', tag.group(0), re.IGNORECASE)
            if not name:
                continue
            if (type_match.group(1) if type_match else "").lower() == "hidden":
                value = re.search(r'value=["\']([^"\']*)["\"]', tag.group(0), re.IGNORECASE)
                fields[name.group(1)] = value.group(1) if value else ""
        import urllib.parse

        return urllib.parse.urlencode(fields)

    @staticmethod
    def _find_phenixid_base(html: str, page_url: str) -> str | None:
        """login003-hosten som medborgare.jsp pekar på."""
        for m in re.finditer(r'https?://[^\s"\'<>]*login003\.stockholm\.se[^\s"\'<>]*', html):
            try:
                u = URL(m.group(0))
                return f"{u.scheme}://{u.host}"
            except Exception:  # noqa: BLE001
                continue
        return None

    @staticmethod
    def _phenixid_common_query(url: str, headers: dict[str, str]) -> str:
        """Delar av PhenixID-querysträngen som är gemensam (TYPE, REALMOID, GUID …).

        Hämtas ur den aktuella URL:ns query så vi bevarar exakt de parametre som
        IdP:s sida skickade — de bär sessionens bindning.
        """
        try:
            u = URL(url)
        except Exception:  # noqa: BLE001
            return ""
        kept: list[tuple[str, str]] = []
        query = u.query
        for key in ("TYPE", "REALMOID", "GUID", "SMAUTHREASON", "METHOD", "SMAGENTNAME", "TARGET"):
            for value in query.getall(key, []):
                kept.append((key, value))
        import urllib.parse

        return urllib.parse.urlencode(kept)

    @staticmethod
    def _verifyorder_approved(body: str, status: int) -> str:
        """Tolkar verifyorder-svaret: 'approved', 'denied' eller 'pending'."""
        # FenixID returnerar status 200 medan den håller på; godkännande/deny syns i kroppen.
        low = (body or "").lower()
        if "denied" in low or "avböjd" in low or "cancelled" in low:
            return "denied"
        # Godkänt: en omdirigering eller en specifik status.
        if status not in (200,) and "verifyorder" not in (body or "")[:0]:
            # Alla icke-200-svar efter initialize tolkas som slutresultat.
            return "approved"
        # Om kroppen bär ett SAMLResponse/redirect, är vi klara.
        if "saml2sso" in low or "login.ashx" in low:
            return "approved"
        return "pending"

    @staticmethod
    def _smsession_from_jar(jar: CookieJar) -> str | None:
        for cookie in jar.all():
            if cookie["name"] == "SMSESSION":
                return cookie["value"]
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

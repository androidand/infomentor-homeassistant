"""Gemensamt för BankID-inloggning mot InfoMentors IdP:er.

Varje kommun har sin egen IdP (`sso.infomentor.se/login.ashx?idp=<värde>`) med egna sidor,
egen start av BankID-ordern, egen QR och egen polling. Det som är lika för alla ligger här:
felen, `BankIDStep`/`BankIDResult`, kontrollen av SAML-formen och `post_saml_response`.

En drivrutin (en klass per IdP, se `bankid_stockholm.py`) har
`async run(do_request, *, progress) -> BankIDResult`, anropar `progress(BankIDStep)` medan
användaren skannar och slutar med `saml_result(sidan, jar)`. Den registreras i `DRIVERS`
i `api.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode, urljoin, urlsplit

from .const import SSO_LOGIN_PATH
from .token_client import TokenError, submit_oauth_token
from .util import hidden_inputs
from .web import CookieJar, DoRequest, Page, fetch, follow, form_action, host_and_path

SSO_BASE = "https://sso.infomentor.se"


class BankIDError(Exception):
    """Basfel för BankID-inloggningen."""


class BankIDTimeout(BankIDError):
    """Tiden gick ut innan BankID-appen godkände. Be användaren försöka igen."""


class BankIDDenied(BankIDError):
    """Användaren avbröt BankID (avböjde) i stället för att godkänna."""


class BankIDPageChanged(BankIDError):
    """En IdP-sida såg annorlunda ut än förväntat."""


@dataclass
class BankIDStep:
    """Ett steg i inloggningen att visa i config-flödet."""

    message: str
    qr_data: str | None = None
    autostart_token: str | None = None
    state: str = "PENDING"


@dataclass
class BankIDResult:
    """Resultatet av BankID-inloggningen."""

    saml_response: str
    jar: CookieJar  # cookies från IdP-hoppen


def saml_result(page: Page, jar: CookieJar) -> BankIDResult:
    """SAML-intyget ur IdP:ns sista sida (en auto-post-form till sso.infomentor.se)."""
    saml = hidden_inputs(page.text).get("SAMLResponse")
    action = form_action(page.text, page.url)
    if page.status != 200 or not saml or not action:
        raise BankIDPageChanged(
            f"ingen SAMLResponse-form efter godkännande ({page.status} {host_and_path(page.url)})"
        )
    if urlsplit(action).hostname != "sso.infomentor.se":
        # Bara parten som litar på intyget får ta emot det.
        raise BankIDPageChanged(f"SAMLResponse-formen pekar på {urlsplit(action).hostname}")
    return BankIDResult(saml_response=saml, jar=jar)


async def post_saml_response(
    do_request: DoRequest,
    *,
    saml_response: str,
    jar: CookieJar,
    idp: str,
) -> CookieJar:
    """Posta intyget till login.ashx och följ till hubbens request-token-sida och IMHome.

    `jar` är den som `run` fyllde: login.ashx behöver cookies från det första hoppet.
    """
    post_url = f"{SSO_BASE}{SSO_LOGIN_PATH}?idp={idp}"
    page = await fetch(
        do_request,
        jar,
        "POST",
        post_url,
        body=urlencode({"SAMLResponse": saml_response}),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": SSO_BASE},
    )
    location = page.headers.get("location")
    if not location:
        raise TokenError(f"POST SAMLResponse till login.ashx svarade {page.status}")
    page = await follow(do_request, jar, urljoin(post_url, location), error=TokenError)
    if page.status != 200 or "oauth_token" not in page.text:
        raise TokenError(f"nådde inte request-token-sidan (status {page.status})")
    return await submit_oauth_token(do_request, jar, page.url)

"""Appens OAuth2 refresh-token och bryggan från den till en hub-session.

Den hållbara inloggningsuppgiften är den roterande refresh-token som BankID ger en gång.
Varje uppdatering friskar den, byter access-token mot en ny IMHome-cookie och kan då
använda de vanliga hubb-anropen.

Konstanterna är appens egna: client_secret är den litterala strängen `NONE`.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any, TypedDict

import aiohttp

from .util import extract_oauth_token
from .web import CookieJar, fetch, follow, form_action, where

IM_HUB_BASE = "https://hub.infomentor.se"
IM_AUTH_BASE = "https://im.infomentor.se"
IM_API_BASE = "https://api-im.infomentor.se"

APP_OAUTH = {
    "client_id": "notificationapp",
    "client_secret": "NONE",
    "scope": "IM2-API-NOTIFICATION",
    "redirect_uri": "InfomentorNotification://oauth2Callback",
    "device_platform": "Android",
}

SSO_HANDOFF_PATH = "/NA1/Authentication/sso"
MENTOR_HANDOFF_PATH = "/Swedish/Production/mentor/"  # slutsnedstrecket krävs
MENTOR_TOKEN_PATH = "/swedish/production/mentor/"  # slutsnedstrecket krävs

_FORM = {"Content-Type": "application/x-www-form-urlencoded"}
AUTH_GUID_RE = re.compile(r"authGuid=([^&\"'\s]+)")
AUTH_CODE_RE = re.compile(r"[?&]code=([^?&\s]+)")


class InfomentorTokenError(Exception):
    """Basfel för token och bryggan."""


class TokenRevoked(InfomentorTokenError):
    """Token är ogiltig: be om ny inloggning. Får aldrig orsakas av ett tillfälligt WAF-fel."""


class Blocked(InfomentorTokenError):
    """Framändan svarade i stället för OAuth-endpointen: försök igen, aldrig reauth."""


class NetworkError(InfomentorTokenError):
    """Nätverksfel: försök igen."""


class TokenError(InfomentorTokenError):
    """Bridge eller parning: kedjan kunde inte fullföljas."""


class Token(TypedDict):
    access_token: str
    token_type: str
    expires_in: int
    refresh_token: str
    scope: str


def _as_token(data: dict[str, Any]) -> Token:
    return {
        "access_token": data.get("access_token", ""),
        "token_type": data.get("token_type", "bearer"),
        "expires_in": int(data.get("expires_in") or 0),
        "refresh_token": data.get("refresh_token", ""),
        "scope": data.get("scope", APP_OAUTH["scope"]),
    }


def classify_token_response(status: int, content_type: str | None, body: str) -> dict[str, Any]:
    """Bedöm ett refresh-svar efter dess *form*, inte statuskoden.

    Bara REVOKED, ett JSON-fel från OAuth-endpointen, säger något om token. BLOCKED är
    framändan (SiteMinder, PhenixID, F5) som svarar med HTML eller tomt, och får aldrig
    starta reauth. ALIVE är ett JSON-svar med refresh_token; allt oklart är UNKNOWN.
    """
    trimmed = (body or "").strip()
    is_json = "json" in (content_type or "").lower()

    if status == 200 and is_json and trimmed.startswith("{"):
        try:
            data = json.loads(trimmed)
        except json.JSONDecodeError:
            return {"verdict": "UNKNOWN", "why": "200 med JSON men oförbart svar"}
        if not isinstance(data, dict):
            return {"verdict": "UNKNOWN", "why": "200 JSON utan ett objekt"}
        if isinstance(data.get("refresh_token"), str) and data["refresh_token"]:
            return {"verdict": "ALIVE", "why": "200 med refresh_token", "token": _as_token(data)}
        if isinstance(data.get("error"), str) and data["error"]:
            return {"verdict": "REVOKED", "why": f"200 med OAuth-felfält '{data['error']}'"}
        return {"verdict": "UNKNOWN", "why": "200 JSON utan refresh_token eller error"}

    if not trimmed or not is_json:
        what = "tom body" if not trimmed else f"{'HTML' if trimmed[0] == '<' else 'icke-JSON'} body"
        return {"verdict": "BLOCKED", "why": f"status {status}, {what}: framändan svarade"}

    return {"verdict": "REVOKED", "why": f"status {status}, JSON body: {trimmed[:160]}"}


async def refresh_token(
    do_request: Any, refresh_token: str, *, auth_base: str = IM_AUTH_BASE
) -> Token:
    """Byt en refresh-token mot en ny token. Kastar TokenRevoked eller Blocked."""
    body = urllib.parse.urlencode(
        {
            "refresh_token": refresh_token,
            "client_id": APP_OAUTH["client_id"],
            "client_secret": APP_OAUTH["client_secret"],
            "grant_type": "refresh_token",
            "scope": APP_OAUTH["scope"],
        }
    )
    try:
        page = await fetch(
            do_request,
            CookieJar(),
            "POST",
            f"{auth_base}/Authentication/OAuth2/Token",
            body=body,
            headers=_FORM,
        )
    except (TimeoutError, aiohttp.ClientError) as err:
        raise NetworkError(str(err)) from err
    verdict = classify_token_response(page.status, page.headers.get("content-type"), page.text)
    if verdict["verdict"] == "ALIVE":
        return verdict["token"]
    if verdict["verdict"] == "REVOKED":
        raise TokenRevoked(verdict["why"])
    raise Blocked(verdict["why"])


def _json_object(body: str) -> Any:
    """Hubbens svar läses av appen ur ett <pre>-element: tolerera JSON direkt eller i HTML."""
    start, end = body.find("{"), body.rfind("}")
    chunk = body[start : end + 1] if 0 <= start < end else body
    return json.loads(chunk.replace("&amp;", "&"))


async def pair_app_token(
    do_request: Any,
    jar: CookieJar,
    *,
    device_identifier: str,
    device_name: str = "Home Assistant",
    hub_base: str = IM_HUB_BASE,
    auth_base: str = IM_AUTH_BASE,
) -> Token:
    """Byt en webbsession (jar med IMHome, ~40 min) mot appens roterande refresh-token.

    GetAuthenticationData, LoginOAuth2 (302 med engångskod), Token. Koden skickas okodad
    (Retrofit `encoded = true`); omkodning ger HTTP 400. Kastar TokenError.
    """
    page = await fetch(
        do_request,
        jar,
        "POST",
        f"{hub_base}/account/pair/GetAuthenticationData",
        body="",
        headers={
            **_FORM,
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    if page.status != 200:
        raise TokenError(f"GetAuthenticationData svarade {page.status}: är IMHome giltig?")
    try:
        guid = AUTH_GUID_RE.search(str(_json_object(page.text).get("authenticationUrl", "")))
    except (ValueError, AttributeError) as err:
        raise TokenError("GetAuthenticationData gav ett oförbart svar") from err
    if not guid:
        raise TokenError("GetAuthenticationData gav ingen authGuid")

    params = urllib.parse.urlencode(
        {
            "authGuid": guid.group(1),
            "DeviceIdentifier": device_identifier,
            "DeviceFriendlyName": device_name,
            "DevicePlatform": APP_OAUTH["device_platform"],
            "scope": APP_OAUTH["scope"],
            "redirect_uri": APP_OAUTH["redirect_uri"],
            "client_id": APP_OAUTH["client_id"],
            "response_type": "code",
            "isSandBox": "false",
        }
    )
    page = await fetch(
        do_request, jar, "GET", f"{auth_base}/Authentication/Authentication/LoginOAuth2?{params}"
    )
    code = AUTH_CODE_RE.search(page.headers.get("location", ""))
    if page.status != 302 or not code:
        raise TokenError(f"LoginOAuth2 gav ingen kod (status {page.status}): sessionen saknas")

    body = (
        f"code={code.group(1)}&client_id={APP_OAUTH['client_id']}"
        f"&client_secret={APP_OAUTH['client_secret']}"
        f"&redirect_uri={urllib.parse.quote(APP_OAUTH['redirect_uri'], safe='')}"
        "&grant_type=authorization_code"
    )
    page = await fetch(
        do_request,
        jar,
        "POST",
        f"{auth_base}/Authentication/OAuth2/Token",
        body=body,
        headers=_FORM,
    )
    try:
        token = json.loads(page.text)
    except ValueError:
        token = None
    if page.status != 200 or not isinstance(token, dict) or not token.get("refresh_token"):
        raise TokenError(f"Token-anropet vid parning misslyckades (status {page.status})")
    return _as_token(token)


async def submit_oauth_token(
    do_request: Any,
    jar: CookieJar,
    request_token_url: str,
    *,
    headers: dict[str, str] | None = None,
    mentor_base: str = "https://infomentor.se",
) -> CookieJar:
    """Hubbens request-token-sida (dold oauth_token), POST, LoginCallback (sätter IMHome).

    Delas av token-bryggan (Bearer i `headers`) och BankID-flödet (cookies i `jar`).
    """
    page = await fetch(do_request, jar, "GET", request_token_url, headers=headers)
    if not 200 <= page.status < 400:
        raise TokenError(f"request-token-sidan svarade {page.status} på {where(request_token_url)}")
    oauth_token = extract_oauth_token(page.text)
    if not oauth_token:
        raise TokenError(
            "request-token-sidan hade ingen oauth_token: webbsessionen är inte uppbyggd"
        )

    submit_url = form_action(page.text, request_token_url) or f"{mentor_base}{MENTOR_TOKEN_PATH}"
    page = await fetch(
        do_request,
        jar,
        "POST",
        submit_url,
        body=urllib.parse.urlencode({"oauth_token": oauth_token}),
        headers={**(headers or {}), **_FORM},
    )
    location = page.headers.get("location")
    if not location:
        raise TokenError("oauth_token-POST:en omdirigerade inte till LoginCallback")

    # LoginCallback sätter IMHome; den kan själv omdirigera vidare.
    await follow(
        do_request,
        jar,
        urllib.parse.urljoin(submit_url, location),
        error=TokenError,
        headers=headers,
    )
    if not jar.has("IMHome"):
        got = ", ".join(c["name"] for c in jar.all()) or "inga"
        raise TokenError(f"bridgen slutfördes utan IMHome (cookies: {got}): hubben avvisar den")
    return jar


async def bridge_web_session(
    do_request: Any,
    access_token: str,
    *,
    native_base: str = IM_API_BASE,
    mentor_base: str = "https://infomentor.se",
) -> CookieJar:
    """Byt en access-token mot en hub-cookie-jar med IMHome.

    Native sso-handoff, mentor/ (302), hubbens request-token-sida, POST, LoginCallback. En
    kedja som slutar utan IMHome är ett fel: utan IMHome ger hubben tomma 200-svar som läses
    som "inga notiser".
    """
    if not access_token:
        raise TokenError("bridge_web_session kräver en access-token")
    jar = CookieJar()
    bearer = {"Authorization": f"Bearer {access_token}"}

    sso = f"{native_base}{SSO_HANDOFF_PATH}"
    page = await fetch(do_request, jar, "GET", sso, headers=bearer)
    if page.status in (401, 403):
        raise TokenError(f"{SSO_HANDOFF_PATH} avvisade access-token (status {page.status})")
    if not 200 <= page.status < 300:
        raise TokenError(f"{SSO_HANDOFF_PATH} svarade {page.status}: väntade en handoff-URL")
    text = page.text.strip()
    try:
        candidate = json.loads(text) if text.startswith('"') else text
    except json.JSONDecodeError as err:
        raise TokenError(f"{SSO_HANDOFF_PATH} gav ett citat som inte är JSON") from err
    if MENTOR_HANDOFF_PATH not in str(candidate):
        raise TokenError(
            f"{SSO_HANDOFF_PATH} gav inte mentor-handoff (väntade '{MENTOR_HANDOFF_PATH}')"
        )
    handoff = urllib.parse.urljoin(sso, str(candidate))

    page = await fetch(do_request, jar, "GET", handoff, headers=bearer)
    location = page.headers.get("location")
    if not location or not 300 <= page.status < 400:
        raise TokenError(
            f"handoff svarade {page.status} utan omdirigering: "
            f"slutsnedstrecket i {MENTOR_HANDOFF_PATH} krävs"
        )
    return await submit_oauth_token(
        do_request,
        jar,
        urllib.parse.urljoin(handoff, location),
        headers=bearer,
        mentor_base=mentor_base,
    )


def make_aiohttp_request(session: Any, timeout: float = 30.0):
    """Binder en aiohttp-session till (method, url, data, headers) -> svar."""
    client_timeout = aiohttp.ClientTimeout(total=timeout)

    async def do_request(
        method: str, url: Any, data: str | None = None, headers: dict[str, str] | None = None
    ):
        return await session.request(
            method, url, data=data, headers=headers, timeout=client_timeout, allow_redirects=False
        )

    return do_request

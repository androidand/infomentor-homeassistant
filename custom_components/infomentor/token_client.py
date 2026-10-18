"""Durable credential för InfoMentor: appens OAuth2 refresh-token + hub-bridge.

Den hållbara inloggningsuppgiften är inte lösenordet, utan den roterande OAuth2-
refresh-token som BankID ger en gång (drivs i config-flödet, se design.md). Varje
uppdatering friskar token, ber om en sso-handoff, följer den till en ny IMHome-cookie
och kan därmed använda alla befintliga hubb-anrop — utan SMSESSION, utan BankID igen.

Detta portar tre delar från api-clients/infomentor (src/app-auth.ts, src/app-bridge.ts,
src/token-verdict.ts):

- `refresh_token` byter en refresh-token mot nya token på /Authentication/OAuth2/Token.
- `classify_token_response` dömer svaret efter *form*, inte status (REVOKED/BLOCKED).
- `bridge_web_session` byter en access-token mot en hub-cookie-jar med IMHome.

Allt annat (loginformulär, cookies, hubb-anrop) finns i api.py. Inget nytt beroende:
aiohttp följer med Home Assistant.

Konstanter hämtade från appen: client_id = `notificationapp`, client_secret är den
litterella strängen `NONE`, scope = `IM2-API-NOTIFICATION`. Se api-clients/openapi.yaml
(tags OAuth2, Pairing) och ../docs/auth-research.md för hela kedjan.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Protocol

from yarl import URL

# --------------------------------------------------------------- värdar och konstanter
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
MENTOR_HANDOFF_PATH = "/Swedish/Production/mentor/"  # kräver trailing slash
MENTOR_TOKEN_PATH = "/swedish/production/mentor/"  # kräver trailing slash
HUB_REQUEST_TOKEN_PATH = "/authentication/authentication/login"
HUB_LOGIN_CALLBACK_PATH = "/Authentication/Authentication/LoginCallback"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)

TOKEN_RE = re.compile(r"^[A-Za-z0-9!$*\-._~]+$")


# --------------------------------------------------------------- feltyper
class InfomentorTokenError(Exception):
    """Bas fel för token/bridge."""


class TokenRevoked(InfomentorTokenError):
    """Token är ogiltig (REVOKED): reauth. Får *inte* startas på en WAF-blip."""


class Blocked(InfomentorTokenError):
    """Framända svarade (BLOCKED): försök igen, aldrig reauth."""


class NetworkError(InfomentorTokenError):
    """Nätverksfel: försök igen."""


class TokenError(InfomentorTokenError):
    """Bridge: kedjan kunde inte fullföljas (t.ex. förlorad trailing slash)."""


class Token(Protocol):
    access_token: str
    token_type: str
    expires_in: int
    refresh_token: str
    scope: str


# --------------------------------------------------------------- friskning
def _token_body(refresh_token: str, *, scope: str | None) -> str:
    import urllib.parse

    params = {
        "refresh_token": refresh_token,
        "client_id": APP_OAUTH["client_id"],
        "client_secret": APP_OAUTH["client_secret"],
        "grant_type": "refresh_token",
    }
    if scope:
        params["scope"] = scope
    elif APP_OAUTH["scope"]:
        params["scope"] = APP_OAUTH["scope"]
    return urllib.parse.urlencode(params)


async def refresh_token(
    do_request: Any,
    refresh_token: str,
    *,
    auth_base: str = IM_AUTH_BASE,
    timeout: float = 30.0,
    scope: str | None = None,
) -> Token:
    """Byt en refresh-token mot nya token. Kastar REVOKED/BLOCKED/NetworkError.

    `do_request` är en async callable som tar (method, url, data, headers) och returnerar
    ett aiohttp:svar. I produktionen är det en aiohttp-session; i tester injekteras en
    mock. Detta gör att logiken är rent testbar utan att kräva en live-session.
    """
    body = _token_body(refresh_token, scope=scope)
    url = f"{auth_base}/Authentication/OAuth2/Token"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    response = await do_request("POST", url, body, headers)
    try:
        text = await response.text()
        content_type = (response.headers.get("Content-Type") or "").lower()
    finally:
        response.close()

    verdict = classify_token_response(
        response.status, content_type, text, _set_cookie_names(response)
    )
    if verdict["verdict"] == "ALIVE":
        token = json.loads(text)
        if not isinstance(token.get("refresh_token"), str) or not token["refresh_token"]:
            raise TokenRevoked("200 men ingen refresh_token i svaret")
        return _as_token(token)
    # REVOKED: JSON OAuth-fel → död token, reauth.
    if verdict["verdict"] == "REVOKED":
        raise TokenRevoked(verdict["why"])
    # BLOCKED / UNKNOWN: aldrig en död token, försök igen.
    raise Blocked(verdict["why"])


def _set_cookie_names(response: Any) -> list[str]:
    names = []
    for chunk in response.headers.getall("Set-Cookie", []):
        first = chunk.split(";", 1)[0].strip()
        if "=" in first:
            names.append(first.split("=", 1)[0].strip())
    return names


def _as_token(data: dict[str, Any]) -> Token:
    return {
        "access_token": data.get("access_token", ""),
        "token_type": data.get("token_type", "bearer"),
        "expires_in": int(data.get("expires_in") or 0),
        "refresh_token": data.get("refresh_token", ""),
        "scope": data.get("scope", APP_OAUTH["scope"]),
    }


# ------------------------------------------------------- parning (webbsession -> appens token)
AUTH_GUID_RE = re.compile(r"authGuid=([^&\"'\s]+)")
AUTH_CODE_RE = re.compile(r"[?&]code=([^?&\s]+)")


def _extract_json(body: str) -> Any:
    """Hubbens svar läses av appen ur ett <pre>-element: tolerera JSON direkt eller i HTML."""
    trimmed = body.strip()
    start, end = trimmed.find("{"), trimmed.rfind("}")
    chunk = trimmed[start : end + 1] if start >= 0 and end > start else trimmed
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
    """Byt en webbsession (jar med IMHome) mot appens roterande refresh-token.

    Webbsessionen räcker bara ~40 min; refresh-tokenen är den hållbara uppgiften.
    GetAuthenticationData -> LoginOAuth2 (302 med engångskod) -> Token. Koden skickas
    OKODAD (Retrofit `encoded = true`); omkodning ger HTTP 400. Samma kedja som
    infomentor/src/app-auth.ts i androidand/api-clients. Kastar TokenError.
    """
    import urllib.parse

    def headers_for(url: str, **extra: str) -> dict[str, str]:
        out = {"User-Agent": USER_AGENT, **extra}
        cookie = jar.cookie_header(url)
        if cookie:
            out["Cookie"] = cookie
        return out

    url = f"{hub_base}/account/pair/GetAuthenticationData"
    resp = await _hop(
        do_request,
        "POST",
        url,
        headers_for(
            url,
            Accept="application/json, text/plain, */*",
            **{
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "XMLHttpRequest",
            },
        ),
        "",
    )
    try:
        status, text = resp.status, await resp.text()
    finally:
        resp.close()
    if status != 200:
        raise TokenError(
            f"GetAuthenticationData svarade {status} — är webbsessionen (IMHome) giltig?"
        )
    try:
        data = _extract_json(text)
        guid = AUTH_GUID_RE.search(str(data.get("authenticationUrl", "")))
    except (ValueError, AttributeError) as err:
        raise TokenError("GetAuthenticationData gav ett oförbart svar") from err
    if not guid:
        raise TokenError("GetAuthenticationData gav ingen authGuid i authenticationUrl")

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
    url = f"{auth_base}/Authentication/Authentication/LoginOAuth2?{params}"
    resp = await _hop(
        do_request, "GET", url, headers_for(url, Accept="text/html,application/xhtml+xml,*/*")
    )
    try:
        status, location = resp.status, resp.headers.get("Location") or ""
    finally:
        resp.close()
    code = AUTH_CODE_RE.search(location)
    if status != 302 or not code:
        raise TokenError(
            f"LoginOAuth2 gav ingen auktoriseringskod (status {status}) —"
            " sessionen saknas eller är förbrukad"
        )

    body = (
        f"code={code.group(1)}"
        f"&client_id={APP_OAUTH['client_id']}"
        f"&client_secret={APP_OAUTH['client_secret']}"
        f"&redirect_uri={urllib.parse.quote(APP_OAUTH['redirect_uri'], safe='')}"
        "&grant_type=authorization_code"
    )
    resp = await _hop(
        do_request,
        "POST",
        f"{auth_base}/Authentication/OAuth2/Token",
        {"Content-Type": "application/x-www-form-urlencoded"},
        body,
    )
    try:
        status, text = resp.status, await resp.text()
    finally:
        resp.close()
    try:
        token = json.loads(text)
    except ValueError:
        token = None
    if status != 200 or not isinstance(token, dict) or not token.get("refresh_token"):
        raise TokenError(f"Token-anropet vid parning misslyckades (status {status})")
    return _as_token(token)


# --------------------------------------------------------------- domare
def classify_token_response(
    status: int,
    content_type: str | None,
    body: str,
    set_cookie_names: list[str] | None = None,
) -> dict[str, Any]:
    """Döm ett refresh-token-svar efter *form*, inte status (token-verdict.ts port).

    Endast REVOKED säger något om token: JSON-svar från OAuth-endpointen. BLOCKED betyder
    att framända (Sisteminder/PhenixID/F5) svarade istället och säger inget — ett sådant
    svar får aldrig starta reauth. Källan, de faktiska svaren och de falska livstider de
    gav, finns i ../docs/auth-research.md.

    - ALIVE:   200, JSON med en refresh_token.
    - REVOKED: JSON OAuth-fel (invalid_grant m.m.) — token är död.
    - BLOCKED: HTML/"Bad Request", XHTML-challenge, tom body — framända svarade.
    - UNKNOWN: ogiltig JSON eller annat som varken är tydligt ALIVE eller REVOKED.
    """
    trimmed = (body or "").strip()
    is_json = bool(content_type) and "json" in content_type.lower()
    looks_html = bool(trimmed) and trimmed[0] == "<"
    f5 = any(n.startswith("BIGipServer") for n in (set_cookie_names or []))

    if status == 200 and is_json and trimmed.startswith("{"):
        try:
            data = json.loads(trimmed)
        except json.JSONDecodeError:
            return {"verdict": "UNKNOWN", "why": "200 med JSON men oförbart svar"}
        if not isinstance(data, dict):
            return {"verdict": "UNKNOWN", "why": "200 JSON utan ett objekt"}
        rt = data.get("refresh_token")
        if isinstance(rt, str) and rt:
            return {"verdict": "ALIVE", "why": "200 med refresh_token", "token": _as_token(data)}
        error = data.get("error")
        if isinstance(error, str) and error:
            return {"verdict": "REVOKED", "why": f"200 med OAuth-felfält '{error}'"}
        return {"verdict": "UNKNOWN", "why": "200 JSON utan refresh_token eller error"}

    if looks_html or (not is_json and trimmed):
        kind = "JSON-typed" if is_json else "HTML" if looks_html else "icke-JSON"
        return {
            "verdict": "BLOCKED",
            "why": (
                f"status {status}, {kind} body ({len(trimmed)} tecken)"
                + (" och F5-cookie" if f5 else "")
                + " — framända svarade, inte OAuth-endpointen, säger inget om token"
            ),
        }

    if not trimmed:
        return {"verdict": "BLOCKED", "why": f"status {status}, tom body — okänd"}

    if is_json:
        return {"verdict": "REVOKED", "why": f"status {status}, JSON body: {trimmed[:160]}"}

    return {
        "verdict": "UNKNOWN",
        "why": f"status {status}, content-type {content_type!r}, {len(trimmed)} tecken",
    }


# --------------------------------------------------------------- cookie-jar
class CookieJar:
    """Minimal RFC 6265-cookie-jar, nog för att bära IMHome across domäner."""

    def __init__(self) -> None:
        self._cookies: list[dict[str, Any]] = []

    def all(self) -> list[dict[str, Any]]:
        return [dict(c) for c in self._cookies]

    def has(self, name: str) -> bool:
        return any(c["name"] == name for c in self._cookies)

    def cookie_header(self, url: str) -> str:
        try:
            u = URL(url)
        except Exception:  # noqa: BLE001
            return ""
        host, path, secure = u.host, u.path or "/", u.scheme == "https"
        now = time.time()
        matching = []
        for c in self._cookies:
            if c.get("expires") is not None and now > c["expires"]:
                continue
            if not self._domain_matches(c.get("domain", ""), host):
                continue
            if not self._path_matches(c.get("path", "/"), path):
                continue
            if c.get("secure") and not secure:
                continue
            matching.append(c)
        matching.sort(key=lambda c: len(c.get("path", "/")), reverse=True)
        return "; ".join(f'{c["name"]}={c["value"]}' for c in matching)

    @staticmethod
    def _domain_matches(domain: str, host: str) -> bool:
        if not domain:
            return False
        if domain == host:
            return True
        if domain.startswith("."):
            root = domain[1:]
            return host == root or host.endswith(domain)
        return False

    @staticmethod
    def _path_matches(cookie_path: str, request_path: str) -> bool:
        if cookie_path == request_path:
            return True
        if request_path.startswith(cookie_path):
            return cookie_path.endswith("/") or request_path[len(cookie_path)] == "/"
        return False

    def set_from_header(self, set_cookie: str, url: str) -> None:
        parts = set_cookie.split(";")
        first = parts[0]
        eq = first.find("=")
        if eq == -1:
            return
        name = first[:eq].strip()
        value = first[eq + 1 :].strip()
        try:
            host = URL(url).host or ""
        except Exception:  # noqa: BLE001
            return
        domain = "." + host if host else ""
        path = "/"
        secure = False
        expires: float | None = None
        for part in parts[1:]:
            kv = part.strip()
            key_eq = kv.find("=")
            key = kv if key_eq == -1 else kv[:key_eq].lower()
            val = "" if key_eq == -1 else kv[key_eq + 1 :].strip()
            if key == "domain":
                domain = val if val.startswith(".") else "." + val
            elif key == "path":
                path = val if val.startswith("/") else "/"
            elif key == "secure":
                secure = True
            elif key == "max-age":
                try:
                    expires = time.time() + int(val)
                except ValueError:
                    expires = 0
            elif key == "expires":
                try:
                    import email.utils

                    expires = email.utils.parsedate_to_datetime(val).timestamp()
                except (TypeError, ValueError):
                    pass
        if not domain:
            domain = host
        self._cookies.append(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path,
                "secure": secure,
                "expires": expires,
            }
        )


# --------------------------------------------------------------- bridge
async def _oauth_submit_to_callback(
    do_request: Any,
    *,
    request_token_url: str,
    headers_for: Any,
    mentor_base: str = "https://infomentor.se",
    jar: CookieJar | None = None,
) -> CookieJar:
    """Del 2 av bridgen: request-token-sidan (dold oauth_token) -> submit -> LoginCallback.

    Delas mellan token-bridge:n (friskad access-token) och BankID-driver:n (POST:ad
    SAMLResponse ger request-token-sidan). Krävir att request-token-sidan nås med
    headers_for (Bearer eller IdP-cookie); sätter IMHome i en jar på
    LoginCallback-hoppet. Använder samma hopphandtering som bridge_web_session.
    """
    import urllib.parse

    if jar is None:
        jar = CookieJar()

    # Hopp A — request-token-sidan bär en dold oauth_token.
    resp = await _hop(do_request, "GET", request_token_url, headers_for(request_token_url))
    _absorb_cookies(resp, request_token_url, jar)
    try:
        token_status = resp.status
        if token_status < 200 or token_status >= 400:
            raise TokenError(
                f"hubbens OAuth request-token-sida misslyckades "
                f"(status {token_status}) på {request_token_url}"
            )
        page_html = await resp.text()
    finally:
        resp.close()
    oauth_token = _field_value(page_html, "oauth_token")
    if not oauth_token:
        raise TokenError(
            f"request-token-sidan hade ingen oauth_token — webbsessionen är inte uppbyggd "
            f"(sidlängd {len(page_html)})"
        )

    # Hopp B — POST oauth_token; omdirigar till LoginCallback.
    submit_url = _form_action(page_html, request_token_url) or f"{mentor_base}{MENTOR_TOKEN_PATH}"
    resp = await _hop(
        do_request,
        "POST",
        submit_url,
        {**headers_for(submit_url), "Content-Type": "application/x-www-form-urlencoded"},
        urllib.parse.urlencode({"oauth_token": oauth_token}),
    )
    _absorb_cookies(resp, submit_url, jar)
    try:
        callback_location = resp.headers.get("Location") or ""
    finally:
        resp.close()
    if not callback_location:
        raise TokenError("oauth_token POST:en omdirigade inte till LoginCallback")

    # Hopp C — LoginCallback sätter IMHome.
    callback_url = str(URL(submit_url).join(URL(callback_location)))
    resp = await _hop(do_request, "GET", callback_url, headers_for(callback_url))
    _absorb_cookies(resp, callback_url, jar)
    try:
        after_callback = resp.headers.get("Location") or ""
    finally:
        resp.close()
    if after_callback:
        next_url = str(URL(callback_url).join(URL(after_callback)))
        resp = await _hop(do_request, "GET", next_url, headers_for(next_url))
        _absorb_cookies(resp, next_url, jar)
        resp.close()

    if not jar.has("IMHome"):
        got = ", ".join(c["name"] for c in jar.all()) or "(inga)"
        raise TokenError(
            f"bridgen slutförde men cookie-jaren har ingen IMHome — hubben avvisar den. "
            f"Cookies erhålda: {got}"
        )
    return jar


def _field_value(html: str, name: str) -> str | None:
    """Värdet på ett dolt formulärfält, oavsett attribute-ordning."""
    direct = re.search(
        rf'name=["\']\s*{re.escape(name)}\s*["\'][^>]*value=["\']([^"\']*)["\']',
        html,
        re.IGNORECASE,
    )
    if direct:
        return direct.group(1)
    reversed_ = re.search(
        rf'value=["\']([^"\']*)["\'][^>]*name=["\']\s*{re.escape(name)}\s*["\']',
        html,
        re.IGNORECASE,
    )
    return reversed_.group(1) if reversed_ else None


def _form_action(html: str, page_url: str) -> str | None:
    """action:t på första <form>, upplöst mot sidan det kom från."""
    open_form = re.search(r"<form\b[^>]*>", html, re.IGNORECASE)
    if not open_form:
        return None
    action = re.search(r'action=["\']([^"\']*)["\']', open_form.group(0), re.IGNORECASE)
    if not action:
        return None
    try:
        return str(URL(page_url).join(URL(action.group(1))))
    except Exception:  # noqa: BLE001
        return None


def _absorb_cookies(response: Any, url: str, jar: CookieJar) -> None:
    for chunk in response.headers.getall("Set-Cookie", []):
        jar.set_from_header(chunk, url)


async def _hop(
    do_request: Any,
    method: str,
    url: str,
    headers: dict[str, str],
    body: str | None = None,
) -> Any:
    return await do_request(method, url, body, headers)


async def bridge_web_session(
    do_request: Any,
    access_token: str,
    *,
    native_base: str = IM_API_BASE,
    hub_base: str = IM_HUB_BASE,
    mentor_base: str = "https://infomentor.se",
    user_agent: str = USER_AGENT,
) -> CookieJar:
    """Byt en access-token mot en hub-cookie-jar med IMHome.

    `do_request` är en async callable (method, url, body, headers) som returnerar aiohttp:svar.
    I produktionen är det en aiohttp-session; i tester injekteras en mock.

    Kedjan (app-bridge.ts port): native sso-handoff → mentor/ (302) → hub request-token
    (dold oauth_token) → mentor/ POST (302) → LoginCallback (sätter IMHome).

    Varje hopp status- *och* form-kontrollerar. En kedja som slutar utan IMHome är ett
    fel — att returnera den jar:t ger tomma 200 från hubben som läses som "inga notiser".
    """
    if not access_token:
        raise TokenError("bridge_web_session kräver en access-token")
    jar = CookieJar()

    def headers_for(url: str) -> dict[str, str]:
        cookie = jar.cookie_header(url)
        out = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
            "User-Agent": user_agent,
        }
        if cookie:
            out["Cookie"] = cookie
        return out

    # Hopp 1 — be den native API: om handoff-URL:n.
    handoff_target = f"{native_base}{SSO_HANDOFF_PATH}"
    response = await _hop(do_request, "GET", handoff_target, headers_for(handoff_target))
    try:
        status = response.status
        text = await response.text()
    finally:
        response.close()
    if status in (401, 403):
        raise TokenError(f"{SSO_HANDOFF_PATH} avvisade access-token (status {status}) — förnya den")
    if status < 200 or status >= 300:
        raise TokenError(f"{SSO_HANDOFF_PATH} svarade {status} — förväntade en handoff-URL")
    trimmed = text.strip()
    if trimmed.startswith('"'):
        try:
            candidate = json.loads(trimmed)
        except json.JSONDecodeError as err:
            raise TokenError(
                f"{SSO_HANDOFF_PATH} gav ett citat som inte är JSON — "
                f"vägrar gissa handoff-URL:n"
            ) from err
    else:
        candidate = trimmed
    if MENTOR_HANDOFF_PATH not in str(candidate):
        raise TokenError(
            f"{SSO_HANDOFF_PATH} gav en URL som inte är mentor-handoff "
            f"(förväntade sökväg med '{MENTOR_HANDOFF_PATH}')"
        )
    handoff_url = str(URL(f"{native_base}{SSO_HANDOFF_PATH}").join(URL(str(candidate))))

    # Hopp 2 — handoff omdirigerar till hubbens request-token-sida.
    resp = await _hop(do_request, "GET", handoff_url, headers_for(handoff_url))
    _absorb_cookies(resp, handoff_url, jar)
    try:
        location = resp.headers.get("Location") or ""
    finally:
        resp.close()
    if not location:
        raise TokenError(
            f"handoff omdirigade inte (ingen Location) — handoff-URL:n måste behålla "
            f"trailing slash ({MENTOR_HANDOFF_PATH})"
        )
    if resp.status < 300 or resp.status >= 400:
        raise TokenError(f"handoff svarade {resp.status} — förväntade 302")
    request_token_url = str(URL(handoff_url).join(URL(location)))

    # Hopp 3-5 — request-token-sidan (dold oauth_token) -> submit -> LoginCallback (IMHome).
    return await _oauth_submit_to_callback(
        do_request,
        request_token_url=request_token_url,
        headers_for=headers_for,
        mentor_base=mentor_base,
        jar=jar,
    )


# --------------------------------------------------------------- aiohttp-packare
# aiohttp:s ClientSession tar GET/POST. Vi binder do_request till sessionen i
# api.py/coordinator.py, så token_client är rent testbar mot en mock fetch.
def make_aiohttp_request(session: Any, timeout: float = 30.0):
    """Binder en aiohttp-session till (method, url, data, headers) -> svar."""
    import aiohttp

    client_timeout = aiohttp.ClientTimeout(total=timeout)

    async def do_request(
        method: str, url: str, data: str | None = None, headers: dict[str, str] | None = None
    ):
        if method == "GET":
            return await session.get(
                url, headers=headers, timeout=client_timeout, allow_redirects=False
            )
        if method == "POST":
            return await session.post(
                url, data=data, headers=headers, timeout=client_timeout, allow_redirects=False
            )
        raise ValueError(f"okej metod: {method}")

    return do_request

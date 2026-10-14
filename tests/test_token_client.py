"""Tester för token_client: friskning, domare och hub-bridge (kräver aiohttp).

Här portas api-clients/infomentors tre delar utan nätverk:

- `refresh_token`: byter refresh-token mot nya token; domar svaret REVOKED/BLOCKED.
- `classify_token_response`: dömer efter form, inte status.
- `bridge_web_session`: hela handoff-kedjan till en jar med IMHome, och varje hopp
  degradert för sig så en varselvakt alltid ser röd ut i minst ett fall.
"""

from __future__ import annotations

import unittest

import pytest

try:
    import yarl

    from custom_components.infomentor.token_client import (
        Blocked,
        CookieJar,
        TokenError,
        TokenRevoked,
        bridge_web_session,
        classify_token_response,
        make_aiohttp_request,
        refresh_token,
    )

    AVAILABLE = True
except Exception:  # noqa: BLE001
    AVAILABLE = False


@pytest.fixture(autouse=True)
def _no_hass_autouse():
    """Dena testmodulen kräver ingen HA-hass (rent aiohttp + logik).

    conftest.py har en autouse `enable_custom_integrations` som tar den nya
    async `hass`-fixturen och bricker på äldre HA-versioner; vår logik hänger
    inte på config-entry/nätverket, så vi undviker den här.
    """
    yield


# --------------------------------------------------------------- mock-svar och session
class HAHeaders(dict):
    """Litet dict-subclass som stödje aiohttp:s getall (för Set-Cookie)."""

    def __init__(self, data=None):
        super().__init__()
        for key, value in (data or {}).items():
            if isinstance(value, list):
                for item in value:
                    self.setdefault(key.lower(), []).append(item)
            else:
                self[key.lower()] = value

    def getall(self, key, default=None):
        value = super().get(key.lower(), [])
        return value if isinstance(value, list) else [value]

    def get(self, key, default=None):
        value = super().get(key.lower(), None)
        if value is None:
            return default
        if isinstance(value, list):
            return value[0] if value else default
        return value


class FakeResponse:
    def __init__(self, status=200, text="", headers=None, location=None):
        self.status = status
        self._text = text
        self.headers = HAHeaders(headers or {})
        if location is not None:
            self.headers["Location"] = location

    async def text(self):
        return self._text

    def close(self):
        return None

    @property
    def url(self):
        return yarl.URL("https://hub.infomentor.se")


class FakeSession:
    """Skriptad session: svar per url/metod. Spårar anropen för påståenden."""

    def __init__(self, routes):
        self._routes = routes
        self.calls = []
        self.allow_redirects_calls = []

    def route(self, method, predicate, responder):
        self._routes.append((method, predicate, responder))
        return self

    async def request(self, method, url, data=None, headers=None, allow_redirects=False, timeout=None):  # noqa: ANN001
        self.calls.append((method, str(url), data, headers))
        for route_method, predicate, responder in self._routes:
            if route_method == method and predicate(str(url)):
                return responder(str(url), method)
        raise AssertionError(f"oväntat anrop: {method} {url}")

    async def get(self, url, headers=None, timeout=None, allow_redirects=True):  # noqa: ANN001
        self.allow_redirects_calls.append(allow_redirects)
        return await self.request("GET", url, headers=headers)

    async def post(self, url, data=None, headers=None, timeout=None, allow_redirects=True):  # noqa: ANN001
        self.allow_redirects_calls.append(allow_redirects)
        return await self.request("POST", url, data=data, headers=headers)

    async def close(self):
        return None


class MockFetch:
    """Injekterbar fetch: svar per url, spårar anropen.

    Används som do_request(method, url, body, headers) -> svar i bridge_web_session.
    """

    def __init__(self, responses, *, expected=None):
        """
        responses: dict url-sökväg -> statiskt FakeResponse eller callable(self) -> svar.
        expected: valfri lista url-sökvägen som måste ha träffat.
        """
        self._responses = responses
        self._expected = expected or []
        self.calls = []
        self.posted_bodies = []
        self.headers_per_call = []

    async def __call__(self, method, url, body=None, headers=None):
        if isinstance(url, yarl.URL):
            url = str(url)
        path = url.split("?", 1)[0]
        self.calls.append((path, method))
        self.headers_per_call.append(headers or {})
        if body:
            self.posted_bodies.append(str(body))
        responder = self._responses.get(path)
        if responder is None:
            for rkey, rresponder in self._responses.items():
                if path.endswith(rkey):
                    responder = rresponder
                    break
        if responder is None:
            raise AssertionError(f"oväntat mock-anrop: {method} {url}")
        if callable(responder) and not isinstance(responder, FakeResponse):
            return responder(self)
        return responder


# --------------------------------------------------------------- konstanter för kedjan
IM_HUB = "https://hub.infomentor.se"
IM_API = "https://api-im.infomentor.se"
MENTOR = "https://infomentor.se"
SSO_URL = f"{IM_API}/NA1/Authentication/sso"
HANDOFF_URL = f"{MENTOR}/Swedish/Production/mentor/?login=remote&id=0Ab12"
REQUEST_TOKEN_URL = f"{IM_HUB}/authentication/authentication/login"
SUBMIT_URL = f"{MENTOR}/swedish/production/mentor/"
CALLBACK_URL = f"{IM_HUB}/Authentication/Authentication/LoginCallback"

REQUEST_TOKEN_PAGE = (
    "<!DOCTYPE html><html><body><form method='post' action='"
    + SUBMIT_URL
    + "'><input type='hidden' name='oauth_token' value='t7Yx2Kq9Lm4' /></form></body></html>"
)


def good_responses():
    """Hela handoff-kedjan som svarar korrekt, med IMHome sätts i hopp 5.

    Nycklarna är path-only (utan query) — handoff-URL:n bär query-sträng.
    """
    handoff_path = MENTOR + "/Swedish/Production/mentor/"
    request_token_path = "/authentication/authentication/login"
    submit_path = "/swedish/production/mentor/"
    return {
        "/NA1/Authentication/sso": FakeResponse(
            200,
            '"https://infomentor.se/Swedish/Production/mentor/?login=remote&id=0Ab12"',
            headers={"Content-Type": "application/json"},
        ),
        handoff_path: FakeResponse(
            302,
            "",
            headers={
                "Location": f"{IM_HUB}{request_token_path}?forceOAuth=true",
                "Set-Cookie": ['.ASPXAUTH=auth; Path=/; HttpOnly'],
            },
        ),
        request_token_path: FakeResponse(
            200,
            REQUEST_TOKEN_PAGE.replace(SUBMIT_URL, MENTOR + submit_path),
            headers={"Content-Type": "text/html"},
        ),
        submit_path: FakeResponse(302, "", headers={"Location": f"{IM_HUB}/Authentication/Authentication/LoginCallback?apitype=im1"}),
        "/Authentication/Authentication/LoginCallback": FakeResponse(
            302,
            "",
            headers={
                "Location": "/#/",
                "Set-Cookie": ['IMHome=session-value; Path=/; HttpOnly', 'andsyl2=x; Path=/'],
            },
        ),
        "/": FakeResponse(200, "<html>app</html>"),
    }


# --------------------------------------------------------------- refresh_token
@unittest.skipUnless(AVAILABLE, "kräver aiohttp och Home Assistant-paketet")
class TestRefreshToken(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_returns_rotated_token(self):
        session = FakeSession([]).route(
            "POST",
            lambda u: u.endswith("/OAuth2/Token"),
            lambda u, m: FakeResponse(200, '{"access_token":"a1","refresh_token":"r1","token_type":"bearer","expires_in":600,"scope":"IM2-API-NOTIFICATION"}',
                                      headers={"Content-Type": "application/json"}),
        )
        request = make_aiohttp_request(session)
        token = await refresh_token(request, "r0")
        self.assertEqual(token["refresh_token"], "r1")
        self.assertEqual(token["access_token"], "a1")
        posted = session.calls[0][2]
        self.assertIn("grant_type=refresh_token", posted)
        self.assertIn("refresh_token=r0", posted)
        self.assertIn("client_secret=NONE", posted)

    async def test_refresh_invalid_grant_is_revoked(self):
        session = FakeSession([]).route(
            "POST",
            lambda u: u.endswith("/OAuth2/Token"),
            lambda u, m: FakeResponse(200, '{"error":"invalid_grant"}',
                                      headers={"Content-Type": "application/json"}),
        )
        request = make_aiohttp_request(session)
        with self.assertRaises(TokenRevoked):
            await refresh_token(request, "r0")

    async def test_refresh_bad_request_is_blocked_not_revoked(self):
        """Ett F5 "Bad Request" (HTML) får INTE tolkas som död token."""
        session = FakeSession([]).route(
            "POST",
            lambda u: u.endswith("/OAuth2/Token"),
            lambda u, m: FakeResponse(400, "<html>Bad Request</html>",
                                       headers={"Content-Type": "text/html",
                                                "Set-Cookie": "BIGipServer~INFOMENTOR~POOL=rd1; Path=/"}),
        )
        request = make_aiohttp_request(session)
        with self.assertRaises(Blocked):
            await refresh_token(request, "r0")

    async def test_make_aiohttp_request_disables_redirects(self):
        """Bridge:n läser Location själv — aiohttp får inte följa redirects (#5)."""
        session = FakeSession([]).route(
            "GET",
            lambda u: True,
            lambda u, m: FakeResponse(200, "ok"),
        )
        request = make_aiohttp_request(session)
        await request("GET", "https://example.com/x")
        self.assertEqual(session.allow_redirects_calls, [False])


# --------------------------------------------------------------- classify_token_response
@unittest.skipUnless(AVAILABLE, "kräver aiohttp")
class TestClassify(unittest.TestCase):
    def test_alive(self):
        v = classify_token_response(200, "application/json", '{"refresh_token":"r","access_token":"a"}')
        self.assertEqual(v["verdict"], "ALIVE")

    def test_revoked_json_error(self):
        v = classify_token_response(200, "application/json", '{"error":"revoked"}')
        self.assertEqual(v["verdict"], "REVOKED")

    def test_blocked_html_bad_request(self):
        v = classify_token_response(400, "text/html", "Bad Request")
        self.assertEqual(v["verdict"], "BLOCKED")

    def test_blocked_empty_body(self):
        v = classify_token_response(200, "text/plain", "")
        self.assertEqual(v["verdict"], "BLOCKED")

    def test_unknown_unparseable_json(self):
        v = classify_token_response(200, "application/json", "{not json")
        self.assertEqual(v["verdict"], "UNKNOWN")

    def test_non_200_json_error_is_revoked(self):
        """Ett JSON OAuth-fel med status >= 400 ärREVOKED — annars faller detta test."""
        v = classify_token_response(400, "application/json", '{"error":"invalid_grant"}')
        self.assertEqual(v["verdict"], "REVOKED")

    def test_json_with_charset_is_recognized(self):
        """`application/json; charset=utf-8` är JSON — annars faller detta test (#4)."""
        v = classify_token_response(
            200, "application/json; charset=utf-8", '{"refresh_token":"r","access_token":"a"}'
        )
        self.assertEqual(v["verdict"], "ALIVE")

    def test_json_with_charset_error_is_revoked(self):
        """OAuth-fel med charset-bärda content-type är REVOKED, inte BLOCKED (#4)."""
        v = classify_token_response(
            400, "application/json; charset=utf-8", '{"error":"invalid_grant"}'
        )
        self.assertEqual(v["verdict"], "REVOKED")


# --------------------------------------------------------------- bridge_web_session
@unittest.skipUnless(AVAILABLE, "kräver aiohttp")
class TestBridge(unittest.IsolatedAsyncioTestCase):
    async def test_full_chain_sets_imhome(self):
        fetch = MockFetch(good_responses())
        jar = await bridge_web_session(fetch, "access-1")
        self.assertTrue(jar.has("IMHome"))
        self.assertTrue(jar.has(".ASPXAUTH"))

    async def test_bearer_carried_on_native_hop(self):
        fetch = MockFetch(good_responses())
        await bridge_web_session(fetch, "access-1")
        # Hopp 1 (native sso) får ha Bearer; .ASPXAUTH sätts inte förrän hopp 2.
        self.assertIn("Bearer access-1", str(fetch.headers_per_call[0]))

    async def test_lost_trailing_slash_is_blocked_handoff(self):
        """Hopp 2 svarar 200 utan Location — exakt vad en förlorad trailing slash gör."""
        handoff_path = MENTOR + "/Swedish/Production/mentor/"
        responses = good_responses()
        responses[handoff_path] = FakeResponse(200, "<html>a different page</html>",
                                              headers={"Content-Type": "text/html"})
        fetch = MockFetch(responses)
        with self.assertRaises(TokenError):
            await bridge_web_session(fetch, "access-1")

    async def test_request_token_without_oauth_token(self):
        responses = good_responses()
        responses[REQUEST_TOKEN_URL] = FakeResponse(200, "<html><form action='x'></form></html>",
                                                    headers={"Content-Type": "text/html"})
        fetch = MockFetch(responses)
        with self.assertRaises(TokenError):
            await bridge_web_session(fetch, "access-1")

    async def test_no_callback_redirect(self):
        responses = good_responses()
        responses[SUBMIT_URL] = FakeResponse(200, "<html>no callback</html>",
                                             headers={"Content-Type": "text/html"})
        fetch = MockFetch(responses)
        with self.assertRaises(TokenError):
            await bridge_web_session(fetch, "access-1")

    async def test_no_imhome_at_end(self):
        responses = good_responses()
        responses[CALLBACK_URL] = FakeResponse(302, "", headers={"Location": "/#/"})
        fetch = MockFetch(responses)
        with self.assertRaises(TokenError):
            await bridge_web_session(fetch, "access-1")

    async def test_empty_access_token_never_touches_network(self):
        fetch = MockFetch(good_responses())
        with self.assertRaises(TokenError):
            await bridge_web_session(fetch, "")
        self.assertEqual(fetch.calls, [])


# --------------------------------------------------------------- CookieJar
@unittest.skipUnless(AVAILABLE, "kräver aiohttp")
class TestCookieJar(unittest.TestCase):
    def test_domain_matching_and_header(self):
        jar = CookieJar()
        jar.set_from_header("IMHome=abc; Path=/; HttpOnly", f"{IM_HUB}/x")
        header = jar.cookie_header(f"{IM_HUB}/some/path")
        self.assertIn("IMHome=abc", header)
        # Andra domäner får inte cookie:t.
        self.assertNotIn("IMHome", jar.cookie_header("https://evil.example.com/"))

    def test_header_matching(self):
        jar = CookieJar()
        jar.set_from_header("IMHome=abc; Domain=.infomentor.se; Path=/; HttpOnly", f"{IM_HUB}/x")
        self.assertIn("IMHome=abc", jar.cookie_header("https://hub.infomentor.se/x"))
        self.assertIn("IMHome=abc", jar.cookie_header("https://infomentor.se/x"))


if __name__ == "__main__":
    unittest.main()

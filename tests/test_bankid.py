"""Tester för BankID-driver:n (bankid.py) mot en skriptad server med den live-verifierade kedjan.

Fixturerna följer formen på infomentor/tests/bankid-login.test.ts i androidand/api-clients
(commit c081bed): metodvalet är en länk (inte en form), BankID-sidans URL bär SiteMinders
query och används byte för byte, verifyorder-JSON serveras som text/html, en oskannad
order dör med ERROR, och efter OK hämtas SAML-intyget med en GET på sidans URL.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from custom_components.infomentor.bankid import (
    BankIDDenied,
    BankIDDriver,
    BankIDError,
    BankIDPageChanged,
    BankIDTimeout,
)
from custom_components.infomentor.token_client import CookieJar, TokenError, pair_app_token


class FakeResponse:
    """Svar driver:n `_once`-kontrakt: status, headers.getall, text(), release()."""

    def __init__(self, status=200, text="", headers=None, location=None, set_cookies=None):
        self.status = status
        self._text = text
        h = {}
        for k, v in (headers or {}).items():
            h[k.lower()] = v
        if location is not None:
            h["location"] = location
        if set_cookies:
            h["set-cookie"] = set(set_cookies)
        self.headers = _Headers(h)

    async def text(self):
        return self._text

    def release(self):
        return None

    def close(self):
        return None

    @property
    def url(self):
        import yarl

        return yarl.URL("https://hub.infomentor.se")


class _Headers(dict):
    def getall(self, key, default=None):
        value = super().get(key.lower(), default)
        return list(value) if value else (default or [])

    def get(self, key, default=None):
        value = super().get(key.lower(), None)
        if value is None:
            return default
        if isinstance(value, list):
            return value[0] if value else default
        return value


class MockDriver:
    """Skriptad do_request(method, url, body, headers) -> FakeResponse."""

    def __init__(self, routes):
        self._routes = routes
        self.calls = []

    async def __call__(self, method, url, body=None, headers=None):
        self.calls.append((method, str(url), body))
        for predicate, responder in self._routes:
            if predicate(method, str(url), body):
                return responder(method, str(url), body)
        raise AssertionError(f"oväntat driver-anrop: {method} {url} body={body!r}")




# --------------------------------------------------------------- kedjekonstanter
START = "https://sso.infomentor.se/login.ashx?idp=stockholm_par"
SAML2SSO = "https://login001.stockholm.se/affwebservices/public/saml2sso?SAMLRequest=abc"
# SiteMinders query med tecken som en omkodning skulle förstöra.
AUTH_URL = (
    "https://login003.stockholm.se/NECSadcmbid/authenticate/NECSadcmbid"
    "?TYPE=33554433&REALMOID=06-0a1b%2Dc&TARGET=-SM-HTTPS%3a%2f%2fsso%2einfomentor%2ese%2f"
)
CHOOSER = (
    "<html><body><a href='https://login003.stockholm.se/NECSadc/mbid/b64startpage.jsp"
    "?startpage=https%3A%2F%2Flogin001&amp;x=1'>Mobilt BankID</a></body></html>"
)
ASSERTION = (
    "<html><body onload='document.forms[0].submit()'>"
    "<form method='post' action='https://sso.infomentor.se/login.ashx?idp=stockholm_par'>"
    "<input type='hidden' name='SAMLResponse' value='ENCODED-ASSERT'></form></body></html>"
)


class FakeServer:
    """Skriptad server. `states` är verifyorder-svaren i tur och ordning."""

    def __init__(self, states, *, chooser=CHOOSER, assertion=ASSERTION, poll_status=200):
        self.states = list(states)
        self.chooser = chooser
        self.assertion = assertion
        self.poll_status = poll_status
        self.calls: list[tuple[str, str, dict]] = []
        self.initializes = 0
        self.ok_seen = False

    async def __call__(self, method, url, body=None, headers=None):
        url = str(url)
        self.calls.append((method, url, headers or {}))
        if url == START:
            return FakeResponse(
                302, "", location=SAML2SSO, set_cookies=["ASP.NET_SessionId=s1; Path=/"]
            )
        if url == SAML2SSO:
            return FakeResponse(200, self.chooser)
        if "b64startpage.jsp" in url:
            return FakeResponse(302, "", location=AUTH_URL)
        if url == AUTH_URL and not self.ok_seen:
            return FakeResponse(
                200, "<html>BankID</html>", set_cookies=["PhxAuthN=p1; Path=/"]
            )
        if url == AUTH_URL:
            return FakeResponse(302, "", location="https://login003.stockholm.se/adc")
        if url == "https://login003.stockholm.se/adc":
            return FakeResponse(
                302, "", location="https://login001.stockholm.se/saml2sso-ok",
                set_cookies=["SMSESSION=sm1; Domain=.stockholm.se; Path=/"],
            )
        if url == "https://login001.stockholm.se/saml2sso-ok":
            return FakeResponse(200, self.assertion)
        if "initialize=bankid" in url:
            self.initializes += 1
            payload = {"token": f"tok{self.initializes}", "order": f"ord{self.initializes}",
                       "qrData": f"qr{self.initializes}.0"}
            return FakeResponse(200, json.dumps(payload),
                                headers={"content-type": "application/json;charset=UTF-8"})
        if "verifyorder=" in url:
            state = self.states.pop(0)
            if state == "OK":
                self.ok_seen = True
            payload = {"state": state}
            if state in ("PENDING", "USER_SIGN"):
                payload["qrData"] = f"qr-{len(self.calls)}"
            # SERVER QUIRK: JSON märkt som text/html.
            return FakeResponse(self.poll_status, json.dumps(payload),
                                headers={"content-type": "text/html;charset=UTF-8"})
        raise AssertionError(f"oväntat anrop: {method} {url}")

    def polls(self):
        return [c for c in self.calls if "verifyorder=" in c[1]]

    def initialize_calls(self):
        return [c for c in self.calls if "initialize=bankid" in c[1]]


def _driver():
    return BankIDDriver(poll_interval=0)


class TestBankIDRun(unittest.IsolatedAsyncioTestCase):
    async def test_happy_path_returns_samlresponse(self):
        server = FakeServer(["PENDING", "USER_SIGN", "OK"])
        steps = []

        async def progress(step):
            steps.append((step.state, step.qr_data, step.autostart_url))

        result = await _driver().run(server, progress=progress)

        self.assertEqual(result.saml_response, "ENCODED-ASSERT")
        # Förloppet: QR visas, ändras med varje poll, och OK sist.
        self.assertEqual(steps[0][0], "PENDING")
        self.assertEqual(steps[0][1], "qr1.0")
        self.assertEqual(steps[0][2], "bankid:///?autostarttoken=tok1&redirect=null")
        self.assertEqual(steps[-1][0], "OK")
        self.assertIn("USER_SIGN", [s[0] for s in steps])

    async def test_api_calls_keep_the_siteminder_query_byte_for_byte(self):
        server = FakeServer(["OK"])
        await _driver().run(server)
        init = server.initialize_calls()[0]
        self.assertTrue(init[1].startswith(AUTH_URL + "&initialize=bankid&_="), init[1])
        poll = server.polls()[0]
        self.assertTrue(poll[1].startswith(AUTH_URL + "&verifyorder=ord1&_="), poll[1])
        self.assertEqual(poll[2].get("X-Requested-With"), "XMLHttpRequest")

    async def test_assertion_is_fetched_with_the_bare_page_url_after_ok(self):
        server = FakeServer(["OK"])
        await _driver().run(server)
        after_ok = [c for c in server.calls if c[1] == AUTH_URL]
        self.assertEqual(len(after_ok), 2)  # sidan, sedan intyget (302 efter OK)

    async def test_cookies_accumulate_across_hops(self):
        server = FakeServer(["OK"])
        result = await _driver().run(server)
        last = server.calls[-1][2].get("Cookie", "")
        self.assertIn("SMSESSION=sm1", last)
        self.assertTrue(result.jar.has("ASP.NET_SessionId"))
        self.assertTrue(result.jar.has("PhxAuthN"))

    async def test_unscanned_order_error_restarts_with_a_new_order(self):
        server = FakeServer(["PENDING", "ERROR", "PENDING", "OK"])
        result = await _driver().run(server)
        self.assertEqual(server.initializes, 2)
        self.assertEqual(result.saml_response, "ENCODED-ASSERT")
        self.assertIn("verifyorder=ord2", server.polls()[-1][1])

    async def test_error_after_scan_does_not_restart(self):
        server = FakeServer(["USER_SIGN", "ERROR"])
        with self.assertRaises(BankIDError):
            await _driver().run(server)
        self.assertEqual(server.initializes, 1)

    async def test_restarts_are_limited(self):
        server = FakeServer(["ERROR"] * 10)
        with self.assertRaises(BankIDError):
            await _driver().run(server, max_restarts=2)
        self.assertEqual(server.initializes, 3)

    async def test_cancelled_raises_denied(self):
        server = FakeServer(["USER_SIGN", "CANCELLED"])
        with self.assertRaises(BankIDDenied):
            await _driver().run(server)

    async def test_http_500_poll_is_not_approval(self):
        # Efter CANCELLED svarar servern 500: det får aldrig läsas som godkänt.
        server = FakeServer(["PENDING"], poll_status=500)
        with self.assertRaises(BankIDPageChanged):
            await _driver().run(server)
        self.assertFalse(server.ok_seen)

    async def test_http_302_poll_is_not_approval(self):
        server = FakeServer(["PENDING"], poll_status=302)
        with self.assertRaises(BankIDPageChanged):
            await _driver().run(server)

    async def test_unknown_state_raises(self):
        server = FakeServer(["WAT"])
        with self.assertRaises(BankIDError):
            await _driver().run(server)

    async def test_timeout(self):
        server = FakeServer(["PENDING"] * 5)
        with self.assertRaises(BankIDTimeout):
            await _driver().run(server, timeout=-1)

    async def test_chooser_without_bankid_link_raises(self):
        server = FakeServer(["OK"], chooser="<html><form action='/mbid/start'></form></html>")
        with self.assertRaises(BankIDPageChanged):
            await _driver().run(server)

    async def test_saml_form_for_another_host_is_refused(self):
        evil = ASSERTION.replace("sso.infomentor.se", "evil.example.com")
        server = FakeServer(["OK"], assertion=evil)
        with self.assertRaises(BankIDPageChanged):
            await _driver().run(server)

    async def test_no_samlresponse_after_ok_raises(self):
        server = FakeServer(["OK"], assertion="<html>nothing</html>")
        with self.assertRaises(BankIDPageChanged):
            await _driver().run(server)


class TestPostSamlResponse(unittest.IsolatedAsyncioTestCase):
    async def test_posts_with_the_shared_jar_and_hands_the_token_page_on(self):
        jar = CookieJar()
        jar.set_from_header("ASP.NET_SessionId=s1; Path=/", START)
        seen = {}

        async def do_request(method, url, body=None, headers=None):
            seen.setdefault("calls", []).append((method, str(url), body, headers or {}))
            if method == "POST":
                return FakeResponse(302, "", location="https://hub.infomentor.se/page")
            return FakeResponse(200, "<input name='oauth_token' value='t'>")

        async def fake_submit(do_request, *, request_token_url, headers_for, jar):
            seen["url"] = request_token_url
            jar.set_from_header("IMHome=1; Path=/", request_token_url)
            return jar

        with patch(
            "custom_components.infomentor.token_client._oauth_submit_to_callback", fake_submit
        ):
            out = await _driver().post_saml_response(
                do_request, saml_response="a+b/c=", jar=jar
            )

        method, url, body, headers = seen["calls"][0]
        self.assertEqual((method, url), ("POST", START))
        self.assertEqual(body, "SAMLResponse=a%2Bb%2Fc%3D")
        self.assertIn("ASP.NET_SessionId=s1", headers["Cookie"])
        self.assertEqual(seen["url"], "https://hub.infomentor.se/page")
        self.assertTrue(out.has("IMHome"))


class TestPairAppToken(unittest.IsolatedAsyncioTestCase):
    """Webbsession -> appens refresh-token: GetAuthenticationData -> LoginOAuth2 -> Token."""

    GAD = json.dumps(
        {"authenticationUrl": "https://im.infomentor.se/x?authGuid=G-1&y=2", "apiUrl": "u"}
    )

    def _server(self, *, code="a+b/c==", gad_status=200, auth_status=302, token_status=200):
        calls = []

        async def do_request(method, url, body=None, headers=None):
            url = str(url)
            calls.append((method, url, body, headers or {}))
            if "GetAuthenticationData" in url:
                return FakeResponse(gad_status, f"<pre>{self.GAD}</pre>")
            if "LoginOAuth2" in url:
                loc = f"InfomentorNotification://oauth2Callback/?code={code}"
                return FakeResponse(auth_status, "", location=loc if auth_status == 302 else None)
            return FakeResponse(
                token_status,
                json.dumps({"access_token": "a", "refresh_token": "rt", "expires_in": 60}),
            )

        return do_request, calls

    async def test_pairs_and_sends_the_code_unencoded(self):
        jar = CookieJar()
        jar.set_from_header("IMHome=1; Domain=.infomentor.se; Path=/", "https://hub.infomentor.se")
        do_request, calls = self._server()
        token = await pair_app_token(do_request, jar, device_identifier="0123456789abcdef")
        self.assertEqual(token["refresh_token"], "rt")
        gad, auth, tok = calls
        self.assertIn("IMHome=1", gad[3]["Cookie"])
        self.assertIn("authGuid=G-1", auth[1])
        self.assertIn("DeviceIdentifier=0123456789abcdef", auth[1])
        # Koden skickas OKODAD (Retrofit encoded=true); omkodning ger HTTP 400.
        self.assertTrue(tok[2].startswith("code=a+b/c=="), tok[2])
        self.assertIn("client_secret=NONE", tok[2])

    async def test_no_code_raises(self):
        do_request, _ = self._server(auth_status=200)
        with self.assertRaises(TokenError):
            await pair_app_token(do_request, CookieJar(), device_identifier="0" * 16)

    async def test_rejected_web_session_raises(self):
        do_request, _ = self._server(gad_status=302)
        with self.assertRaises(TokenError):
            await pair_app_token(do_request, CookieJar(), device_identifier="0" * 16)

    async def test_token_without_refresh_token_raises(self):
        do_request, _ = self._server(token_status=400)
        with self.assertRaises(TokenError):
            await pair_app_token(do_request, CookieJar(), device_identifier="0" * 16)


class TestHelpers(unittest.TestCase):
    def test_extract_saml_response_form(self):
        value = BankIDDriver._extract_saml_response_form(ASSERTION, "https://x")
        self.assertEqual(value, "ENCODED-ASSERT")

    def test_extract_saml_response_form_none_without_login_ashx(self):
        self.assertIsNone(BankIDDriver._extract_saml_response_form("<html>x</html>", "https://x"))

    def test_urlencode_saml_response_escapes_reserved_chars(self):
        encoded = BankIDDriver._urlencode_saml_response("a+b/c=")
        self.assertEqual(encoded, "SAMLResponse=a%2Bb%2Fc%3D")


if __name__ == "__main__":
    unittest.main()

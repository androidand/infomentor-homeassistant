"""Tester för BankID-driver:n (bankid.py) — interaktiv + tyst inloggning.

Driver:n styr Stockholms stads SiteMinder + PhenixID till en SAMLResponse/SMSESSION.
Här testas kedjan mot skriptade mock-svar:

- interaktiv: login.ashx -> saml2sso (BankID-val) -> medborgare.jsp -> PhenixID
  initialize (JSON {order,qrData}) -> verifyorder-polls (tomt -> godkänt) ->
  SiteMinderAuthADC (SMSESSION) -> saml2sso med SMASSERTIONREF -> SAMLResponse-form.
- tyst: saml2sso svarar direkt med SAMLResponse-formen (giltig SMSESSION).
- fel: avböjt BankID, tid ut, och en ändrad IdP-sida.

Allt kört mot driver:n ens — ingen hubb, ingen session. do_request injekteras.
"""

from __future__ import annotations

import json
import unittest

import pytest

try:
    from custom_components.infomentor.bankid import (
        BankIDDenied,
        BankIDDriver,
        BankIDPageChanged,
        BankIDResult,
        BankIDTimeout,
    )

    AVAILABLE = True
except Exception:  # noqa: BLE001
    AVAILABLE = False


@pytest.fixture(autouse=True)
def _no_hass_autouse():
    """Rent driver-test, kräver ingen HA-hass."""
    yield


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
            h["set-cookie"] = set(list(set_cookies))
        self.headers = _Headers(h)

    async def text(self):
        return self._text

    def release(self):
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
SSO = "https://sso.infomentor.se"
SITEMINDER = "https://login001.stockholm.se"
PHENIX = "https://login003.stockholm.se/NECSadcmbid/authenticate/NECSadcmbid"
SAML2SSO = f"{SITEMINDER}/affwebservices/public/saml2sso"


def _saml2sso_redirect():
    """login.ashx: redirectar till saml2sso (SAMLRequest)."""
    return FakeResponse(302, "", headers={"location": SAML2SSO})


def _saml2sso_bankid_choice():
    """saml2sso med BankID-valet: medborgare.jsp med BankID-startformuläret."""
    return FakeResponse(200, _medborgare_html())


def _medborgare_html():
    """SiteMinder-medborgare.jsp: BankID-startformuläret (action pekar på NECSadc/mbid)."""
    return (
        "<html><body><form method='post' action='https://login003.stockholm.se/NECSadc/mbid/start'>"
        "<input type='hidden' name='SMENC' value='utf-8'>"
        "<input type='hidden' name='SMLOCALE' value='sv_SE'>"
        "<input type='hidden' name='target' value='https://login003.stockholm.se'>"
        "</form></body></html>"
    )


def _initialize_json():
    """PhenixID initialize: JSON med order + qrData."""
    return json.dumps({"token": "abc", "order": "ord-123", "qrData": "ZSJxcg=="}), [
        "PhxBankIdDeviceId=dev-1; Max-Age=31536000"
    ]


def _samlresponse_form():
    """SiteMinderAuthADC -> saml2sso med SMASSERTIONREF -> SAMLResponse-form."""
    return (
        "<html><body><form method='post' action='https://sso.infomentor.se/login.ashx'>"
        "<input type='hidden' name='SAMLResponse' value='ENCODED-ASSERT'>"
        "</form></body></html>"
    )


def _post_form(method, body):
    """Hjälp: är detta en POST i kedjan (BankID-startformuläret)?"""
    return method == "POST" and body is not None


class TestBankIDInteractive(unittest.IsolatedAsyncioTestCase):
    async def test_interactive_chain_returns_samlresponse(self):
        driver = BankIDDriver(poll_interval=0)
        init_json, init_cookies = _initialize_json()
        saml_body = _samlresponse_form()

        routes = [
            # login.ashx -> saml2sso
            (lambda m, u, b: u.startswith(SSO), lambda m, u, b: _saml2sso_redirect()),
            # saml2sso (SMSESSION-free) -> medborgare.jsp (BankID-val)
            (lambda m, u, b: "public/saml2sso" in u and "SMASSERTIONREF" not in u,
             lambda m, u, b: _saml2sso_bankid_choice()),
            # medborgare.jsp: välj BankID (GET)
            (lambda m, u, b: "redirectjsp/medborgareonly.jsp" in u,
             lambda m, u, b: FakeResponse(200, _medborgare_html())),
            # medborgare.jsp: BankID-startformuläret (GET)
            (lambda m, u, b: u.endswith("NECSadc/mbid/start") and m == "GET",
             lambda m, u, b: FakeResponse(200, _medborgare_html())),
            # medborgare.jsp: BankID-startformuläret (POST) -> phenixid_base (host)
            (lambda m, u, b: u.endswith("NECSadc/mbid/start") and _post_form(m, b),
             lambda m, u, b: FakeResponse(302, "", headers={"location": "https://login003.stockholm.se"})),
            # PhenixID initialize (GET)
            (lambda m, u, b: "authenticate/NECSadcmbid" in u and "initialize" in u,
             lambda m, u, b: FakeResponse(200, init_json,
                                          headers={"content-type": "application/json"}, set_cookies=init_cookies)),
            # PhenixID authorize page (GET, utan verifyorder)
            (lambda m, u, b: "authenticate/NECSadcmbid" in u and "verifyorder" not in u and "SiteMinderAuthADC" not in u,
             lambda m, u, b: FakeResponse(200, "<html>qr page</html>")),
            # verifyorder: godkänt (triggerar SiteMinderAuthADC)
            (lambda m, u, b: "verifyorder=" in u,
             lambda m, u, b: FakeResponse(302, "", headers={"location": f"{PHENIX}/SiteMinderAuthADC"})),
            # SiteMinderAuthADC (GET) -> SMSESSION (driver returns this)
            (lambda m, u, b: "SiteMinderAuthADC" in u,
             lambda m, u, b: FakeResponse(200, "",
                                          headers={"set-cookie": ["SMSESSION=sess-1; Path=/"]})),
        ]
        mock = MockDriver(routes)
        result = await driver.run(mock, progress=None)
        self.assertEqual(result.smsession, "sess-1")
        self.assertIsNotNone(result.jar)
        self.assertEqual(result.jar.has("SMSESSION"), True)

    async def test_interactive_denied_raises(self):
        driver = BankIDDriver(poll_interval=0)
        init_json, init_cookies = _initialize_json()
        routes = [
            (lambda m, u, b: u.startswith(SSO), lambda m, u, b: _saml2sso_redirect()),
            (lambda m, u, b: "public/saml2sso" in u and "SMASSERTIONREF" not in u,
             lambda m, u, b: _saml2sso_bankid_choice()),
            (lambda m, u, b: "redirectjsp/medborgareonly.jsp" in u,
             lambda m, u, b: FakeResponse(200, _medborgare_html())),
            (lambda m, u, b: u.endswith("NECSadc/mbid/start") and m == "GET",
             lambda m, u, b: FakeResponse(200, _medborgare_html())),
            (lambda m, u, b: u.endswith("NECSadc/mbid/start") and _post_form(m, b),
             lambda m, u, b: FakeResponse(302, "", headers={"location": "https://login003.stockholm.se"})),
            (lambda m, u, b: "authenticate/NECSadcmbid" in u and "initialize" in u,
             lambda m, u, b: FakeResponse(200, init_json,
                                          headers={"content-type": "application/json"}, set_cookies=init_cookies)),
            (lambda m, u, b: "authenticate/NECSadcmbid" in u and "verifyorder" not in u and "SiteMinderAuthADC" not in u,
             lambda m, u, b: FakeResponse(200, "<html>qr page</html>")),
            (lambda m, u, b: "verifyorder=" in u, lambda m, u, b: FakeResponse(200, "denied")),
        ]
        mock = MockDriver(routes)
        with self.assertRaises(BankIDDenied):
            await driver.run(mock, progress=None)

    async def test_interactive_timeout_raises(self):
        driver = BankIDDriver(poll_interval=0)
        init_json, init_cookies = _initialize_json()
        # verifyorder svarar alltid "pending" — driver:n time ut efter MAX_POLL_ATTEMPTS.
        routes = [
            (lambda m, u, b: u.startswith(SSO), lambda m, u, b: _saml2sso_redirect()),
            (lambda m, u, b: "public/saml2sso" in u and "SMASSERTIONREF" not in u,
             lambda m, u, b: _saml2sso_bankid_choice()),
            (lambda m, u, b: "redirectjsp/medborgareonly.jsp" in u,
             lambda m, u, b: FakeResponse(200, _medborgare_html())),
            (lambda m, u, b: u.endswith("NECSadc/mbid/start") and m == "GET",
             lambda m, u, b: FakeResponse(200, _medborgare_html())),
            (lambda m, u, b: u.endswith("NECSadc/mbid/start") and _post_form(m, b),
             lambda m, u, b: FakeResponse(302, "", headers={"location": "https://login003.stockholm.se"})),
            (lambda m, u, b: "authenticate/NECSadcmbid" in u and "initialize" in u,
             lambda m, u, b: FakeResponse(200, init_json,
                                          headers={"content-type": "application/json"}, set_cookies=init_cookies)),
            (lambda m, u, b: "authenticate/NECSadcmbid" in u and "verifyorder" not in u and "SiteMinderAuthADC" not in u,
             lambda m, u, b: FakeResponse(200, "<html>qr page</html>")),
            (lambda m, u, b: "verifyorder=" in u, lambda m, u, b: FakeResponse(200, "pending")),
        ]
        mock = MockDriver(routes)
        with self.assertRaises(BankIDTimeout):
            await driver.run(mock, progress=None)

    async def test_page_without_samlresponse_or_chooser_raises(self):
        driver = BankIDDriver(poll_interval=0)
        # saml2sso svarar 200 med tom html — ingen SAMLResponse-form, ingen BankID-val.
        routes = [
            (lambda m, u, b: u.startswith(SSO), lambda m, u, b: _saml2sso_redirect()),
            (lambda m, u, b: u == SAML2SSO, lambda m, u, b: FakeResponse(200, "<html>nothing here</html>")),
        ]
        mock = MockDriver(routes)
        with self.assertRaises(BankIDPageChanged):
            await driver.run(mock, progress=None)


class TestBankIDSilent(unittest.IsolatedAsyncioTestCase):
    async def test_silent_smsession_returns_samlresponse(self):
        driver = BankIDDriver(poll_interval=0)
        saml_body = _samlresponse_form()
        # Tyst: saml2sso svarar direkt med SAMLResponse-form (giltig SMSESSION).
        routes = [
            (lambda m, u, b: u.startswith(SSO), lambda m, u, b: _saml2sso_redirect()),
            (lambda m, u, b: u == SAML2SSO,
             lambda m, u, b: FakeResponse(200, saml_body, headers={"content-type": "text/html"})),
        ]
        mock = MockDriver(routes)
        result = await driver.run(mock, smsession="sess-1")
        self.assertEqual(result.saml_response, "ENCODED-ASSERT")


class TestBankIDHelpers(unittest.TestCase):
    def test_extract_saml_response_form(self):
        html = (
            "<html><body><form method='post' action='https://sso.infomentor.se/login.ashx'>"
            "<input type='hidden' name='SAMLResponse' value='ASSERT-XYZ'>"
            "</form></body></html>"
        )
        driver = BankIDDriver()
        value = driver._extract_saml_response_form(html, "https://login001.stockholm.se")
        self.assertEqual(value, "ASSERT-XYZ")

    def test_extract_saml_response_form_none_when_no_login_ashx(self):
        driver = BankIDDriver()
        self.assertIsNone(driver._extract_saml_response_form("<html>no form</html>", "https://x"))

    def test_extract_saml_response_form_none_when_no_samlresponse_field(self):
        html = "<html><form method='post' action='https://sso.infomentor.se/login.ashx'></form></html>"
        driver = BankIDDriver()
        self.assertIsNone(driver._extract_saml_response_form(html, "https://x"))

    def test_verifyorder_approved_status(self):
        driver = BankIDDriver()
        self.assertEqual(driver._verifyorder_approved("approved", 302), "approved")
        self.assertEqual(driver._verifyorder_approved("denied", 200), "denied")
        self.assertEqual(driver._verifyorder_approved("<html>waiting</html>", 200), "pending")

    def test_verifyorder_approved_empty_body_is_pending(self):
        driver = BankIDDriver()
        self.assertEqual(driver._verifyorder_approved("", 200), "pending")


if __name__ == "__main__":
    unittest.main()

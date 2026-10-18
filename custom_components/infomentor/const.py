"""Konstanter för InfoMentor-integrationen."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "infomentor"

MANUFACTURER: Final = "InfoMentor"
MODEL: Final = "Skolplattform"

# Inställningar
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_ENABLE_LUNCH: Final = "enable_lunch"
CONF_MATEO_UNIT: Final = "mateo_unit_id"
CONF_NAMES: Final = "names"
CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_AUTH_MODE: Final = "auth_mode"

DEFAULT_SCAN_INTERVAL_MIN: Final = 20
MIN_SCAN_INTERVAL_MIN: Final = 5
MAX_SCAN_INTERVAL_MIN: Final = 180
DEFAULT_SCAN_INTERVAL: Final = timedelta(minutes=DEFAULT_SCAN_INTERVAL_MIN)

# Ändpunkter
HUB_BASE: Final = "https://hub.infomentor.se"
MENTOR_LOGIN: Final = "https://infomentor.se/swedish/production/mentor/"
MATEO_API: Final = "https://meny-api.mateo.se/api/v1/days"

# BankID via Stockholms stad (SiteMinder + PhenixID). SSO-starten pekar på den
# kommun-specifika IdP-valsidan; `idp` skiljer sig mellan skolor och vissa är
# inte SiteMinder alls (docs/idp-federation.json).
SSO_LOGIN_PATH: Final = "/login.ashx"
IDP_STOCKHOLM_PAR: Final = "stockholm_par"
SSO_BASE: Final = "https://sso.infomentor.se"
PHENIXID_BASE: Final = "https://login003.stockholm.se"
SITEMINDER_BASE: Final = "https://login001.stockholm.se"

ATTR_PUPILS: Final = "pupils"
ATTR_ATTRIBUTION: Final = "Data från InfoMentor"


def clamp_interval(minutes: int) -> int:
    """Håller uppdateringsintervallet inom tillåtna gränser."""
    return max(MIN_SCAN_INTERVAL_MIN, min(MAX_SCAN_INTERVAL_MIN, int(minutes)))

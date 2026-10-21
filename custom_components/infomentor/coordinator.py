"""DataUpdateCoordinator: loggar in och hämtar allt för alla barn."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from time import monotonic
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    ApiError,
    CannotConnect,
    InfomentorApi,
    InfomentorError,
    InvalidAuth,
)
from .const import (
    CONF_ENABLE_LUNCH,
    CONF_MATEO_UNIT,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    DEFAULT_SCAN_INTERVAL_MIN,
    DOMAIN,
    HUB_BASE,
    clamp_interval,
)
from .probe import Outcome
from .util import (
    dedupe_plans,
    display_name,
    normalize_attendance,
    normalize_calendar,
    normalize_fritids,
    normalize_learnlog,
    normalize_lessons,
    normalize_news,
    normalize_notifications,
    normalize_plan_detail,
    normalize_plan_tasks,
    normalize_plans,
    normalize_tasks,
    parse_mateo_days,
    parse_mateo_unit,
    plan_with_detail,
    weekly_letter_from_news,
)

_LOGGER = logging.getLogger(__name__)

# En planeringsdetalj är ett anrop per planering. Den ändras sällan, så vi
# hämtar den för öppna planeringar och minns den i ett dygn.
PLAN_DETAIL_TTL_HOURS = 24


@dataclass(slots=True)
class PupilData:
    """Allt vi vet om ett barn vid en hämtning."""

    pupil_id: str
    name: str
    switch_url: str
    display_name: str
    lessons: list[dict[str, Any]] = field(default_factory=list)
    calendar: list[dict[str, Any]] = field(default_factory=list)
    fritids: list[dict[str, Any]] = field(default_factory=list)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    attendance: dict[str, Any] = field(default_factory=dict)
    learnlog: list[dict[str, Any]] = field(default_factory=list)
    plans: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class InfomentorData:
    """Koordinatorns data."""

    pupils: list[PupilData] = field(default_factory=list)
    notifications: list[dict[str, Any]] = field(default_factory=list)
    news: list[dict[str, Any]] = field(default_factory=list)
    weekly_letter: dict[str, Any] | None = None
    lunch: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    lunch_unit: str | None = None


class InfomentorCoordinator(DataUpdateCoordinator[InfomentorData]):
    """Hämtar data med valt intervall."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, session: aiohttp.ClientSession
    ) -> None:
        self.entry = entry
        self._lock = asyncio.Lock()
        data = entry.data
        self.api = InfomentorApi(session, data.get(CONF_USERNAME), data.get(CONF_PASSWORD))
        self._ok_calls = 0
        self._auth_skips = 0
        # Delar hubben redan avvisat medan sessionen levde (se _safe); loggas bara en gång.
        self._not_permitted: set[str] = set()
        # planerings-id -> {"at": monotonic(), "info": {...}}
        self._plan_details: dict[str, dict[str, Any]] = {}
        self._plan_ids_seen: set[str] = set()
        minutes = int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MIN))
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=clamp_interval(minutes)),
            config_entry=entry,
        )

    async def _async_update_data(self) -> InfomentorData:
        pupils = await self._login()
        try:
            return await self._fetch_all(pupils, strict=True)
        except InvalidAuth as err:
            # Sessionen kan svalna direkt efter inloggningen (nod-affinitet eller
            # kort livstid). Logga in en gång till och försök om innan vi besvärar
            # användaren med en reauth. På andra försöket hoppar vi över enskilda
            # endpoints som avvisas i stället för att fälla allt.
            _LOGGER.warning("InfoMentor: %s – loggar in igen och försöker om", err)
            pupils = await self._login()
            try:
                return await self._fetch_all(pupils, strict=False)
            except InvalidAuth as err2:
                # Inloggningen FUNGERAR men hubben avvisar anropen. Då är det inte
                # fel lösenord – låt HA försöka igen i stället för att tjata om
                # reauth (som ändå skulle lyckas och falla igen).
                raise UpdateFailed(f"Hubben avvisade anropen: {err2}") from err2
            except CannotConnect as err2:
                raise UpdateFailed(f"Kunde inte nå InfoMentor: {err2}") from err2

    async def _login(self) -> list[dict[str, Any]]:
        """Logga in med BankID-token om posten har en, annars med e-post och lösenord.

        Fel uppgifter eller död token: reauth. Nere eller blockerad: försök igen, aldrig reauth.
        """
        try:
            if CONF_REFRESH_TOKEN in self.entry.data:
                return await self._login_with_token()
            return await self.api.async_login()
        except InvalidAuth as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except CannotConnect as err:
            raise UpdateFailed(f"Kunde inte nå InfoMentor: {err}") from err

    async def _login_with_token(self) -> list[dict[str, Any]]:
        # Den roterande token har en enda ägare. Läs den inne i låset: en andra inloggning
        # ska se den redan roterade, inte den förbrukade.
        async with self._lock:
            return await self.api.async_login_with_token(
                self.entry.data[CONF_REFRESH_TOKEN], on_rotated=self._store_token
            )

    def _store_token(self, token: str) -> None:
        """Skriv den roterade token innan något mer kan misslyckas, annars är nästa refresh död."""
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, CONF_REFRESH_TOKEN: token}
        )

    async def _fetch_all(
        self, pupils: list[dict[str, Any]], *, strict: bool = True
    ) -> InfomentorData:
        data = InfomentorData()
        self._begin_round()
        child_by_id: dict[str, str] = {str(p.get("id")): str(p.get("name")) for p in pupils}

        for pupil in pupils:
            pupil_id = str(pupil.get("id"))
            who = display_name(str(pupil.get("name") or ""))

            # Själva barnbytet måste lyckas — annars blir allt fel.
            try:
                await self.api.async_switch_pupil(pupil)
            except InvalidAuth:
                raise
            except CannotConnect:
                raise
            except InfomentorError as err:
                raise UpdateFailed(f"Kunde inte byta till {who}: {err}") from err

            # Enskilda endpoints får fallera utan att fälla hela uppdateringen
            # (t.ex. en kommun där en endpoint svarar oväntat).
            lessons = normalize_lessons(
                await self._safe(
                    f"schema ({who})", self.api.async_lessons(pupil), [], strict=strict
                )
            )
            calendar = normalize_calendar(
                await self._safe(
                    f"kalender ({who})", self.api.async_calendar(pupil), [], strict=strict
                )
            )
            # Inte alla barn har fritids. Vi frågar, minns svaret och frågar sällan om vi vet att
            # barnet saknar det (se probe.py); ett fel säger inget och prövas om med uppehåll.
            fritids = normalize_fritids(
                await self._probed(
                    f"fritids:{pupil_id}",
                    f"fritids ({who})",
                    lambda pupil=pupil: self.api.async_fritids(pupil),
                    {},
                    normalize_fritids,
                )
            )
            tasks = normalize_tasks(
                await self._safe(
                    f"uppgifter ({who})", self.api.async_tasks(pupil), [], strict=strict
                )
            )
            attendance = normalize_attendance(
                await self._safe(
                    f"närvaro ({who})", self.api.async_attendance(pupil), {}, strict=strict
                )
            )
            learnlog = normalize_learnlog(
                await self._safe(
                    f"lärlogg ({who})", self.api.async_learnlog(pupil), {}, strict=strict
                )
            )
            plans = normalize_plans(
                await self._safe(
                    f"planeringar ({who})", self.api.async_plans(pupil), {}, strict=strict
                )
            )
            plans = await self._with_plan_details(plans, strict=strict)

            data.pupils.append(
                PupilData(
                    pupil_id=pupil_id,
                    name=str(pupil.get("name") or ""),
                    switch_url=str(pupil.get("switchPupilUrl") or ""),
                    display_name=who,
                    lessons=lessons,
                    calendar=calendar,
                    fritids=fritids,
                    tasks=tasks,
                    attendance=attendance,
                    learnlog=learnlog,
                    plans=plans,
                )
            )

        notifications = await self._safe(
            "notiser", self.api.async_notifications(), [], strict=strict
        )
        data.notifications = normalize_notifications(notifications, child_by_id)
        news = await self._safe("nyheter", self.api.async_news(), [], strict=strict)
        data.news = normalize_news(news)
        data.weekly_letter = weekly_letter_from_news(news, HUB_BASE)

        # Icke-strikt läge (efter en ny inloggning): om INGET anrop gick igenom
        # är sessionen ändå död — säg till i stället för att visa tom data.
        if not strict and self._ok_calls == 0 and self._auth_skips:
            raise InvalidAuth("inga hubb-anrop accepterades efter ny inloggning")

        if self.entry.options.get(CONF_ENABLE_LUNCH) and (
            unit := parse_mateo_unit(self.entry.options.get(CONF_MATEO_UNIT))
        ):
            days = await self._safe("skolmat", self.api.async_lunch(unit), [])
            data.lunch = parse_mateo_days(days)
            data.lunch_unit = unit

        self._prune_plan_details()
        self._settle_probes()
        return data

    def _begin_round(self) -> None:
        self._ok_calls = 0
        self._auth_skips = 0
        self._plan_ids_seen = set()
        # Svar från en omgång som föll (död session) får inte leva vidare.
        self._probe.discard()

    def _settle_probes(self) -> None:
        """Det vi lärt oss om valfria delar gäller bara om sessionen levde.

        Gick inget igenom kan ett "nej" lika gärna vara en död session.
        """
        if self._ok_calls:
            self._probe.commit()
        else:
            self._probe.discard()

    async def _with_plan_details(
        self, plans: list[dict[str, Any]], *, strict: bool
    ) -> list[dict[str, Any]]:
        """Fyller på öppna planeringar med period, lärare och uppgifter.

        Detaljen kostar ett anrop per planering (och ett till för uppgifterna)
        och ändras sällan, så den cachas i `PLAN_DETAIL_TTL_HOURS`. Avslutade
        planeringar visas ändå inte, så de får ingen detalj.
        """
        out: list[dict[str, Any]] = []
        now = monotonic()
        for plan in plans:
            info: dict[str, Any] | None = None
            if plan["state"] != "finished":
                cached = self._plan_details.get(plan["id"])
                if cached is None or now - cached["at"] > PLAN_DETAIL_TTL_HOURS * 3600:
                    detail = await self._safe(
                        f"planeringsdetalj ({plan['title'] or plan['id']})",
                        self.api.async_plan_detail(plan["id"]),
                        {},
                        strict=strict,
                    )
                    # Tomt svar = inte cacha, så vi försöker igen nästa gång.
                    if detail:
                        info = normalize_plan_detail(detail)
                        # Uppgifterna bär själva provet/läxan; här behövs bara
                        # kopplingen till planeringen, så ett fel får inte fälla
                        # hela uppdateringen.
                        assignments = normalize_plan_tasks(
                            await self._safe(
                                f"planeringsuppgifter ({plan['title'] or plan['id']})",
                                self.api.async_plan_tasks(plan["id"]),
                                {},
                                strict=False,
                            )
                        )
                        if assignments:
                            info["assignments"] = assignments
                        self._plan_details[plan["id"]] = {"at": now, "info": info}
                else:
                    info = cached["info"]
            self._plan_ids_seen.add(plan["id"])
            out.append(plan_with_detail(plan, info))
        return dedupe_plans(out)

    def _prune_plan_details(self) -> None:
        """Släpper detaljer för planeringar som inte längre finns.

        Körs en gång per uppdatering — inte per barn, annars skulle varje barns
        körning kasta de andras cachade detaljer.
        """
        for plan_id in [key for key in self._plan_details if key not in self._plan_ids_seen]:
            del self._plan_details[plan_id]

    async def _probed(self, key: str, label: str, call, default, normalize):
        """Hämtar en valfri del om det är dags, och minns om barnet har den.

        Aldrig en ny inloggning för en sådan del: en död session märks på de andra
        anropen, och ett "nej" gäller först när omgången lyckats (se `_fetch_all`).
        CannotConnect bubblar upp som alltid.
        """
        # Uppehåll eller ett fel ska inte tömma det vi redan har.
        fallback = self._last_good.get(key, default)
        if not self._probe.due(key):
            return fallback
        try:
            value = await call()
        except CannotConnect:
            raise
        except InvalidAuth as err:
            _LOGGER.debug("InfoMentor: %s avvisades (%s) – delen saknas här", label, err)
            self._probe.record(key, Outcome.REFUSED)
            self._last_good.pop(key, None)
            return default
        except (ApiError, InfomentorError) as err:
            _LOGGER.debug("InfoMentor: kunde inte hämta %s – försöker igen senare (%s)", label, err)
            self._probe.record(key, Outcome.ERROR)
            return fallback
        self._ok_calls += 1
        self._last_good[key] = value
        self._probe.record(key, Outcome.DATA if normalize(value) else Outcome.EMPTY)
        return value

    async def _safe(self, label: str, awaitable, default, *, strict: bool = True):
        """Hämtar en endpoint; loggar och hoppar över vid endpoint-fel.

        I strikt läge (första försöket) bubblar InvalidAuth upp så vi kan logga in
        igen. I icke-strikt läge (efter ny inloggning) hoppas enskilda avvisade
        endpoints över — men går inget alls igenom fångas det av anroparen.
        CannotConnect bubblar alltid upp (värden är nere).
        """
        try:
            value = await awaitable
        except CannotConnect:
            raise
        except InvalidAuth as err:
            if strict and self._ok_calls == 0:
                # Inget har gått igenom ännu: sessionen kan vara död, logga in igen.
                raise
            # Andra anrop i samma omgång gick igenom, så sessionen lever och just den här
            # delen är inte tillåten för användaren (alla barn och skolor har inte alla
            # funktioner). Hoppa över den; en ny inloggning hjälper inte.
            level = logging.DEBUG if label in self._not_permitted else logging.WARNING
            _LOGGER.log(level, "InfoMentor: %s är inte tillåten här – hoppar över (%s)", label, err)
            self._not_permitted.add(label)
            self._auth_skips += 1
            return default
        except (ApiError, InfomentorError) as err:
            _LOGGER.warning("InfoMentor: kunde inte hämta %s – hoppar över (%s)", label, err)
            return default
        self._ok_calls += 1
        return value

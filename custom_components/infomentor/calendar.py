"""Kalender: skolans kalender som en HA-kalender per barn."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import InfomentorCoordinator, PupilData
from .entity import InfomentorPupilEntity, add_entities_as_they_appear
from .util import calendar_span, fritids_calendar_entries


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: InfomentorCoordinator = entry.runtime_data.coordinator
    pupils = coordinator.data.pupils if coordinator.data else []
    entities: list[PupilCalendar] = []
    for pupil in pupils:
        entities.append(PupilCalendar(coordinator, entry, pupil))
        entities.append(LessonsCalendar(coordinator, entry, pupil))
    async_add_entities(entities)
    add_entities_as_they_appear(
        entry,
        coordinator,
        async_add_entities,
        lambda pupil: [FritidsCalendar(coordinator, entry, pupil)] if pupil.fritids else []
    )


def _localized(value: datetime | object) -> datetime | object:
    """Hubbens klockslag är lokala utan zon; en HA-kalender vill ha zon."""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    return value


class PupilCalendar(InfomentorPupilEntity, CalendarEntity):
    """Barnets kalender. Hubben ger de närmaste 30 dagarna, så äldre poster saknas."""

    _attr_translation_key = "calendar"

    def __init__(
        self,
        coordinator: InfomentorCoordinator,
        entry: ConfigEntry,
        pupil: PupilData,
        key: str = "calendar",
    ) -> None:
        super().__init__(coordinator, entry, pupil, key)

    def _entries(self) -> list[dict]:
        pupil = self.pupil
        return pupil.calendar if pupil else []

    def _events(self) -> list[CalendarEvent]:
        events = []
        for entry in self._entries():
            span = calendar_span(entry)
            if span is None:
                continue
            events.append(
                CalendarEvent(
                    start=_localized(span[0]),
                    end=_localized(span[1]),
                    summary=entry["title"] or "–",
                    description=entry.get("subjects") or entry.get("teachers") or None,
                    location=entry.get("room") or None,
                    uid=entry.get("id") or None,
                )
            )
        return sorted(events, key=lambda e: (e.start_datetime_local, e.end_datetime_local))

    @property
    def event(self) -> CalendarEvent | None:
        now = dt_util.now()
        return next((e for e in self._events() if e.end_datetime_local > now), None)

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        return [
            e
            for e in self._events()
            if e.start_datetime_local < end_date and e.end_datetime_local > start_date
        ]


class FritidsCalendar(PupilCalendar):
    """Fritidsschemat: tider för lämning och hämtning, ledighet och stängda dagar.

    Hubben visar bara veckan som är aktuell, så kalendern har den veckans dagar.
    """

    _attr_translation_key = "fritids_calendar"

    def __init__(
        self, coordinator: InfomentorCoordinator, entry: ConfigEntry, pupil: PupilData
    ) -> None:
        super().__init__(coordinator, entry, pupil, "fritids_calendar")

    def _entries(self) -> list[dict]:
        pupil = self.pupil
        return fritids_calendar_entries(pupil.fritids) if pupil else []


class LessonsCalendar(PupilCalendar):
    """Skolschemat: lektionerna en vecka bakåt och en månad framåt."""

    _attr_translation_key = "schedule_calendar"

    def __init__(
        self, coordinator: InfomentorCoordinator, entry: ConfigEntry, pupil: PupilData
    ) -> None:
        super().__init__(coordinator, entry, pupil, "schedule_calendar")

    def _entries(self) -> list[dict]:
        pupil = self.pupil
        return pupil.lessons if pupil else []

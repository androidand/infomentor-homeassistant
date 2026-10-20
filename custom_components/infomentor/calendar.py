"""Kalender: skolans kalender som en HA-kalender per barn."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import InfomentorCoordinator, PupilData
from .entity import InfomentorPupilEntity
from .util import calendar_span


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: InfomentorCoordinator = entry.runtime_data.coordinator
    pupils = coordinator.data.pupils if coordinator.data else []
    async_add_entities(PupilCalendar(coordinator, entry, pupil) for pupil in pupils)


def _localized(value: datetime | object) -> datetime | object:
    """Hubbens klockslag är lokala utan zon; en HA-kalender vill ha zon."""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    return value


class PupilCalendar(InfomentorPupilEntity, CalendarEntity):
    """Barnets kalender. Hubben ger de närmaste 30 dagarna, så äldre poster saknas."""

    _attr_translation_key = "calendar"

    def __init__(
        self, coordinator: InfomentorCoordinator, entry: ConfigEntry, pupil: PupilData
    ) -> None:
        super().__init__(coordinator, entry, pupil, "calendar")

    def _events(self) -> list[CalendarEvent]:
        pupil = self.pupil
        events = []
        for entry in pupil.calendar if pupil else []:
            span = calendar_span(entry)
            if span is None:
                continue
            events.append(
                CalendarEvent(
                    start=_localized(span[0]),
                    end=_localized(span[1]),
                    summary=entry["title"] or "–",
                    description=entry["subjects"] or None,
                    uid=entry["id"] or None,
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

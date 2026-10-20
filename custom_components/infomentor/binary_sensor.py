"""Binära sensorer: idrott nästa skoldag."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import InfomentorCoordinator, PupilData
from .entity import InfomentorPupilEntity
from .util import is_absent, lessons_on, next_school_day, pe_lessons, time_of


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: InfomentorCoordinator = entry.runtime_data.coordinator
    data = coordinator.data
    entities: list[BinarySensorEntity] = []
    if data is not None:
        for pupil in data.pupils:
            entities.append(PESensor(coordinator, entry, pupil))
            entities.append(AbsentTodaySensor(coordinator, entry, pupil))
    async_add_entities(entities)


class PESensor(InfomentorPupilEntity, BinarySensorEntity):
    """Tänder om barnet har idrott/gymnastik nästa skoldag."""

    _attr_should_poll = False
    _attr_translation_key = "pe_next_school_day"
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:run"

    def __init__(
        self, coordinator: InfomentorCoordinator, entry: ConfigEntry, pupil: PupilData
    ) -> None:
        super().__init__(coordinator, entry, pupil, "pe")

    def _pe(self) -> list[dict[str, Any]]:
        pupil = self.pupil
        if pupil is None:
            return []
        day = next_school_day(pupil.lessons, dt_util.now().date())
        return pe_lessons(lessons_on(pupil.lessons, day)) if day else []

    @property
    def is_on(self) -> bool | None:
        if self.pupil is None:
            return None
        return bool(self._pe())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        pupil = self.pupil
        if pupil is None:
            return {}
        day = next_school_day(pupil.lessons, dt_util.now().date())
        return {"date": day, "times": [time_of(item["start"]) for item in self._pe()]}


class AbsentTodaySensor(InfomentorPupilEntity, BinarySensorEntity):
    """Tänder om barnet är anmält frånvarande idag. Imorgon ligger som attribut."""

    _attr_should_poll = False
    _attr_translation_key = "absent_today"
    _attr_icon = "mdi:account-off-outline"

    def __init__(
        self, coordinator: InfomentorCoordinator, entry: ConfigEntry, pupil: PupilData
    ) -> None:
        super().__init__(coordinator, entry, pupil, "absent_today")

    @property
    def is_on(self) -> bool | None:
        pupil = self.pupil
        return is_absent(pupil.attendance, "today") if pupil else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        pupil = self.pupil
        if pupil is None:
            return {}
        return {
            "tomorrow": is_absent(pupil.attendance, "tomorrow"),
            "sessions_today": pupil.attendance.get("today", []),
            "sessions_tomorrow": pupil.attendance.get("tomorrow", []),
        }

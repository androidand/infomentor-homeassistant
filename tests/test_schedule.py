"""Schemakalendern och att "inget planerat" blir ett svar i stället för okänt."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from custom_components.infomentor.calendar import LessonsCalendar  # noqa: E402
from custom_components.infomentor.coordinator import InfomentorData, PupilData  # noqa: E402
from custom_components.infomentor.sensor import (  # noqa: E402
    FritidsSensor,
    LearnlogSensor,
    SchoolDaySensor,
)
from custom_components.infomentor.util import nothing_text  # noqa: E402


def _pupil(**kwargs) -> PupilData:
    return PupilData(pupil_id="7", name="A, B", switch_url="", display_name="B A", **kwargs)


def _wire(cls, pupil, language="sv", data=None):
    coordinator = SimpleNamespace(
        data=data or InfomentorData(pupils=[pupil]), last_update_success=True
    )
    entity = cls(coordinator, SimpleNamespace(entry_id="e", options={}), pupil)
    entity.hass = SimpleNamespace(config=SimpleNamespace(language=language))
    return entity


def test_nothing_is_said_in_the_users_language() -> None:
    assert nothing_text("sv") == "Inget"
    assert nothing_text("sv-SE") == "Inget"
    assert nothing_text("en") == "Nothing"
    assert nothing_text(None) == "Nothing"


def test_a_school_day_sensor_without_lessons_says_nothing_not_unknown() -> None:
    assert _wire(SchoolDaySensor, _pupil()).native_value == "Inget"
    assert _wire(SchoolDaySensor, _pupil(), language="en").native_value == "Nothing"


def test_fritids_today_says_nothing_on_a_day_without_times() -> None:
    days = [{"date": "2000-01-01", "start": "08:00", "end": "17:00", "on_leave": False,
             "closed": False, "closed_reason": ""}]
    assert _wire(FritidsSensor, _pupil(fritids=days)).native_value == "Inget"


def test_the_schedule_calendar_shows_lessons_with_room_and_teacher() -> None:
    lessons = [{"title": "Matte", "start": "2026-10-27T08:30:00", "end": "2026-10-27T09:15:00",
                "room": "B12", "teachers": "Anna", "all_day": False, "is_pe": False}]
    (event,) = _wire(LessonsCalendar, _pupil(lessons=lessons))._events()
    assert (event.summary, event.location, event.description) == ("Matte", "B12", "Anna")
    assert event.start.hour == 8 and event.uid is None


def test_the_weekly_letter_text_and_link_are_attributes_of_the_sensor_itself() -> None:
    letter = {"title": "Veckobrev 44", "subject": "", "group": "", "modified": "2026-10-23",
              "text": "Hej!", "attachments": ["Brev.pdf"], "attachment_urls": ["https://x/y"]}
    data = InfomentorData(pupils=[_pupil()], weekly_letter=letter)
    attrs = _wire(LearnlogSensor, _pupil(), data=data).extra_state_attributes
    assert (attrs["text"], attrs["attachments"], attrs["attachment_urls"]) == (
        "Hej!", ["Brev.pdf"], ["https://x/y"],
    )

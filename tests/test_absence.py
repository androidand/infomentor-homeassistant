"""Frånvaro: binär sensor för idag och sensorn för senaste registreringen."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from custom_components.infomentor.binary_sensor import AbsentTodaySensor  # noqa: E402
from custom_components.infomentor.coordinator import InfomentorData, PupilData  # noqa: E402
from custom_components.infomentor.sensor import LastAbsenceSensor  # noqa: E402


def _entity(cls, attendance):
    pupil = PupilData(
        pupil_id="7", name="A, B", switch_url="", display_name="B A", attendance=attendance
    )
    coordinator = SimpleNamespace(data=InfomentorData(pupils=[pupil]), last_update_success=True)
    return cls(coordinator, SimpleNamespace(entry_id="e", options={}), pupil)


HISTORY = [
    {"date": "2026-10-19", "time": "08:30", "reason": "Föranmäld", "minutes": "50", "comment": ""},
    {"date": "2026-10-16", "time": "", "reason": "Sen ankomst", "minutes": "", "comment": ""},
]


def test_the_binary_sensor_is_on_for_a_registered_absence_today() -> None:
    off = _entity(AbsentTodaySensor, {"absent_today": False, "today": [], "tomorrow": []})
    on = _entity(AbsentTodaySensor, {"absent_today": False, "today": ["FKL 09:10"], "tomorrow": []})
    assert off.is_on is False
    assert on.is_on is True
    assert off.extra_state_attributes["tomorrow"] is False


def test_the_last_absence_is_the_newest_registration() -> None:
    sensor = _entity(LastAbsenceSensor, {"history": HISTORY, "pending_leave": 0})
    assert sensor.native_value == date(2026, 10, 19)
    assert sensor.extra_state_attributes["reason"] == "Föranmäld"
    assert len(sensor.extra_state_attributes["history"]) == 2


def test_no_history_gives_no_date_and_a_bad_date_does_not_crash() -> None:
    assert _entity(LastAbsenceSensor, {}).native_value is None
    broken = [{**HISTORY[0], "date": "inte-ett-datum"}]
    assert _entity(LastAbsenceSensor, {"history": broken}).native_value is None

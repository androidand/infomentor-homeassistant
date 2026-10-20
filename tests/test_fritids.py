"""Fritids: kalendern och att ett barn utan fritids inte stör uppdateringen."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from custom_components.infomentor.api import CannotConnect, InvalidAuth  # noqa: E402
from custom_components.infomentor.calendar import FritidsCalendar  # noqa: E402
from custom_components.infomentor.coordinator import (  # noqa: E402
    InfomentorCoordinator,
    InfomentorData,
    PupilData,
)

DAYS = [
    {"date": "2026-10-19", "start": "08:00", "end": "13:15", "on_leave": False, "closed": False,
     "closed_reason": ""},
    {"date": "2026-10-20", "start": "", "end": "", "on_leave": True, "closed": False,
     "closed_reason": ""},
]


def test_the_fritids_calendar_has_the_weeks_times_and_leave_days() -> None:
    pupil = PupilData(pupil_id="7", name="A, B", switch_url="", display_name="B A", fritids=DAYS)
    coordinator = SimpleNamespace(data=InfomentorData(pupils=[pupil]), last_update_success=True)
    calendar = FritidsCalendar(coordinator, SimpleNamespace(entry_id="e", options={}), pupil)
    first, leave = calendar._events()
    assert (first.summary, first.start.hour, first.end.minute) == ("Fritids", 8, 15)
    assert leave.summary == "Ledig från fritids"


def _coordinator() -> SimpleNamespace:
    return SimpleNamespace(_ok_calls=0, _auth_skips=0, _not_permitted=set())


async def _raises(error: Exception):
    raise error


async def test_a_quiet_fetch_that_is_refused_never_forces_a_new_login(caplog) -> None:
    me = _coordinator()
    with caplog.at_level(logging.DEBUG):
        value = await InfomentorCoordinator._safe(
            me, "fritids", _raises(InvalidAuth("403")), {}, strict=True, quiet=True
        )
    assert value == {} and me._auth_skips == 0  # inte en död session
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_a_refused_fetch_that_is_not_quiet_still_forces_a_new_login() -> None:
    with pytest.raises(InvalidAuth):
        await InfomentorCoordinator._safe(
            _coordinator(), "schema", _raises(InvalidAuth("401")), [], strict=True
        )


async def test_a_quiet_fetch_still_reports_that_the_host_is_down() -> None:
    with pytest.raises(CannotConnect):
        await InfomentorCoordinator._safe(
            _coordinator(), "fritids", _raises(CannotConnect("nere")), {}, quiet=True
        )

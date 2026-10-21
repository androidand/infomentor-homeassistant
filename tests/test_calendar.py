"""Barnets kalender som HA-entitet: händelser, aktuell händelse och tidsintervall."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from homeassistant.util import dt as dt_util  # noqa: E402

from custom_components.infomentor.calendar import PupilCalendar  # noqa: E402
from custom_components.infomentor.coordinator import InfomentorData, PupilData  # noqa: E402


def _entry(title, start, end, *, all_day=False, subjects="", uid="1"):
    return {
        "id": uid,
        "title": title,
        "start": start,
        "end": end,
        "all_day": all_day,
        "subjects": subjects,
    }


def _calendar(*entries) -> PupilCalendar:
    pupil = PupilData(
        pupil_id="7", name="Efternamn, Barn", switch_url="", display_name="Barn Efternamn"
    )
    pupil.calendar = list(entries)
    coordinator = SimpleNamespace(data=InfomentorData(pupils=[pupil]), last_update_success=True)
    return PupilCalendar(coordinator, SimpleNamespace(entry_id="e", options={}), pupil)


FRIDAY = _entry("Frivillig läxa", "2026-10-23T00:00:00", "2026-10-23T23:59:59", all_day=True)
TIMED = _entry("Utvecklingssamtal", "2026-10-26T09:00:00", "2026-10-26T09:30:00", uid="2")


def test_an_all_day_post_becomes_a_date_event_and_a_timed_one_gets_the_local_zone() -> None:
    day, timed = _calendar(FRIDAY, TIMED)._events()
    assert (day.start, day.end) == (date(2026, 10, 23), date(2026, 10, 24))
    assert day.summary == "Frivillig läxa"
    assert timed.start == datetime(2026, 10, 26, 9, 0, tzinfo=dt_util.DEFAULT_TIME_ZONE)


async def test_events_are_limited_to_the_requested_range() -> None:
    calendar = _calendar(FRIDAY, TIMED)
    zone = dt_util.DEFAULT_TIME_ZONE
    first = await calendar.async_get_events(
        None, datetime(2026, 10, 23, tzinfo=zone), datetime(2026, 10, 24, tzinfo=zone)
    )
    assert [e.summary for e in first] == ["Frivillig läxa"]
    none = await calendar.async_get_events(
        None, datetime(2026, 10, 24, tzinfo=zone), datetime(2026, 10, 26, tzinfo=zone)
    )
    assert none == []


def test_the_current_event_is_the_first_one_that_has_not_ended() -> None:
    soon = datetime.now() + timedelta(days=1)
    later = soon + timedelta(days=5)
    past = soon - timedelta(days=9)
    fmt = "%Y-%m-%dT%H:%M:%S"
    calendar = _calendar(
        _entry("Förbi", past.strftime(fmt), (past + timedelta(hours=1)).strftime(fmt), uid="a"),
        _entry("Senare", later.strftime(fmt), (later + timedelta(hours=1)).strftime(fmt), uid="c"),
        _entry("Snart", soon.strftime(fmt), (soon + timedelta(hours=1)).strftime(fmt), uid="b"),
    )
    assert calendar.event.summary == "Snart"
    assert _calendar().event is None


def test_a_post_without_a_readable_start_is_skipped() -> None:
    assert _calendar(_entry("Trasig", "", ""))._events() == []


def test_an_event_in_progress_is_the_current_one_not_the_next() -> None:
    fmt = "%Y-%m-%dT%H:%M:%S"
    now = dt_util.now().replace(tzinfo=None)  # hubbens klockslag är lokala utan zon
    calendar = _calendar(
        _entry("Pågår", (now - timedelta(hours=1)).strftime(fmt),
               (now + timedelta(hours=1)).strftime(fmt), uid="a"),
        _entry("Nästa", (now + timedelta(hours=3)).strftime(fmt),
               (now + timedelta(hours=4)).strftime(fmt), uid="b"),
    )
    assert calendar.event.summary == "Pågår"


async def test_a_timed_event_inside_the_range_is_included() -> None:
    zone = dt_util.DEFAULT_TIME_ZONE
    found = await _calendar(TIMED).async_get_events(
        None, datetime(2026, 10, 26, tzinfo=zone), datetime(2026, 10, 27, tzinfo=zone)
    )
    assert [e.summary for e in found] == ["Utvecklingssamtal"]


def test_a_post_without_an_id_gets_no_uid() -> None:
    (event,) = _calendar(_entry("Utan id", "2026-10-26T09:00:00", "2026-10-26T10:00:00", uid=""))._events()
    assert event.uid is None

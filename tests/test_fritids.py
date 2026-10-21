"""Fritids: kalendern och att ett barn utan fritids inte stör uppdateringen."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from custom_components.infomentor.api import ApiError, CannotConnect, InvalidAuth  # noqa: E402
from custom_components.infomentor.calendar import FritidsCalendar  # noqa: E402
from custom_components.infomentor.coordinator import (  # noqa: E402
    InfomentorCoordinator,
    InfomentorData,
    PupilData,
)
from custom_components.infomentor.entity import add_entities_as_they_appear  # noqa: E402
from custom_components.infomentor.probe import Outcome, SectionProbe  # noqa: E402
from custom_components.infomentor.util import normalize_fritids  # noqa: E402

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


def _coordinator(clock=lambda: 0.0) -> SimpleNamespace:
    return SimpleNamespace(
        _ok_calls=0, _auth_skips=0, _probe=SectionProbe(clock=clock), _last_good={}
    )


async def _raises(error: Exception):
    raise error


def _call(error: Exception | None = None, value=None):
    calls = []

    async def call():
        calls.append(1)
        if error:
            raise error
        return value

    return call, calls


async def _probed(me, call):
    return await InfomentorCoordinator._probed(
        me, "fritids:7", "fritids", call, {}, normalize_fritids
    )


async def test_a_refused_probe_never_forces_a_new_login_nor_logs_a_warning(caplog) -> None:
    me = _coordinator()
    call, _ = _call(InvalidAuth("403"))
    with caplog.at_level(logging.DEBUG):
        assert await _probed(me, call) == {}
    assert me._auth_skips == 0
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_a_child_known_to_lack_fritids_is_not_asked_again() -> None:
    now = [0.0]
    me = _coordinator(lambda: now[0])
    call, calls = _call(InvalidAuth("403"))
    await _probed(me, call)
    me._ok_calls = 1
    me._probe.commit()
    await _probed(me, call)
    assert len(calls) == 1  # inom dygnet
    now[0] = 25 * 3600
    await _probed(me, call)
    assert len(calls) == 2  # ett dygn senare frågar vi igen


async def test_a_failed_probe_is_retried_and_does_not_mean_absent() -> None:
    now = [0.0]
    me = _coordinator(lambda: now[0])
    call, calls = _call(ApiError("500"))
    await _probed(me, call)
    me._probe.commit()
    assert not me._probe.is_present("fritids:7")
    now[0] = 21 * 60  # inom ett vanligt uppehåll, inte ett dygn
    await _probed(me, call)
    assert len(calls) == 2


async def test_a_probe_from_a_round_that_died_is_forgotten() -> None:
    me = _coordinator()
    call, calls = _call(InvalidAuth("403"))
    await _probed(me, call)
    me._probe.discard()  # omgången föll på en död session
    await _probed(me, call)
    assert len(calls) == 2  # vi lärde oss inget "saknas"


async def test_a_known_schedule_survives_a_failed_refresh() -> None:
    now = [0.0]
    me = _coordinator(lambda: now[0])
    good, _ = _call(value={"days": [{"date": "2026-10-19"}]})
    first = await _probed(me, good)
    me._probe.commit()
    assert first
    bad, _ = _call(ApiError("500"))
    assert await _probed(me, bad) == first  # inte tömd av ett fel


async def test_the_host_being_down_is_still_reported() -> None:
    call, _ = _call(CannotConnect("nere"))
    with pytest.raises(CannotConnect):
        await _probed(_coordinator(), call)


async def test_an_unprobed_refusal_still_forces_a_new_login() -> None:
    with pytest.raises(InvalidAuth):
        await InfomentorCoordinator._safe(
            SimpleNamespace(_ok_calls=0, _auth_skips=0, _not_permitted=set()),
            "schema",
            _raises(InvalidAuth("401")),
            [],
            strict=True,
        )


def test_fritids_entities_appear_when_the_data_arrives_later_and_only_once() -> None:
    pupil = PupilData(pupil_id="7", name="A, B", switch_url="", display_name="B A")
    listeners = []
    coordinator = SimpleNamespace(
        data=InfomentorData(pupils=[pupil]),
        async_add_listener=lambda cb: listeners.append(cb) or (lambda: None),
    )
    entry = SimpleNamespace(async_on_unload=lambda unsub: None)
    added = []
    add_entities_as_they_appear(
        entry,
        coordinator,
        added.extend,
        lambda p: ["fritids"] if p.fritids else [],
    )
    assert added == []  # inget fritids vid start (t.ex. ett fel vid första hämtningen)
    pupil.fritids = DAYS
    listeners[0]()
    listeners[0]()
    assert added == ["fritids"]  # skapad när datan kom, och bara en gång


def test_probe_answers_only_count_when_something_else_got_through() -> None:
    me = _coordinator()
    me._probe.record("fritids:7", Outcome.REFUSED)
    InfomentorCoordinator._settle_probes(me)  # inget lyckades: kan vara en död session
    assert me._probe.due("fritids:7")
    me._probe.record("fritids:7", Outcome.REFUSED)
    me._ok_calls = 1
    InfomentorCoordinator._settle_probes(me)
    assert not me._probe.due("fritids:7")


def test_a_new_round_forgets_answers_from_a_round_that_fell() -> None:
    me = _coordinator()
    me._probe.record("fritids:7", Outcome.REFUSED)
    me._plan_ids_seen = set()
    InfomentorCoordinator._begin_round(me)
    me._ok_calls = 1
    InfomentorCoordinator._settle_probes(me)
    assert me._probe.due("fritids:7")

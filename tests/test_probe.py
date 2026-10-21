"""SectionProbe: vem vi frågar när, och vad ett svar får lära oss."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_path = Path(__file__).parent.parent / "custom_components" / "infomentor" / "probe.py"
_spec = importlib.util.spec_from_file_location("probe", _path)
_mod = importlib.util.module_from_spec(_spec)
import sys  # noqa: E402

sys.modules["probe"] = _mod
_spec.loader.exec_module(_mod)
Outcome, SectionProbe = _mod.Outcome, _mod.SectionProbe


def _probe(now):
    return SectionProbe(recheck=100, backoff_base=10, backoff_max=40, clock=lambda: now[0])


def _learn(probe, key, outcome):
    probe.record(key, outcome)
    probe.commit()


def test_an_unknown_key_is_asked_at_once() -> None:
    assert _probe([0.0]).due("k")


def test_nothing_counts_until_committed() -> None:
    probe = _probe([0.0])
    probe.record("k", Outcome.REFUSED)
    assert probe.due("k")
    probe.discard()
    probe.commit()
    assert probe.due("k")


def test_a_refusal_or_empty_answer_waits_for_the_recheck() -> None:
    now = [0.0]
    for outcome in (Outcome.REFUSED, Outcome.EMPTY):
        probe = _probe(now)
        _learn(probe, "k", outcome)
        now[0] = 99.0
        assert not probe.due("k")
        now[0] = 100.0
        assert probe.due("k")
        now[0] = 0.0


def test_data_is_asked_every_round() -> None:
    probe = _probe([0.0])
    _learn(probe, "k", Outcome.DATA)
    assert probe.due("k") and probe.is_present("k")


def test_an_empty_week_does_not_remove_a_part_that_has_existed() -> None:
    probe = _probe([0.0])
    _learn(probe, "k", Outcome.DATA)
    _learn(probe, "k", Outcome.EMPTY)
    assert probe.due("k") and probe.is_present("k")


def test_errors_back_off_and_never_mean_absent() -> None:
    now = [0.0]
    probe = _probe(now)
    waits = []
    for _ in range(5):
        _learn(probe, "k", Outcome.ERROR)
        waits.append(probe._states["k"].next_try - now[0])
    assert waits == [10, 20, 40, 40, 40]
    assert not probe.is_present("k")


def test_a_success_resets_the_backoff() -> None:
    probe = _probe([0.0])
    _learn(probe, "k", Outcome.ERROR)
    _learn(probe, "k", Outcome.DATA)
    _learn(probe, "k", Outcome.ERROR)
    assert probe._states["k"].next_try == 10

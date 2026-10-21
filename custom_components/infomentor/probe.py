"""Minns vad hubben har gett för en valfri del, så vi inte frågar i onödan eller ger upp för tidigt.

Alla konton är olika: en skola har fritids, en annan inte, en tredje har det först nästa termin.
En valfri del (fritids, frånvaro, filer ...) får därför tre öden per barn:

- **present**: den har gett data. Hämtas varje omgång.
- **absent**: hubben svarade tomt eller sa nej. Hämtas bara på nytt efter `recheck`
  (ett dygn), så ett barn utan fritids kostar inte ett anrop var tjugonde minut.
- **fel** (nätverk, 500): säger ingenting om vad kontot har. Nytt försök med växande
  uppehåll (`backoff_base`, dubblas, högst `backoff_max`), och delen bedöms aldrig som
  frånvarande på grund av ett fel.

Resultaten gäller först när hela omgången lyckades (`commit`); en omgång som faller på en död
session (`discard`) får inte ha lärt oss att något "saknas".

Ingen HA-import här, så modulen går att testa för sig.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic


class Outcome(enum.Enum):
    DATA = "data"  # hubben gav något
    EMPTY = "empty"  # hubben svarade, men tomt
    REFUSED = "refused"  # hubben sa nej (barnet/skolan har inte delen)
    ERROR = "error"  # tillfälligt fel; säger inget om kontot


@dataclass(slots=True)
class _State:
    present: bool = False  # har någon gång gett data
    next_try: float = 0.0  # tidigast nästa anrop (klockan)
    failures: int = 0  # fel i rad


class SectionProbe:
    """Per nyckel (t.ex. `fritids:<barn>`): ska vi fråga nu, och vad lärde vi oss?"""

    def __init__(
        self,
        *,
        recheck: float = 24 * 3600,
        backoff_base: float = 20 * 60,
        backoff_max: float = 6 * 3600,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._recheck = recheck
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._clock = clock
        self._states: dict[str, _State] = {}
        self._pending: list[tuple[str, Outcome]] = []

    def due(self, key: str) -> bool:
        state = self._states.get(key)
        return state is None or self._clock() >= state.next_try

    def is_present(self, key: str) -> bool:
        state = self._states.get(key)
        return bool(state and state.present)

    def record(self, key: str, outcome: Outcome) -> None:
        """Samla resultatet; det gäller först vid `commit`."""
        self._pending.append((key, outcome))

    def discard(self) -> None:
        self._pending.clear()

    def commit(self) -> None:
        pending, self._pending = self._pending, []
        for key, outcome in pending:
            self._apply(self._states.setdefault(key, _State()), outcome)

    def _apply(self, state: _State, outcome: Outcome) -> None:
        now = self._clock()
        if outcome is Outcome.ERROR:
            state.failures += 1
            delay = min(self._backoff_base * 2 ** (state.failures - 1), self._backoff_max)
            state.next_try = now + delay
            return
        state.failures = 0
        if outcome is Outcome.DATA:
            state.present = True
            state.next_try = 0.0
        elif outcome is Outcome.EMPTY and state.present:
            # En tom vecka (lov) tar inte bort en del som funnits.
            state.next_try = 0.0
        else:
            state.present = False
            state.next_try = now + self._recheck

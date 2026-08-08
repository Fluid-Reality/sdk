"""Clock abstractions used to make firmware timing deterministic in tests."""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class ScaledClock:
    """A real clock whose simulated time can run faster or slower than wall time."""

    def __init__(self, time_scale: float = 1.0) -> None:
        if time_scale <= 0:
            raise ValueError("time_scale must be greater than zero")
        self.time_scale = time_scale
        self._real_start = time.monotonic()
        self._sim_start = self._real_start

    def monotonic(self) -> float:
        elapsed = time.monotonic() - self._real_start
        return self._sim_start + elapsed * self.time_scale

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds / self.time_scale)


class ManualClock:
    """Deterministic clock for tests and scripted simulation."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = float(start)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot move the clock backwards")
        self.now += seconds

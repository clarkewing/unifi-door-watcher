from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...


class MonotonicClock:
    def now(self) -> float:
        return time.monotonic()


_default = MonotonicClock()


def default_clock() -> Clock:
    return _default

"""可注入时钟，便于在测试中复现超时与并发时序。"""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """返回当前时间（秒，单调时钟语义）。"""
        ...


class SystemClock:
    def now(self) -> float:
        return time.monotonic()


class FakeClock:
    """测试用假时钟，可显式推进。"""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> float:
        self._t += seconds
        return self._t

"""时间抽象：让在线超时与并发判定在测试中可复现。"""

import threading
import time


class Clock:
    def now(self) -> float:
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> float:
        return time.time()


class FakeClock(Clock):
    """测试用时钟：手动推进，线程安全。"""

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds

    def set(self, value: float) -> None:
        with self._lock:
            self._now = float(value)

"""OTA 领域服务组装。"""

from .clock import Clock, FakeClock, SystemClock
from .device_service import DeviceService
from .errors import OtaError, RejectReason
from .models import DeviceReleaseHistoryItem
from .report_service import ReportService
from .release_service import ReleaseService
from .store import Store


def build_services(clock: Clock | None = None):
    clock = clock or SystemClock()
    store = Store(clock)
    return DeviceService(store), ReleaseService(store), ReportService(store), store


__all__ = [
    "Clock",
    "SystemClock",
    "FakeClock",
    "Store",
    "DeviceService",
    "ReleaseService",
    "ReportService",
    "OtaError",
    "RejectReason",
    "DeviceReleaseHistoryItem",
    "build_services",
]

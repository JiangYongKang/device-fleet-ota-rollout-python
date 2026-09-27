"""测试公共夹具：每次用例使用独立的 FakeClock 与全新存储，结果可复现。"""

import unittest

from ota import DeviceService, FakeClock, ReportService, ReleaseService, Store
from ota.models import Firmware


class OtaTestCase(unittest.TestCase):
    heartbeat_timeout = 30.0

    def setUp(self) -> None:
        self.clock = FakeClock(1000.0)
        self.store = Store(self.clock)
        self.devices = DeviceService(self.store, heartbeat_timeout=self.heartbeat_timeout)
        self.releases = ReleaseService(self.store)
        self.reports = ReportService(self.store)

    # ---------- helpers ----------

    def register_device(self, device_id: str, group: str = "g", version: str = "1.0.0"):
        return self.devices.register(device_id, group, version)

    def register_devices(self, ids, group: str = "g", version: str = "1.0.0"):
        return [self.register_device(i, group, version) for i in ids]

    def add_firmware(self, firmware_id: str = "fw", version: str = "2.0.0",
                     min_device_version: str = "1.0.0") -> Firmware:
        fw = Firmware(firmware_id=firmware_id, version=version,
                      min_device_version=min_device_version)
        self.store.add_firmware(fw)
        return fw

    def create_and_open(self, release_id: str = "r1", *, group: str = "g",
                        batch_size: int = 10, failure_threshold: float = 1.0,
                        firmware_id: str = "fw"):
        self.releases.create_release(
            release_id, firmware_id, group, batch_size, failure_threshold)
        return self.releases.advance(release_id)

    def reject_reason(self, fn):
        from ota.errors import OtaError
        try:
            fn()
        except OtaError as exc:
            self.assertIsNotNone(exc.reason)
            return exc.reason.value
        self.fail("expected OtaError")

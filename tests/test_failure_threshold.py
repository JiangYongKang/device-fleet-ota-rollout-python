"""失败设备比例阈值触发自动暂停，并记录可解释的触发原因。"""

from ota.errors import RejectReason
from ota.models import PauseReason, ReportStage, ReleaseState
from tests.support import OtaTestCase


class FailureThresholdTest(OtaTestCase):
    def _devices(self, n: int):
        self.register_devices([f"d{i}" for i in range(n)])
        self.add_firmware()

    def test_threshold_triggered_at_boundary(self):
        # 阈值 0.5：1 成功 + 1 失败 => 失败比例 0.5 >= 0.5，应在第二台上报失败时暂停
        self._devices(4)
        self.create_and_open("r1", batch_size=10, failure_threshold=0.5)

        self.reports.report_result("d0", ReportStage.SUCCESS)
        self.reports.report_result("d1", ReportStage.FAIL, "verify_checksum")

        view = self.releases.get_release("r1")
        self.assertEqual(view.state, ReleaseState.AUTO_PAUSED)
        self.assertEqual(view.pause_reason, PauseReason.FAILURE_THRESHOLD)
        self.assertAlmostEqual(view.failure_ratio, 0.5)
        self.assertEqual(view.failed, 1)
        self.assertEqual(view.succeeded, 1)
        # 失败原因保留在设备/记录侧
        self.assertEqual(self.devices.get_device("d1").failure_reason, "verify_checksum")

    def test_below_threshold_keeps_release_running(self):
        # 阈值 0.5：1 成功 + 1 失败恰好 0.5 会触发；这里用 3 成功 + 1 失败 = 0.25 不触发
        self._devices(6)
        self.create_and_open("r1", batch_size=10, failure_threshold=0.5)
        for d in ("d0", "d1", "d2"):
            self.reports.report_result(d, ReportStage.SUCCESS)
        self.reports.report_result("d3", ReportStage.FAIL, "download_timeout")

        view = self.releases.get_release("r1")
        self.assertEqual(view.state, ReleaseState.IN_PROGRESS)
        self.assertIsNone(view.pause_reason)
        self.assertAlmostEqual(view.failure_ratio, 0.25)

    def test_zero_threshold_pauses_on_first_failure(self):
        self._devices(2)
        self.create_and_open("r1", batch_size=10, failure_threshold=0.0)
        self.reports.report_result("d0", ReportStage.FAIL, "x")
        view = self.releases.get_release("r1")
        self.assertEqual(view.state, ReleaseState.AUTO_PAUSED)
        self.assertEqual(view.pause_reason, PauseReason.FAILURE_THRESHOLD)

    def test_threshold_does_not_fire_on_inflight_or_pending(self):
        # 只统计终态（成功/失败），下载中等中间阶段不计入失败比例
        self._devices(3)
        self.create_and_open("r1", batch_size=10, failure_threshold=0.5)
        self.reports.report_result("d0", ReportStage.DOWNLOAD)
        self.reports.report_result("d1", ReportStage.INSTALL)
        view = self.releases.get_release("r1")
        self.assertEqual(view.state, ReleaseState.IN_PROGRESS)
        self.assertEqual(view.failure_ratio, 0.0)
        self.assertEqual(view.pending_or_inflight, 3)  # 2 中间阶段 + 1 尚未开始

    def test_after_auto_pause_reports_are_rejected_then_resume_accepts(self):
        self._devices(3)
        self.create_and_open("r1", batch_size=10, failure_threshold=0.5)
        self.reports.report_result("d0", ReportStage.SUCCESS)
        self.reports.report_result("d1", ReportStage.FAIL, "boom")
        self.assertEqual(self.releases.get_release("r1").state, ReleaseState.AUTO_PAUSED)

        # 暂停期间的上报必须被可区分地拒绝
        reason = self.reject_reason(
            lambda: self.reports.report_result("d2", ReportStage.DOWNLOAD))
        self.assertEqual(reason, RejectReason.RELEASE_PAUSED.value)

        # 继续后可重新接受上报；此时失败比例 1/2 已达阈值，再来一台成功 => 1/3 < 0.5
        self.releases.resume("r1")
        view = self.reports.report_result("d2", ReportStage.SUCCESS)
        self.assertIsNotNone(view)
        self.assertEqual(self.releases.get_release("r1").failure_ratio, 1 / 3)

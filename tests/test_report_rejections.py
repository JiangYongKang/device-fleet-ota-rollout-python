"""升级结果上报：阶段区分、阶段单调、各类拒绝原因可被区分。"""

from ota.errors import RejectReason
from ota.models import ReportStage, UpgradeStatus
from tests.support import OtaTestCase


class ReportRejectionTest(OtaTestCase):
    def _setup(self, low_version_device: bool = True):
        self.register_devices(["d0", "d1", "d2"], group="g", version="1.0.0")
        self.register_device("x0", group="other", version="1.0.0")
        if low_version_device:
            self.register_device("low", group="g", version="0.9.0")
        self.add_firmware()
        self.create_and_open("r1", batch_size=10, failure_threshold=1.0)

    def test_stage_progression_maps_to_distinct_statuses(self):
        self._setup(low_version_device=False)
        cases = [
            (ReportStage.DOWNLOAD, UpgradeStatus.DOWNLOADING),
            (ReportStage.VERIFY, UpgradeStatus.VERIFYING),
            (ReportStage.INSTALL, UpgradeStatus.INSTALLING),
            (ReportStage.REBOOT, UpgradeStatus.REBOOTING),
            (ReportStage.SUCCESS, UpgradeStatus.SUCCEEDED),
        ]
        for stage, expected in cases:
            view = self.reports.report_result("d0", stage)
            self.assertEqual(view.upgrade_status, expected)
            self.assertEqual(view.last_stage, stage)
        self.assertEqual(self.devices.get_device("d0").firmware_version, "2.0.0")

    def test_stale_stage_is_rejected(self):
        self._setup()
        self.reports.report_result("d0", ReportStage.DOWNLOAD)
        self.reports.report_result("d0", ReportStage.VERIFY)
        reason = self.reject_reason(
            lambda: self.reports.report_result("d0", ReportStage.DOWNLOAD))
        self.assertEqual(reason, RejectReason.STALE_STAGE.value)

    def test_terminal_success_rejects_regression_but_allows_duplicate(self):
        self._setup()
        self.reports.report_result("d0", ReportStage.SUCCESS)
        # 重复成功幂等
        self.reports.report_result("d0", ReportStage.SUCCESS)
        # 成功后再回退阶段 => stale
        self.assertEqual(
            self.reject_reason(lambda: self.reports.report_result("d0", ReportStage.REBOOT)),
            RejectReason.STALE_STAGE.value,
        )

    def test_terminal_fail_rejects_non_fail_but_allows_duplicate(self):
        # 阈值设高，避免单台失败即触发自动暂停，专门验证终态语义
        self.register_devices(["d0", "d1", "d2"], group="g", version="1.0.0")
        self.add_firmware()
        self.create_and_open("r1", batch_size=10, failure_threshold=1.0)
        # 先造一台成功，使 1 失败 / 2 终态 = 0.5 < 1.0，发布仍在进行
        self.reports.report_result("d1", ReportStage.SUCCESS)
        self.reports.report_result("d0", ReportStage.FAIL, "err")
        self.reports.report_result("d0", ReportStage.FAIL, "err")  # 重复 FAIL 幂等
        self.assertEqual(
            self.reject_reason(lambda: self.reports.report_result("d0", ReportStage.SUCCESS)),
            RejectReason.STALE_STAGE.value,
        )

    def test_duplicate_fail_still_idempotent_after_auto_pause(self):
        self.register_devices(["d0"], group="g", version="1.0.0")
        self.add_firmware()
        self.create_and_open("r1", batch_size=10, failure_threshold=1.0)
        self.reports.report_result("d0", ReportStage.FAIL, "err")  # 触发自动暂停
        # 自动暂停后重传 FAIL 仍应幂等，得到相同最终状态而非报错
        view = self.reports.report_result("d0", ReportStage.FAIL, "err")
        self.assertEqual(view.upgrade_status, UpgradeStatus.FAILED)

    def test_device_not_found(self):
        self._setup()
        self.assertEqual(
            self.reject_reason(lambda: self.reports.report_result("ghost", ReportStage.DOWNLOAD)),
            RejectReason.DEVICE_NOT_FOUND.value,
        )

    def test_not_in_target_group(self):
        self._setup()
        # other 分组设备从未被任何发布分配
        self.assertEqual(
            self.reject_reason(lambda: self.reports.report_result("x0", ReportStage.DOWNLOAD)),
            RejectReason.DEVICE_NOT_IN_TARGET_GROUP.value,
        )

    def test_below_min_version_distinguishable(self):
        self._setup()
        self.assertEqual(
            self.reject_reason(lambda: self.reports.report_result("low", ReportStage.DOWNLOAD)),
            RejectReason.BELOW_MIN_VERSION.value,
        )

    def test_all_rejection_reasons_are_distinct_strings(self):
        reasons = {
            r.value for r in (
                RejectReason.DEVICE_NOT_FOUND,
                RejectReason.DEVICE_NOT_IN_TARGET_GROUP,
                RejectReason.BELOW_MIN_VERSION,
                RejectReason.RELEASE_PAUSED,
                RejectReason.RELEASE_ROLLING_BACK,
                RejectReason.RELEASE_FINISHED,
                RejectReason.STALE_STAGE,
            )
        }
        self.assertEqual(len(reasons), 7)

    def test_release_finished_rejection(self):
        self._setup(low_version_device=False)
        for d in ("d0", "d1", "d2"):
            self.reports.report_result(d, ReportStage.SUCCESS)
        self.assertEqual(
            self.reject_reason(lambda: self.reports.report_result("d0", ReportStage.DOWNLOAD)),
            RejectReason.RELEASE_FINISHED.value,
        )

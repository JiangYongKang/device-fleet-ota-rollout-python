"""回滚与暂停后的最终状态一致性：设备视图 == 发布记录，重复查询结论稳定。"""

from ota.errors import RejectReason
from ota.models import (
    BatchState,
    PauseReason,
    ReleaseState,
    ReportStage,
    UpgradeStatus,
)
from tests.support import OtaTestCase


class RollbackConsistencyTest(OtaTestCase):
    def _release(self, threshold: float = 0.9):
        self.register_devices(["d0", "d1", "d2"], group="g", version="1.0.0")
        self.add_firmware()
        self.create_and_open("r1", batch_size=10, failure_threshold=threshold)

    def test_rollback_restores_versions_and_views_match_records(self):
        self._release()
        self.reports.report_result("d0", ReportStage.SUCCESS)   # 已升到 2.0.0
        self.reports.report_result("d1", ReportStage.INSTALL)  # 进行中
        # d2 尚未开始

        view = self.releases.rollback("r1")
        self.assertEqual(view.state, ReleaseState.ROLLED_BACK)
        self.assertEqual(view.rolled_back, 3)

        # 设备视图与发布记录两侧一致
        for device_id in ("d0", "d1", "d2"):
            device_view = self.devices.get_device(device_id)
            self.assertEqual(device_view.firmware_version, "1.0.0")  # 版本恢复
            self.assertIsNone(device_view.active_release_id)
            self.assertEqual(device_view.upgrade_status, UpgradeStatus.ROLLED_BACK)

            record = self.store.get_record("r1", device_id)
            self.assertEqual(record.status, UpgradeStatus.ROLLED_BACK)

    def test_repeated_query_after_rollback_is_stable(self):
        self._release()
        self.reports.report_result("d0", ReportStage.SUCCESS)
        self.releases.rollback("r1")
        first = self.devices.get_device("d0")
        for _ in range(3):
            again = self.devices.get_device("d0")
            self.assertEqual(again.upgrade_status, first.upgrade_status)
            self.assertEqual(again.firmware_version, first.firmware_version)
            self.assertEqual(again.active_release_id, first.active_release_id)
        release_view = self.releases.get_release("r1")
        self.assertEqual(release_view.state, ReleaseState.ROLLED_BACK)
        self.assertEqual(release_view.rolled_back, 3)

    def test_report_during_rollback_rejected_distinguishably(self):
        self._release()
        self.reports.report_result("d0", ReportStage.SUCCESS)
        self.releases.rollback("r1")
        reason = self.reject_reason(
            lambda: self.reports.report_result("d1", ReportStage.REBOOT))
        self.assertEqual(reason, RejectReason.RELEASE_ROLLING_BACK.value)

    def test_rollback_is_idempotent(self):
        self._release()
        self.reports.report_result("d0", ReportStage.SUCCESS)
        first = self.releases.rollback("r1")
        second = self.releases.rollback("r1")
        self.assertEqual(first.state, second.state)
        self.assertEqual(first.rolled_back, second.rolled_back)
        # 版本没有被二次错误改写
        self.assertEqual(self.devices.get_device("d0").firmware_version, "1.0.0")

    def test_advance_after_rollback_rejected(self):
        self._release()
        self.releases.rollback("r1")
        reason = self.reject_reason(lambda: self.releases.advance("r1"))
        self.assertEqual(reason, RejectReason.ILLEGAL_RELEASE_STATE.value)

    def test_paused_then_rollback_device_and_release_agree(self):
        self._release()
        self.reports.report_result("d0", ReportStage.INSTALL)
        self.releases.pause("r1")
        self.assertEqual(self.releases.get_release("r1").pause_reason, PauseReason.MANUAL)

        # 暂停期间上报被拒绝，状态不被改变
        reason = self.reject_reason(
            lambda: self.reports.report_result("d1", ReportStage.DOWNLOAD))
        self.assertEqual(reason, RejectReason.RELEASE_PAUSED.value)
        self.assertEqual(self.devices.get_device("d1").upgrade_status, UpgradeStatus.PENDING)

        self.releases.rollback("r1")
        for device_id in ("d0", "d1", "d2"):
            self.assertEqual(
                self.devices.get_device(device_id).upgrade_status,
                UpgradeStatus.ROLLED_BACK,
            )

    def test_auto_pause_reason_remains_explainable(self):
        self._release(threshold=0.5)
        self.reports.report_result("d0", ReportStage.SUCCESS)
        self.reports.report_result("d1", ReportStage.FAIL, "checksum")
        view = self.releases.get_release("r1")
        self.assertEqual(view.state, ReleaseState.AUTO_PAUSED)
        self.assertEqual(view.pause_reason, PauseReason.FAILURE_THRESHOLD)
        # 手动 pause 不覆盖自动暂停的触发原因
        self.releases.pause("r1")
        self.assertEqual(
            self.releases.get_release("r1").pause_reason,
            PauseReason.FAILURE_THRESHOLD,
        )

    def test_open_batch_closed_on_rollback(self):
        self._release()
        self.releases.rollback("r1")
        states = [b["state"] for b in self.releases.get_release("r1").batches]
        self.assertTrue(all(s == BatchState.DONE.value for s in states))

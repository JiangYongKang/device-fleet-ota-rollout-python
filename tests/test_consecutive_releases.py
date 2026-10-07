"""设备跨发布复用与连续升级：占用/释放、唯一占用并发、版本基线、迟到上报隔离。

判定日志（action=... inputs=... decision=...）在测试运行时打印到 stderr，
可直接看出每次推进/上报的输入与判定依据，方便复现。
"""

import threading

from ota.errors import RejectReason
from ota.models import (
    DeviceReleaseHistoryItem,
    ReleaseState,
    ReportStage,
    UpgradeStatus,
)
from tests.support import OtaTestCase


class ConsecutiveReleasesTest(OtaTestCase):
    def _devices(self, ids=("d0", "d1", "d2"), version="1.0.0", group="g"):
        self.register_devices(list(ids), group=group, version=version)

    def _firmwares_v2_v3(self, v2_min="1.0.0", v3_min="2.0.0"):
        self.store.add_firmware(self._fw("fw2", "2.0.0", v2_min))
        self.store.add_firmware(self._fw("fw3", "3.0.0", v3_min))

    @staticmethod
    def _fw(firmware_id, version, minimum):
        from ota.models import Firmware
        return Firmware(firmware_id=firmware_id, version=version,
                        min_device_version=minimum)

    def _succeed_all(self, release_id, device_ids):
        for d in device_ids:
            self.reports.report_result(d, ReportStage.SUCCESS, release_id=release_id)

    # ------------------------------------------------------------------
    # 场景 1：一轮正常结束后，同一批设备能再次进入新一轮并被“真正分配”
    # ------------------------------------------------------------------
    def test_devices_reassigned_to_new_round_after_completion(self):
        self._devices()
        self._firmwares_v2_v3()

        self.releases.create_release("r1", "fw2", "g", 10, 1.0)
        self.releases.advance("r1")
        self._succeed_all("r1", ["d0", "d1", "d2"])
        r1 = self.releases.get_release("r1")
        self.assertEqual(r1.state, ReleaseState.COMPLETED)

        # 收尾后占用指针必须释放
        for d in ("d0", "d1", "d2"):
            self.assertIsNone(self.store.get_active(d))

        # 新一轮针对同一批设备：必须真正把设备分进去，而不是整批跳过
        self.releases.create_release("r2", "fw3", "g", 10, 1.0)
        r2 = self.releases.advance("r2")
        self.assertEqual(r2.assigned, 3)
        self.assertEqual(r2.skipped_device_ids, [])
        self.assertEqual(r2.state, ReleaseState.IN_PROGRESS)
        for d in ("d0", "d1", "d2"):
            self.assertEqual(self.store.get_active(d), "r2")
            self.assertIsNotNone(self.store.get_record("r2", d))

        self._succeed_all("r2", ["d0", "d1", "d2"])
        self.assertEqual(self.releases.get_release("r2").state, ReleaseState.COMPLETED)
        # 两轮各 3 条独立记录，互不覆盖
        self.assertEqual(len(self.store.list_records("r1")), 3)
        self.assertEqual(len(self.store.list_records("r2")), 3)

    # ------------------------------------------------------------------
    # 场景 2：前一轮还在进行中时，新一轮不会抢占设备，且能看出被谁占着
    # ------------------------------------------------------------------
    def test_active_round_blocks_new_round_and_occupier_is_visible(self):
        self._devices()
        self._firmwares_v2_v3()

        self.releases.create_release("r1", "fw2", "g", 10, 1.0)
        self.releases.advance("r1")  # r1 进行中，占住 d0,d1,d2

        self.releases.create_release("r2", "fw3", "g", 10, 1.0)
        r2 = self.releases.advance("r2")
        self.assertEqual(r2.assigned, 0)
        # 不是笼统一句跳过：明细能看出设备仍被进行中的 r1 占用
        self.assertEqual([s["device_id"] for s in r2.skipped_devices],
                         ["d0", "d1", "d2"])
        for s in r2.skipped_devices:
            self.assertTrue(s["still_blocked"])
            self.assertEqual(s["occupied_by_release_id"], "r1")
            self.assertEqual(s["occupier_state"], ReleaseState.IN_PROGRESS.value)
            self.assertEqual(s["occupier_target_version"], "2.0.0")
        # 占用指针没有被 r2 抢走
        for d in ("d0", "d1", "d2"):
            self.assertEqual(self.store.get_active(d), "r1")
            self.assertIsNone(self.store.get_record("r2", d))

    def test_concurrent_two_releases_never_double_assign(self):
        # 同分组两条发布同时推进：同一台设备只能被其中一条占用
        self._devices(ids=[f"d{i}" for i in range(8)])
        self._firmwares_v2_v3()
        self.releases.create_release("r1", "fw2", "g", 3, 1.0)
        self.releases.create_release("r2", "fw3", "g", 3, 1.0)
        barrier = threading.Barrier(16)

        def worker(rid):
            barrier.wait()
            for _ in range(4):
                self.releases.advance(rid)

        threads = [threading.Thread(target=worker, args=("r1",)) for _ in range(8)]
        threads += [threading.Thread(target=worker, args=("r2",)) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 每台设备至多被一条发布线占用；同一 (release, device) 只产生一条记录
        owners = {}
        for rid in ("r1", "r2"):
            for rec in self.store.list_records(rid):
                owners.setdefault(rec.device_id, []).append(rid)
        for device_id, rids in owners.items():
            self.assertEqual(len(rids), 1, f"{device_id} double-assigned to {rids}")
        # 被分配的设备的 active 指针与其唯一记录一致
        for device_id, rids in owners.items():
            self.assertEqual(self.store.get_active(device_id), rids[0])

    # ------------------------------------------------------------------
    # 场景 3：回滚或结束之后，设备不再被旧发布挡住，下一轮可继续使用
    # ------------------------------------------------------------------
    def test_rollback_releases_devices_for_next_round(self):
        self._devices()
        self._firmwares_v2_v3()

        self.releases.create_release("r1", "fw2", "g", 10, 1.0)
        self.releases.advance("r1")
        self.reports.report_result("d0", ReportStage.SUCCESS, release_id="r1")
        self.reports.report_result("d1", ReportStage.INSTALL, release_id="r1")
        # d2 尚未开始
        self.releases.rollback("r1")

        for d in ("d0", "d1", "d2"):
            self.assertIsNone(self.store.get_active(d))  # 占用解除
            self.assertEqual(self.store.get_record("r1", d).status,
                             UpgradeStatus.ROLLED_BACK)
            self.assertEqual(self.devices.get_device(d).firmware_version, "1.0.0")

        # 回滚后下一轮同一批设备可被真正分配
        self.releases.create_release("r2", "fw2", "g", 10, 1.0)
        r2 = self.releases.advance("r2")
        self.assertEqual(r2.assigned, 3)
        self.assertEqual(r2.skipped_device_ids, [])

    def test_finished_old_release_no_longer_blocks_and_skip_resolves(self):
        # r1 进行中先挡住 r2（r2 全部跳过并收口）；r1 完成后，
        # r2 视图里的跳过明细应显示“不再被占”，且后续新发布 r3 能真正分进设备。
        self._devices()
        self._firmwares_v2_v3()
        self.releases.create_release("r1", "fw2", "g", 10, 1.0)
        self.releases.advance("r1")
        self.releases.create_release("r2", "fw3", "g", 10, 1.0)
        blocked = self.releases.advance("r2")
        self.assertEqual(blocked.assigned, 0)
        self.assertTrue(all(s["still_blocked"] for s in blocked.skipped_devices))

        self._succeed_all("r1", ["d0", "d1", "d2"])
        r2_after = self.releases.get_release("r2")
        # 占用方已收尾：明细可看出不再被挡
        self.assertTrue(all(not s["still_blocked"] for s in r2_after.skipped_devices))
        self.assertTrue(all(s["occupied_by_release_id"] is None
                            for s in r2_after.skipped_devices))

        self.releases.create_release("r3", "fw3", "g", 10, 1.0)
        r3 = self.releases.advance("r3")
        self.assertEqual(r3.assigned, 3)
        for d in ("d0", "d1", "d2"):
            self.assertEqual(self.store.get_active(d), "r3")

    # ------------------------------------------------------------------
    # 场景 4：连续两轮版本升级的基线正确（from_version 按当时实际版本快照）
    # ------------------------------------------------------------------
    def test_consecutive_upgrades_use_correct_version_baseline(self):
        self._devices(version="1.0.0")
        self._firmwares_v2_v3()

        self.releases.create_release("r1", "fw2", "g", 10, 1.0)
        self.releases.advance("r1")
        rec1 = self.store.get_record("r1", "d0")
        self.assertEqual(rec1.from_version, "1.0.0")
        self.assertEqual(rec1.target_version, "2.0.0")
        self._succeed_all("r1", ["d0", "d1", "d2"])
        self.assertEqual(self.devices.get_device("d0").firmware_version, "2.0.0")

        self.releases.create_release("r2", "fw3", "g", 10, 1.0)
        self.releases.advance("r2")
        rec2 = self.store.get_record("r2", "d0")
        # 新一轮基线是设备当时“实际生效”的 2.0.0，而不是沿用首轮的 1.0.0
        self.assertEqual(rec2.from_version, "2.0.0")
        self.assertEqual(rec2.target_version, "3.0.0")
        self._succeed_all("r2", ["d0", "d1", "d2"])
        self.assertEqual(self.devices.get_device("d0").firmware_version, "3.0.0")

        # 历史两轮各自基线/目标/结论独立，顺序为 r1 -> r2
        hist = self.devices.release_history("d0")
        self.assertEqual([(h.release_id, h.from_version, h.target_version,
                           h.status) for h in hist],
                         [("r1", "1.0.0", "2.0.0", UpgradeStatus.SUCCEEDED),
                          ("r2", "2.0.0", "3.0.0", UpgradeStatus.SUCCEEDED)])

    def test_rollback_baseline_restores_per_round_independently(self):
        # v2 成功后再开 v3 轮，v3 轮回滚只恢复到该轮基线 2.0.0，不会退回 1.0.0
        self._devices(version="1.0.0")
        self._firmwares_v2_v3()
        self.releases.create_release("r1", "fw2", "g", 10, 1.0)
        self.releases.advance("r1")
        self._succeed_all("r1", ["d0", "d1", "d2"])

        self.releases.create_release("r2", "fw3", "g", 10, 1.0)
        self.releases.advance("r2")
        self.reports.report_result("d0", ReportStage.SUCCESS, release_id="r2")
        self.releases.rollback("r2")
        # r2 基线是 2.0.0：回滚后应是 2.0.0 而非 1.0.0
        self.assertEqual(self.devices.get_device("d0").firmware_version, "2.0.0")
        self.assertEqual(self.store.get_record("r2", "d0").from_version, "2.0.0")
        # r1 历史结论不被 r2 回滚改写
        self.assertEqual(self.store.get_record("r1", "d0").status,
                         UpgradeStatus.SUCCEEDED)

    # ------------------------------------------------------------------
    # 场景 5：旧一轮迟到的上报被区分拒绝，不污染新一轮、不改回版本
    # ------------------------------------------------------------------
    def _two_completed_rounds(self):
        self._devices(version="1.0.0")
        self._firmwares_v2_v3()
        self.releases.create_release("r1", "fw2", "g", 10, 1.0)
        self.releases.advance("r1")
        self._succeed_all("r1", ["d0", "d1", "d2"])
        self.releases.create_release("r2", "fw3", "g", 10, 1.0)
        self.releases.advance("r2")
        self._succeed_all("r2", ["d0", "d1", "d2"])

    def test_late_report_from_old_completed_round_is_distinguishably_rejected(self):
        self._two_completed_rounds()
        # 两轮都结束、设备无 active：旧一轮 r1 迟到的中间阶段上报
        reason = self.reject_reason(
            lambda: self.reports.report_result(
                "d0", ReportStage.INSTALL, release_id="r1"))
        self.assertEqual(reason, RejectReason.RELEASE_FINISHED.value)
        # 版本与历史结论不被改动
        self.assertEqual(self.devices.get_device("d0").firmware_version, "3.0.0")
        self.assertEqual(self.store.get_record("r1", "d0").status,
                         UpgradeStatus.SUCCEEDED)

    def test_late_report_from_old_round_does_not_pollute_new_active_round(self):
        # r1 完成、r2 进行中（d0 已属 r2）：显式对 r1 的迟到上报不得写到 r2
        self._devices(version="1.0.0")
        self._firmwares_v2_v3()
        self.releases.create_release("r1", "fw2", "g", 10, 1.0)
        self.releases.advance("r1")
        self._succeed_all("r1", ["d0", "d1", "d2"])
        self.releases.create_release("r2", "fw3", "g", 10, 1.0)
        self.releases.advance("r2")
        self.assertEqual(self.store.get_active("d0"), "r2")

        # 旧一轮迟到的非终态上报 -> 按旧轮已完成拒绝
        reason = self.reject_reason(
            lambda: self.reports.report_result(
                "d0", ReportStage.DOWNLOAD, release_id="r1"))
        self.assertEqual(reason, RejectReason.RELEASE_FINISHED.value)

        # 旧一轮迟到的重复 SUCCESS 幂等：不改版本、不改 r2 记录
        view = self.reports.report_result(
            "d0", ReportStage.SUCCESS, release_id="r1")
        self.assertEqual(view.firmware_version, "2.0.0")  # r2 尚未成功，设备仍在 2.0.0
        r2_rec = self.store.get_record("r2", "d0")
        self.assertEqual(r2_rec.status, UpgradeStatus.PENDING)
        self.assertIsNone(r2_rec.last_stage)
        self.assertEqual(r2_rec.reported_count, 0)

        # 当前轮 r2 的正常上报仍然被接受并推进
        accepted = self.reports.report_result(
            "d0", ReportStage.DOWNLOAD, release_id="r2")
        self.assertEqual(accepted.upgrade_status, UpgradeStatus.DOWNLOADING)
        self.assertEqual(self.store.get_record("r2", "d0").last_stage,
                         ReportStage.DOWNLOAD)

    def test_late_report_after_rollback_is_rejected_as_rolling_back(self):
        self._devices()
        self._firmwares_v2_v3()
        self.releases.create_release("r1", "fw2", "g", 10, 1.0)
        self.releases.advance("r1")
        self.reports.report_result("d1", ReportStage.INSTALL, release_id="r1")
        self.releases.rollback("r1")

        # 回滚后旧一轮迟到上报（显式轮次与无轮次回看都应可区分拒绝）
        reason = self.reject_reason(
            lambda: self.reports.report_result(
                "d1", ReportStage.REBOOT, release_id="r1"))
        self.assertEqual(reason, RejectReason.RELEASE_ROLLING_BACK.value)
        reason2 = self.reject_reason(
            lambda: self.reports.report_result("d1", ReportStage.REBOOT))
        self.assertEqual(reason2, RejectReason.RELEASE_ROLLING_BACK.value)

    def test_explicit_unknown_or_unrelated_release_rejected(self):
        self._devices()
        self._firmwares_v2_v3()
        self.releases.create_release("r1", "fw2", "g", 10, 1.0)
        self.releases.advance("r1")

        self.assertEqual(
            self.reject_reason(lambda: self.reports.report_result(
                "d0", ReportStage.DOWNLOAD, release_id="ghost")),
            RejectReason.RELEASE_NOT_FOUND.value,
        )
        # 设备没有参与该发布（其它分组设备 x0 对 r1 上报）
        self.register_device("x0", group="other", version="1.0.0")
        self.assertEqual(
            self.reject_reason(lambda: self.reports.report_result(
                "x0", ReportStage.DOWNLOAD, release_id="r1")),
            RejectReason.DEVICE_NOT_IN_TARGET_GROUP.value,
        )

    # ------------------------------------------------------------------
    # 升级历史查询：可回看、结论一致、幂等
    # ------------------------------------------------------------------
    def test_release_history_is_complete_stable_and_marks_active_round(self):
        self._devices(version="1.0.0")
        self._firmwares_v2_v3()
        self.releases.create_release("r1", "fw2", "g", 10, 1.0)
        self.releases.advance("r1")
        self._succeed_all("r1", ["d0", "d1", "d2"])
        self.releases.create_release("r2", "fw3", "g", 10, 1.0)
        self.releases.advance("r2")  # r2 进行中
        self.reports.report_result("d0", ReportStage.INSTALL, release_id="r2")

        def snapshot():
            return self.devices.release_history("d0")

        first = snapshot()
        self.assertEqual(len(first), 2)
        r1_item, r2_item = first
        self.assertIsInstance(r1_item, DeviceReleaseHistoryItem)
        self.assertEqual(r1_item.release_state, ReleaseState.COMPLETED)
        self.assertFalse(r1_item.active)
        self.assertTrue(r2_item.active)
        self.assertEqual(r2_item.status, UpgradeStatus.INSTALLING)
        self.assertEqual(r2_item.from_version, "2.0.0")
        self.assertEqual(r2_item.target_version, "3.0.0")

        # 重复查询结论严格一致
        for _ in range(3):
            again = snapshot()
            self.assertEqual(
                [(h.release_id, h.status, h.active, h.target_version) for h in again],
                [(h.release_id, h.status, h.active, h.target_version) for h in first],
            )

        # 查询不改状态
        self.assertEqual(
            self.store.get_record("r2", "d0").status, UpgradeStatus.INSTALLING)

    def test_history_excludes_merely_skipped_round(self):
        self._devices()
        self._firmwares_v2_v3()
        self.releases.create_release("r1", "fw2", "g", 10, 1.0)
        self.releases.advance("r1")  # r1 占住设备
        self.releases.create_release("r2", "fw3", "g", 10, 1.0)
        self.releases.advance("r2")  # r2 全部跳过，没有真正分配
        hist = self.devices.release_history("d0")
        self.assertEqual([h.release_id for h in hist], ["r1"])

"""设备跨发布复用与连续升级：

- 发布收尾（完成/回滚）后设备重新可分配，下一轮能真正分进去；
- 进行中发布不被抢占，跳过原因可解释（被哪个发布占用）；
- 连续两轮升级的版本基线取自设备当时实际生效版本；
- 旧一轮迟到的上报被区分拒绝，不污染新一轮、不改回版本；
- 设备可查询历次发布历史，重复查询结论一致。
"""

import threading

from ota.errors import RejectReason
from ota.models import ReleaseState, ReportStage, UpgradeStatus
from tests.support import OtaTestCase


class ContinuousRolloutTest(OtaTestCase):
    def _three_devices(self):
        self.register_devices(["d0", "d1", "d2"], group="g", version="1.0.0")

    def _run_release_to_completion(self, release_id: str, firmware_id: str):
        self.create_and_open(release_id, firmware_id=firmware_id,
                             batch_size=10, failure_threshold=1.0)
        for d in ("d0", "d1", "d2"):
            self.reports.report_result(d, ReportStage.SUCCESS)
        view = self.releases.get_release(release_id)
        self.assertEqual(view.state, ReleaseState.COMPLETED)
        return view

    # ---- 1) 一轮正常结束后，同一批设备能再次进入新一轮并被真正分配 ----

    def test_devices_reassigned_after_release_completes(self):
        self._three_devices()
        self.add_firmware("fw2", "2.0.0", "1.0.0")
        self._run_release_to_completion("r1", "fw2")
        for d in ("d0", "d1", "d2"):
            self.assertIsNone(self.store.get_active(d))  # 占用已释放

        self.add_firmware("fw3", "3.0.0", "2.0.0")
        view = self.create_and_open("r2", firmware_id="fw3",
                                    batch_size=10, failure_threshold=1.0)
        # 真正分进去：不是全部跳过
        self.assertEqual(view.assigned, 3)
        self.assertEqual(view.skipped_device_ids, [])
        for d in ("d0", "d1", "d2"):
            self.assertEqual(self.store.get_active(d), "r2")
            self.reports.report_result(d, ReportStage.SUCCESS)
            self.assertEqual(self.devices.get_device(d).firmware_version, "3.0.0")
        self.assertEqual(self.releases.get_release("r2").state, ReleaseState.COMPLETED)

    # ---- 2) 前一轮还在进行中时，新一轮不会抢占设备 ----

    def test_in_progress_release_is_not_preempted(self):
        self.register_devices(["d0", "d1", "d2", "d3"], group="g", version="1.0.0")
        self.add_firmware("fw2", "2.0.0", "1.0.0")
        self.releases.create_release("r1", "fw2", "g", 2, 1.0)
        self.releases.advance("r1")  # r1 占用 d0,d1

        self.releases.create_release("r2", "fw2", "g", 2, 1.0)
        v2 = self.releases.advance("r2")  # 批次0 = d0,d1 被 r1 占用
        self.assertEqual(v2.assigned, 0)
        self.assertEqual(v2.skipped_device_ids, ["d0", "d1"])
        # 查询能看出被哪个进行中发布占着，而不是笼统一句跳过
        self.assertEqual(
            v2.skipped,
            [{"device_id": "d0", "occupied_by": "r1"},
             {"device_id": "d1", "occupied_by": "r1"}],
        )
        v2 = self.releases.advance("r2")  # 批次1 = d2,d3 空闲，正常分配
        self.assertEqual(v2.assigned, 2)

        # r1 的占用与记录未被 r2 抢走
        for d in ("d0", "d1"):
            self.assertEqual(self.store.get_active(d), "r1")
            self.assertIsNone(self.store.get_record("r2", d))

        # r1 正常走完并收尾
        for d in ("d0", "d1"):
            self.reports.report_result(d, ReportStage.SUCCESS)
        self.releases.advance("r1")  # 打开批次1：d2,d3 已被 r2 占用则跳过
        for d in ("d2", "d3"):
            self.reports.report_result(d, ReportStage.SUCCESS, release_id="r2")
        self.assertEqual(self.releases.get_release("r2").state, ReleaseState.COMPLETED)

        # 两条发布线都结束后，全部设备可进入再下一轮
        self.add_firmware("fw3", "3.0.0", "2.0.0")
        v3 = self.create_and_open("r3", firmware_id="fw3",
                                  batch_size=10, failure_threshold=1.0)
        self.assertEqual(v3.assigned, 4)
        self.assertEqual(v3.skipped_device_ids, [])

    # ---- 3) 回滚后设备不再被旧发布挡住 ----

    def test_devices_unblocked_after_rollback(self):
        self._three_devices()
        self.add_firmware("fw2", "2.0.0", "1.0.0")
        self.create_and_open("r1", firmware_id="fw2",
                             batch_size=10, failure_threshold=1.0)
        self.reports.report_result("d0", ReportStage.INSTALL)
        self.releases.rollback("r1")
        for d in ("d0", "d1", "d2"):
            self.assertIsNone(self.store.get_active(d))
            self.assertEqual(self.devices.get_device(d).firmware_version, "1.0.0")

        view = self.create_and_open("r2", firmware_id="fw2",
                                    batch_size=10, failure_threshold=1.0)
        self.assertEqual(view.assigned, 3)
        self.assertEqual(view.skipped_device_ids, [])

    # ---- 4) 连续两轮升级的版本基线正确 ----

    def test_consecutive_rounds_version_baseline(self):
        self._three_devices()
        self.add_firmware("fw2", "2.0.0", "1.0.0")
        self._run_release_to_completion("r1", "fw2")

        self.add_firmware("fw3", "3.0.0", "2.0.0")
        self.create_and_open("r2", firmware_id="fw3",
                             batch_size=10, failure_threshold=1.0)
        # 新一轮基线 = 设备当时实际生效版本（2.0.0），不串用上一轮记录
        for d in ("d0", "d1", "d2"):
            rec = self.store.get_record("r2", d)
            self.assertEqual(rec.from_version, "2.0.0")
            self.reports.report_result(d, ReportStage.SUCCESS)
        self.assertEqual(self.releases.get_release("r2").state, ReleaseState.COMPLETED)

        # 设备侧能查到历次发布：每轮目标版本与最终结果
        history = self.devices.get_upgrade_history("d0")
        self.assertEqual([h.release_id for h in history], ["r1", "r2"])
        self.assertEqual(
            [(h.from_version, h.target_version, h.status) for h in history],
            [("1.0.0", "2.0.0", UpgradeStatus.SUCCEEDED),
             ("2.0.0", "3.0.0", UpgradeStatus.SUCCEEDED)],
        )
        # 重复查询结论一致
        again = self.devices.get_upgrade_history("d0")
        self.assertEqual(
            [(h.release_id, h.status, h.target_version) for h in again],
            [(h.release_id, h.status, h.target_version) for h in history],
        )

    # ---- 5) 旧一轮迟到的上报被区分拒绝，不污染新一轮 ----

    def test_late_report_from_previous_round_rejected(self):
        self._three_devices()
        self.add_firmware("fw2", "2.0.0", "1.0.0")
        self._run_release_to_completion("r1", "fw2")  # 全部升到 2.0.0

        self.add_firmware("fw3", "3.0.0", "2.0.0")
        self.create_and_open("r2", firmware_id="fw3",
                             batch_size=10, failure_threshold=1.0)

        # 旧一轮迟到的中间阶段上报：按“发布已完成”区分拒绝
        reason = self.reject_reason(
            lambda: self.reports.report_result("d0", ReportStage.DOWNLOAD,
                                               release_id="r1"))
        self.assertEqual(reason, RejectReason.RELEASE_FINISHED.value)
        # 旧一轮迟到的 FAIL：同样拒绝，绝不影响新一轮记录与设备版本
        reason = self.reject_reason(
            lambda: self.reports.report_result("d0", ReportStage.FAIL,
                                               failure_reason="late", release_id="r1"))
        self.assertEqual(reason, RejectReason.RELEASE_FINISHED.value)
        rec_r2 = self.store.get_record("r2", "d0")
        self.assertEqual(rec_r2.status, UpgradeStatus.PENDING)
        self.assertIsNone(rec_r2.last_stage)
        self.assertEqual(self.devices.get_device("d0").firmware_version, "2.0.0")

        # 新一轮正常推进到 3.0.0
        self.reports.report_result("d0", ReportStage.SUCCESS)
        self.assertEqual(self.devices.get_device("d0").firmware_version, "3.0.0")
        # 旧一轮迟到的重复 SUCCESS：幂等接受但绝不把设备改回旧版本
        view = self.reports.report_result("d0", ReportStage.SUCCESS, release_id="r1")
        self.assertEqual(view.firmware_version, "3.0.0")
        self.assertEqual(self.store.get_record("r2", "d0").status,
                         UpgradeStatus.SUCCEEDED)

    def test_late_report_after_rollback_rejected_as_rolling_back(self):
        self._three_devices()
        self.add_firmware("fw2", "2.0.0", "1.0.0")
        self.create_and_open("r1", firmware_id="fw2",
                             batch_size=10, failure_threshold=1.0)
        self.reports.report_result("d0", ReportStage.DOWNLOAD)
        self.releases.rollback("r1")
        # 旧一轮（已回滚）迟到的上报：可区分为“回滚中”
        reason = self.reject_reason(
            lambda: self.reports.report_result("d0", ReportStage.VERIFY,
                                               release_id="r1"))
        self.assertEqual(reason, RejectReason.RELEASE_ROLLING_BACK.value)
        self.assertEqual(self.devices.get_device("d0").firmware_version, "1.0.0")

    # ---- 6) 并发推进：同一设备不会被两条发布线同时占用 ----

    def test_concurrent_releases_never_share_a_device(self):
        ids = [f"d{i}" for i in range(6)]
        self.register_devices(ids, group="g", version="1.0.0")
        self.add_firmware("fw2", "2.0.0", "1.0.0")
        self.releases.create_release("r1", "fw2", "g", 10, 1.0)
        self.releases.create_release("r2", "fw2", "g", 10, 1.0)

        barrier = threading.Barrier(8)

        def worker(release_id: str) -> None:
            barrier.wait()
            self.releases.advance(release_id)

        threads = [
            threading.Thread(target=worker, args=(rid,))
            for rid in ("r1", "r2") for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 每台设备在全系统至多一条记录，且占用指针与记录所属发布一致
        for d in ids:
            owners = [rid for rid in ("r1", "r2")
                      if self.store.get_record(rid, d) is not None]
            self.assertLessEqual(len(owners), 1)
            active = self.store.get_active(d)
            if owners:
                self.assertEqual(active, owners[0])
            else:
                self.assertIsNone(active)
        total = sum(len(self.store.list_records(rid)) for rid in ("r1", "r2"))
        self.assertEqual(total, 6)  # 不漏不重：6 台设备恰好 6 条记录

    # ---- 7) 历史查询边界 ----

    def test_history_empty_for_never_assigned_device(self):
        self._three_devices()
        self.assertEqual(self.devices.get_upgrade_history("d0"), [])
        reason = self.reject_reason(
            lambda: self.devices.get_upgrade_history("ghost"))
        self.assertEqual(reason, RejectReason.DEVICE_NOT_FOUND.value)

    # ---- 8) 上报指向未分配该设备的进行中发布：可区分拒绝 ----

    def test_report_for_release_not_owning_device_rejected(self):
        self._three_devices()
        self.add_firmware("fw2", "2.0.0", "1.0.0")
        self.create_and_open("r1", firmware_id="fw2",
                             batch_size=10, failure_threshold=1.0)
        # r2 存在但从未分配到 d0，而 d0 正被 r1 占用
        self.releases.create_release("r2", "fw2", "g", 10, 1.0)
        reason = self.reject_reason(
            lambda: self.reports.report_result("d0", ReportStage.DOWNLOAD,
                                               release_id="r2"))
        self.assertEqual(reason, RejectReason.DEVICE_IN_ANOTHER_ACTIVE_RELEASE.value)
        # 拒绝不产生任何状态变化
        rec = self.store.get_record("r1", "d0")
        self.assertEqual(rec.status, UpgradeStatus.PENDING)
        self.assertIsNone(rec.last_stage)

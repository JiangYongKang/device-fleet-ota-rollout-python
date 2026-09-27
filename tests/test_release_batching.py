"""灰度批次推进：幂等、并发安全、不重复分配、不漏批/不跳批、设备唯一占用。"""

import threading

from ota.errors import RejectReason
from ota.models import BatchState, ReportStage, ReleaseState
from tests.support import OtaTestCase


class ReleaseBatchingTest(OtaTestCase):
    def _five_devices(self):
        self.register_devices([f"d{i}" for i in range(5)])
        self.add_firmware()

    def test_targets_snapshot_is_sorted_and_deterministic(self):
        self.register_devices(["d9", "d1", "d5", "d3", "d0"])
        self.add_firmware()
        view = self.releases.create_release("r1", "fw", "g", 2, 1.0)
        self.assertEqual(view.total_targets, 5)
        self.assertEqual(
            [d for b in view.batches for d in b["device_ids"]],
            ["d0", "d1", "d3", "d5", "d9"],
        )
        self.assertEqual([len(b["device_ids"]) for b in view.batches], [2, 2, 1])

    def test_advance_opens_one_batch_at_a_time(self):
        self._five_devices()
        self.releases.create_release("r1", "fw", "g", 2, 1.0)
        view = self.releases.advance("r1")
        self.assertEqual(view.state, ReleaseState.IN_PROGRESS)
        self.assertEqual(view.assigned, 2)
        self.assertEqual(view.next_batch_to_open, 1)
        self.assertEqual(view.batches[0]["state"], BatchState.OPEN.value)
        self.assertEqual(view.batches[1]["state"], BatchState.PENDING.value)

    def test_advance_blocked_until_current_batch_terminal(self):
        self._five_devices()
        self.releases.create_release("r1", "fw", "g", 2, 1.0)
        self.releases.advance("r1")  # 批次0进行中
        again = self.releases.advance("r1")
        self.assertEqual(again.assigned, 2)              # 没有新增分配
        self.assertEqual(again.next_batch_to_open, 1)    # 未跳过批次
        self.assertEqual(again.batches[1]["state"], BatchState.PENDING.value)

    def test_no_batch_skipped_and_each_device_assigned_once(self):
        self._five_devices()
        self.releases.create_release("r1", "fw", "g", 2, 1.0)
        self.releases.advance("r1")  # d0,d1

        # 完成批次0
        for d in ("d0", "d1"):
            self.reports.report_result(d, ReportStage.SUCCESS)
        self.releases.advance("r1")  # d2,d3
        for d in ("d2", "d3"):
            self.reports.report_result(d, ReportStage.SUCCESS)
        self.releases.advance("r1")  # d4
        self.reports.report_result("d4", ReportStage.SUCCESS)

        final = self.releases.get_release("r1")
        self.assertEqual(final.state, ReleaseState.COMPLETED)
        self.assertEqual(final.succeeded, 5)
        self.assertEqual([b["state"] for b in final.batches],
                         [BatchState.DONE.value] * 3)
        # 每台设备恰好一条记录
        records = self.store.list_records("r1")
        self.assertEqual(sorted(r.device_id for r in records),
                         [f"d{i}" for i in range(5)])

    def test_repeated_advance_after_all_batches_is_idempotent(self):
        self._five_devices()
        self.releases.create_release("r1", "fw", "g", 10, 1.0)
        self.releases.advance("r1")
        for d in [f"d{i}" for i in range(5)]:
            self.reports.report_result(d, ReportStage.SUCCESS)
        v1 = self.releases.advance("r1")
        v2 = self.releases.advance("r1")
        self.assertEqual(v1.state, ReleaseState.COMPLETED)
        self.assertEqual(v2.state, ReleaseState.COMPLETED)
        self.assertEqual(v2.assigned, 5)

    def test_concurrent_advance_opens_exactly_one_batch(self):
        self._five_devices()
        self.releases.create_release("r1", "fw", "g", 2, 1.0)
        assigned: list[int] = []
        barrier = threading.Barrier(16)

        def worker() -> None:
            barrier.wait()
            assigned.append(self.releases.advance("r1").assigned)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 无论多少线程并发，批次0只打开一次，只分配 d0,d1 两台
        self.assertEqual(sorted(set(assigned)), [2])
        self.assertEqual(len(self.store.list_records("r1")), 2)
        self.assertEqual(self.releases.get_release("r1").next_batch_to_open, 1)

    def test_device_cannot_belong_to_two_active_releases(self):
        self._five_devices()
        self.add_firmware()  # 仅确保存在；复用同一固件
        self.releases.create_release("r1", "fw", "g", 2, 1.0)
        self.releases.advance("r1")  # r1 占用 d0,d1

        self.releases.create_release("r2", "fw", "g", 2, 1.0)
        v2 = self.releases.advance("r2")  # r2 批次0 = d0,d1 全部被占用
        self.assertEqual(v2.assigned, 0)
        self.assertEqual(v2.skipped_device_ids, ["d0", "d1"])
        self.assertEqual(v2.batches[0]["state"], BatchState.DONE.value)

        # r2 继续推进到批次1：d2,d3 尚未被占用，可正常分配
        v2 = self.releases.advance("r2")
        self.assertEqual(v2.assigned, 2)
        self.assertEqual(v2.skipped_device_ids, ["d0", "d1"])
        self.assertEqual(self.store.get_active("d2"), "r2")
        self.assertIsNone(self.store.get_record("r1", "d2"))

    def test_paused_release_rejects_advance(self):
        self._five_devices()
        self.releases.create_release("r1", "fw", "g", 2, 1.0)
        self.releases.advance("r1")
        self.releases.pause("r1")
        reason = self.reject_reason(lambda: self.releases.advance("r1"))
        self.assertEqual(reason, RejectReason.RELEASE_PAUSED.value)

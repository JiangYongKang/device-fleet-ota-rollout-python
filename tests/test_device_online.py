"""在线状态判定、超时、乱序/重复/并发心跳的一致性测试。"""

import threading

from ota.errors import RejectReason
from tests.support import OtaTestCase


class DeviceOnlineTest(OtaTestCase):
    def test_registered_device_offline_before_first_heartbeat(self):
        view = self.register_device("d1")
        self.assertFalse(view.online)
        self.assertIsNone(view.last_heartbeat_ts)
        self.assertEqual(view.heartbeat_count, 0)

    def test_heartbeat_marks_online_and_timeout_boundary(self):
        self.register_device("d1")
        view = self.devices.heartbeat("d1")  # ts = 1000
        self.assertTrue(view.online)

        self.clock.advance(29)
        self.assertTrue(self.devices.get_device("d1").online)  # 未超时

        self.clock.advance(1)  # 30s，到达超时边界（>= timeout 判离线）
        self.assertFalse(self.devices.get_device("d1").online)

    def test_stale_heartbeat_does_not_overwrite_or_revive(self):
        self.register_device("d1")
        self.devices.heartbeat("d1", ts=1000.0)
        self.clock.set(1030.0)  # 已超时
        self.assertFalse(self.devices.get_device("d1").online)

        # 旧心跳（乱序/迟到）不能复活，也不能覆盖 last_heartbeat_ts
        view = self.devices.heartbeat("d1", ts=500.0)
        self.assertFalse(view.online)
        self.assertEqual(view.last_heartbeat_ts, 1000.0)
        self.assertEqual(view.heartbeat_count, 1)  # 不计入重复旧心跳

        # 与已记录时间戳相同的重复心跳同样幂等忽略
        view = self.devices.heartbeat("d1", ts=1000.0)
        self.assertEqual(view.heartbeat_count, 1)
        self.assertEqual(view.last_heartbeat_ts, 1000.0)

        # 更新的心跳才生效
        view = self.devices.heartbeat("d1", ts=1030.0)
        self.assertTrue(view.online)
        self.assertEqual(view.heartbeat_count, 2)

    def test_concurrent_heartbeats_oldest_never_overwrites(self):
        # 20 个线程分别提交不同时间戳；并发交错下计数可能小于提交数，
        # 但核心不变量必须成立：最终 last_heartbeat_ts 一定是最大时间戳，
        # 且任何已接受的更新时间戳都严格递增，绝不被旧值覆盖。
        self.register_device("d1")
        timestamps = [1000.0 + i for i in range(20)]
        errors: list[Exception] = []
        accepted: list[float] = []
        accepted_lock = threading.Lock()
        original = self.devices.heartbeat

        def tracked_heartbeat(ts: float):
            view = original("d1", ts=ts)
            if view.last_heartbeat_ts == ts:
                with accepted_lock:
                    accepted.append(ts)
            return view

        def worker(ts: float) -> None:
            try:
                tracked_heartbeat(ts)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in timestamps]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        view = self.devices.get_device("d1")
        # 不论并发提交顺序如何，last_heartbeat_ts 必须是最大值 1019
        self.assertEqual(view.last_heartbeat_ts, 1019.0)
        # 所有真正生效的更新按时间戳严格递增，无旧值覆盖
        self.assertEqual(accepted, sorted(accepted))
        self.assertEqual(len(set(accepted)), len(accepted))
        self.assertEqual(view.heartbeat_count, len(accepted))

    def test_concurrent_identical_heartbeats_counted_once(self):
        self.register_device("d1")
        self.devices.heartbeat("d1", ts=1000.0)

        def worker() -> None:
            self.devices.heartbeat("d1", ts=1000.0)  # 全部相同时间戳

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.devices.get_device("d1").heartbeat_count, 1)

    def test_rejection_reasons_distinguishable(self):
        self.assertEqual(
            self.reject_reason(lambda: self.devices.heartbeat("ghost")),
            RejectReason.DEVICE_NOT_FOUND.value,
        )
        self.register_device("dup")
        self.assertEqual(
            self.reject_reason(lambda: self.register_device("dup")),
            RejectReason.DEVICE_ALREADY_REGISTERED.value,
        )

    def test_repeated_query_is_stable(self):
        self.register_device("d1")
        self.devices.heartbeat("d1")
        first = self.devices.get_device("d1")
        second = self.devices.get_device("d1")
        self.assertEqual(first.online, second.online)
        self.assertEqual(first.last_heartbeat_ts, second.last_heartbeat_ts)

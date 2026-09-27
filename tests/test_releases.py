"""灰度发布推进 / 暂停 / 失败阈值测试。"""

from __future__ import annotations

import logging
import threading

import pytest

from ota.clock import FakeClock
from ota.errors import RejectCode
from ota.models import PauseReason, ReleaseState, Stage
from ota.service import OtaService
from ota.storage import InMemoryStore

log = logging.getLogger("ota.test.releases")


@pytest.fixture
def env():
    clock = FakeClock(start=1000.0)
    svc = OtaService(InMemoryStore(), clock, heartbeat_timeout_seconds=30.0)
    for i in range(10):
        svc.register_device(f"d{i}", "g1", "1.0.0")
    svc.create_firmware("fw1", "2.0.0", "1.0.0")
    return svc, clock


def _release(svc, rid):
    return svc.store.get_release(rid)


def test_batches_allocate_exactly_once_and_in_order(env):
    svc, clock = env
    svc.create_release("r1", "fw1", "g1", [2, 3, 5], failure_threshold=1.0)

    r0 = svc.advance_batch("r1", "req-1")
    assert r0.ok and r0.batch_index == 0 and len(r0.device_ids) == 2

    # 同一 request_id 重复推进 => 幂等回放，不分配第二批
    r0dup = svc.advance_batch("r1", "req-1")
    assert r0dup.batch_index == 0
    assert r0dup.device_ids == r0.device_ids

    r1 = svc.advance_batch("r1", "req-2")
    r2 = svc.advance_batch("r1", "req-3")
    assert r1.batch_index == 1 and len(r1.device_ids) == 3
    assert r2.batch_index == 2 and len(r2.device_ids) == 5

    # 无重复、无遗漏：10 台设备各一次
    all_devs = r0.device_ids + r1.device_ids + r2.device_ids
    assert sorted(all_devs) == [f"d{i}" for i in range(10)]
    assert len(set(all_devs)) == 10

    # 批次发完 => ROLLOUT_COMPLETE，再推进被拒
    r3 = svc.advance_batch("r1", "req-4")
    assert r3.state == ReleaseState.ROLLOUT_COMPLETE
    assert r3.code is RejectCode.RELEASE_NOT_RUNNING
    log.info("basis=plan=[2,3,5]; allocated=%s; final=%s", all_devs, r3.state.value)


def test_concurrent_advance_is_serialized_no_skip_no_dup(env):
    svc, clock = env
    svc.create_release("r1", "fw1", "g1", [2, 2, 2, 2, 2], failure_threshold=1.0)

    n = 10
    barrier = threading.Barrier(n)
    results: list = []

    def worker(i: int) -> None:
        barrier.wait()
        results.append(svc.advance_batch("r1", f"req-{i}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok = [r for r in results if r.ok]
    indexes = sorted(r.batch_index for r in ok)
    log.info(
        "input=10 concurrent advances on plan=[2]*5; basis=lock + request_id; "
        "batch_indexes=%s",
        indexes,
    )
    # 恰好 5 个批次成功，下标 0..4 每个恰好一次（不跳过、不重复）
    assert indexes == [0, 1, 2, 3, 4]
    allocated = [d for r in ok for d in r.device_ids]
    assert len(allocated) == 10 and len(set(allocated)) == 10

    release = _release(svc, "r1")
    assert release.state == ReleaseState.ROLLOUT_COMPLETE


def test_concurrent_advance_same_request_id_allocate_once(env):
    svc, clock = env
    svc.create_release("r1", "fw1", "g1", [4, 6], failure_threshold=1.0)

    n = 8
    barrier = threading.Barrier(n)
    results: list = []

    def worker() -> None:
        barrier.wait()
        # 8 个并发请求全部使用同一个 request_id
        results.append(svc.advance_batch("r1", "same-req"))

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log.info(
        "input=8 concurrent advances request_id=same; "
        "distinct allocations=%s",
        {tuple(r.device_ids) for r in results},
    )
    assert all(r.ok for r in results)
    assert all(r.batch_index == 0 for r in results)
    assert len({tuple(r.device_ids) for r in results}) == 1
    release = _release(svc, "r1")
    assert len(release.allocations) == 1  # 只分配了一个批次


def test_device_cannot_belong_to_two_active_releases(env):
    svc, clock = env
    svc.create_release("r1", "fw1", "g1", [3, 7], failure_threshold=1.0)
    svc.create_release("r2", "fw1", "g1", [3, 7], failure_threshold=1.0)

    a1 = svc.advance_batch("r1", "r1-b0")
    assert len(a1.device_ids) == 3
    # r2 此时能拿到的设备是剩余 7 台（r1 占用的 3 台被跳过）
    a2 = svc.advance_batch("r2", "r2-b0")
    assert len(a2.device_ids) == 3
    assert set(a2.device_ids).isdisjoint(a1.device_ids)
    log.info(
        "basis=r1 holds %s; r2 got %s (busy skipped)",
        a1.device_ids, a2.device_ids,
    )

    # r2 再推进：只剩 4 台空闲 => 分配 4 台（批次不饱和也算一次推进）
    a3 = svc.advance_batch("r2", "r2-b1")
    assert len(a3.device_ids) == 4
    assert set(a3.device_ids).isdisjoint(a1.device_ids)

    # 视图中每台设备的归属唯一
    owners = {}
    for i in range(10):
        v = svc.get_device_view(f"d{i}")
        assert v.active_release_id is not None
        owners.setdefault(v.active_release_id, []).append(v.device_id)
    assert set(owners["r1"]).isdisjoint(owners["r2"])
    assert len(owners["r1"]) + len(owners["r2"]) == 10


def _succeed(svc, dev, rid):
    for stage in (Stage.DOWNLOAD, Stage.VERIFY, Stage.INSTALL, Stage.REBOOT):
        r = svc.report_upgrade(dev, rid, stage, success=True)
        assert r.ok, (r.code, r.message)
    return r


def test_failure_threshold_auto_pauses(env):
    svc, clock = env
    # 第一批 2 台，阈值 0.5 => 1/2 失败即触发
    svc.create_release("r1", "fw1", "g1", [2, 8], failure_threshold=0.5)
    b0 = svc.advance_batch("r1", "b0")
    d_fail, d_ok = b0.device_ids

    svc.report_upgrade(d_fail, "r1", Stage.DOWNLOAD, False, "disk full")
    _succeed(svc, d_ok, "r1")

    # 推进第二批时，失败比例 1/2 == 0.5 => 自动暂停，不分配任何新设备
    r = svc.advance_batch("r1", "b1")
    release = _release(svc, "r1")
    assert r.ok is False and r.auto_paused is True
    assert r.pause_reason == PauseReason.FAILURE_THRESHOLD.value
    assert release.state == ReleaseState.PAUSED
    assert release.pause_reason is PauseReason.FAILURE_THRESHOLD
    assert len(release.allocations) == 1
    log.info("auto-pause basis=%s", release.pause_detail)
    assert "1/2" in release.pause_detail

    # 阈值未解除前，继续推进仍然被拒（不会悄悄恢复）
    again = svc.advance_batch("r1", "b1-retry")
    assert again.state == ReleaseState.PAUSED
    assert again.code is RejectCode.RELEASE_NOT_RUNNING

    # resume 不允许绕过自动暂停；rearm 之后才能继续
    assert svc.resume_release("r1").code is RejectCode.RELEASE_NOT_RUNNING
    assert svc.rearm_release("r1").ok
    assert svc.advance_batch("r1", "b1").ok


def test_manual_pause_and_resume_flow(env):
    svc, clock = env
    svc.create_release("r1", "fw1", "g1", [2, 8], failure_threshold=1.0)
    svc.advance_batch("r1", "b0")
    p = svc.pause_release("r1")
    assert p.state == ReleaseState.PAUSED
    assert svc.advance_batch("r1", "b1").code is RejectCode.RELEASE_NOT_RUNNING
    # 暂停期间设备上报也被拒，且原因可区分
    rep = svc.report_upgrade("d0", "r1", Stage.DOWNLOAD, True)
    assert rep.code is RejectCode.RELEASE_PAUSED
    assert svc.resume_release("r1").state == ReleaseState.IN_PROGRESS
    assert svc.advance_batch("r1", "b1").ok

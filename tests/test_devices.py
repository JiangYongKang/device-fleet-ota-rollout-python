"""设备注册 / 心跳在线判定测试。

每个用例都打印输入与判定依据（FakeClock 时间、心跳间隔、超时阈值），
方便复现“为什么判在线/离线/拒绝”。
"""

from __future__ import annotations

import logging
import threading

import pytest

from ota.clock import FakeClock
from ota.errors import OtaError, RejectCode
from ota.service import OtaService
from ota.storage import InMemoryStore

log = logging.getLogger("ota.test.devices")


@pytest.fixture
def env():
    clock = FakeClock(start=1000.0)
    svc = OtaService(InMemoryStore(), clock, heartbeat_timeout_seconds=30.0)
    return svc, clock


def _view(svc: OtaService, dev: str):
    v = svc.get_device_view(dev)
    log.info(
        "view device=%s online=%s version=%s last_hb=%s owner=%s state=%s stage=%s",
        dev, v.online, v.firmware_version, v.last_heartbeat_at,
        v.active_release_id, v.assignment_state, v.assignment_stage,
    )
    return v


def test_never_heartbeat_is_offline(env):
    svc, clock = env
    svc.register_device("d1", "g1", "1.0.0")
    log.info("input=no heartbeat; basis=timeout=30s => expect OFFLINE")
    assert _view(svc, "d1").online is False


def test_fresh_heartbeat_online_then_timeout_offline(env):
    svc, clock = env
    svc.register_device("d1", "g1", "1.0.0")

    r = svc.heartbeat("d1")
    log.info("input=hb@%s => accepted=%s online=%s", clock.now, r.accepted, r.online)
    assert r.accepted and r.online is True

    clock.advance(29.9)
    assert _view(svc, "d1").online is True  # 未超时

    clock.advance(0.1)  # 恰好 30s：临界相等 => 离线
    v = _view(svc, "d1")
    log.info("input=elapsed==timeout(30s); basis=strict '<' => online=%s", v.online)
    assert v.online is False


def test_stale_heartbeat_does_not_overwrite_expiry(env):
    svc, clock = env
    svc.register_device("d1", "g1", "1.0.0")
    svc.heartbeat("d1", ts=1000.0)
    clock.advance(100)  # 当前时间 1100，设备已离线

    r = svc.heartbeat("d1", ts=1005.0)  # 比已记录的 1000 新 -> 接受但仍离线
    assert r.accepted is True and r.online is False

    stale = svc.heartbeat("d1", ts=900.0)  # 乱序旧心跳 -> 拒绝
    log.info(
        "input=stale hb ts=900 vs last=1005 => accepted=%s code=%s online=%s",
        stale.accepted, stale.code, stale.online,
    )
    assert stale.accepted is False
    assert stale.code is RejectCode.STALE_HEARTBEAT
    # 过期时间没有被旧数据覆盖
    view = _view(svc, "d1")
    assert view.last_heartbeat_at == 1005.0
    assert view.online is False


def test_duplicate_same_timestamp_is_idempotent(env):
    svc, clock = env
    svc.register_device("d1", "g1", "1.0.0")
    r1 = svc.heartbeat("d1", ts=1000.0)
    r2 = svc.heartbeat("d1", ts=1000.0)  # 完全重复
    assert r1.accepted and r2.accepted
    assert _view(svc, "d1").last_heartbeat_at == 1000.0


def test_concurrent_heartbeats_never_go_backwards(env):
    svc, clock = env
    svc.register_device("d1", "g1", "1.0.0")
    svc.heartbeat("d1", ts=1000.0)
    clock.advance(100)

    # 并发：一批“旧”心跳与一批“新”心跳同时到达
    timestamps = [900.0, 950.0, 1200.0, 1100.0, 1300.0, 1050.0] * 4
    outcomes: list[tuple[float, object]] = []
    barrier = threading.Barrier(len(timestamps))

    def worker(ts: float) -> None:
        barrier.wait()
        outcomes.append((ts, svc.heartbeat("d1", ts=ts)))

    threads = [threading.Thread(target=worker, args=(t,)) for t in timestamps]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    results = [r for _, r in outcomes]
    accepted_ts = sorted(r.last_heartbeat_at for r in results if r.accepted)
    rejected_ts = sorted(ts for ts, r in outcomes if not r.accepted)
    log.info(
        "input=24 concurrent hbs mixed stale/new; basis=max-wins under lock; "
        "accepted=%s rejected=%s",
        accepted_ts, rejected_ts,
    )
    # 关键不变量：并发下记录值只可能推进到所有上报中的最大值，绝不倒退
    assert _view(svc, "d1").last_heartbeat_at == 1300.0
    # 被接受的上报都必须 >= 初始值 1000；900/950 这种旧数据绝不可能被接受
    assert all(t >= 1000.0 for t in accepted_ts)
    assert not any(t < 1000.0 for t in accepted_ts)
    # 所有拒绝都必须以可区分的 STALE_HEARTBEAT 原因返回
    assert all(
        r.code is RejectCode.STALE_HEARTBEAT
        for r in results
        if not r.accepted
    )


def test_heartbeat_unknown_device_rejected(env):
    svc, clock = env
    r = svc.heartbeat("ghost")
    assert r.accepted is False
    assert r.code is RejectCode.DEVICE_NOT_FOUND

    with pytest.raises(OtaError) as ei:
        svc.get_device_view("ghost")
    assert ei.value.code is RejectCode.DEVICE_NOT_FOUND

"""升级结果上报的拒绝原因区分、阶段顺序与回滚后状态一致性。"""

from __future__ import annotations

import logging
import threading

import pytest

from ota.clock import FakeClock
from ota.errors import RejectCode
from ota.models import AssignmentState, ReleaseState, Stage
from ota.service import OtaService
from ota.storage import InMemoryStore

log = logging.getLogger("ota.test.reports")


@pytest.fixture
def env():
    clock = FakeClock(start=1000.0)
    svc = OtaService(InMemoryStore(), clock, heartbeat_timeout_seconds=30.0)
    # g1: 满足最低版本；g2: 不在目标分组；d-low: 版本过低
    for i in range(4):
        svc.register_device(f"g1-{i}", "g1", "1.0.0")
    svc.register_device("g2-0", "g2", "1.0.0")
    svc.register_device("d-low", "g1", "0.9.0")
    svc.create_firmware("fw1", "2.0.0", "1.0.0")
    return svc, clock


def _succeed(svc, dev, rid):
    results = []
    for stage in (Stage.DOWNLOAD, Stage.VERIFY, Stage.INSTALL, Stage.REBOOT):
        r = svc.report_upgrade(dev, rid, stage, success=True)
        assert r.ok, (r.code, r.message)
        results.append(r)
    return results


def test_happy_path_upgrades_version_and_state(env):
    svc, clock = env
    svc.create_release("r1", "fw1", "g1", [6], failure_threshold=1.0)
    b0 = svc.advance_batch("r1", "b0")
    dev = "g1-0"
    assert dev in b0.device_ids

    states = [r.assignment_state for r in _succeed(svc, dev, "r1")]
    log.info("input=4 stage reports; basis=stage order; states=%s", states)
    assert states[:3] == [AssignmentState.IN_PROGRESS] * 3
    assert states[3] == AssignmentState.SUCCEEDED
    assert svc.get_device_view(dev).firmware_version == "2.0.0"


def test_failed_stage_is_distinguishable(env):
    svc, clock = env
    svc.create_release("r1", "fw1", "g1", [6], failure_threshold=1.0)
    b0 = svc.advance_batch("r1", "b0")
    dev = "g1-0"

    svc.report_upgrade(dev, "r1", Stage.DOWNLOAD, True)
    svc.report_upgrade(dev, "r1", Stage.VERIFY, True)
    r = svc.report_upgrade(dev, "r1", Stage.INSTALL, False, "checksum mismatch")
    assert r.ok
    assert r.assignment_state is AssignmentState.FAILED
    assert r.failed_stage is Stage.INSTALL  # 失败阶段被精确记录
    view = svc.get_device_view(dev)
    assert view.assignment_state is AssignmentState.FAILED
    assert view.assignment_stage is Stage.INSTALL


def test_stage_out_of_order_rejected(env):
    svc, clock = env
    svc.create_release("r1", "fw1", "g1", [6], failure_threshold=1.0)
    assert "g1-0" in svc.advance_batch("r1", "b0").device_ids
    dev = "g1-0"

    jump = svc.report_upgrade(dev, "r1", Stage.INSTALL, True)
    assert jump.code is RejectCode.STAGE_OUT_OF_ORDER

    svc.report_upgrade(dev, "r1", Stage.DOWNLOAD, True)
    # 同阶段重复成功 => 幂等回放
    back = svc.report_upgrade(dev, "r1", Stage.DOWNLOAD, True)
    assert back.ok
    # 同阶段重复报失败 / 回退到更早阶段 => 顺序冲突
    dup_fail = svc.report_upgrade(dev, "r1", Stage.DOWNLOAD, False)
    assert dup_fail.code is RejectCode.STAGE_OUT_OF_ORDER
    backward = svc.report_upgrade(dev, "r1", Stage.DOWNLOAD, True)
    assert backward.ok  # 仍幂等，状态未被失败上报污染
    assert backward.stage is Stage.DOWNLOAD


def test_reject_reasons_are_distinguishable(env):
    svc, clock = env
    svc.create_release("r1", "fw1", "g1", [1, 5], failure_threshold=1.0)
    allocated = svc.advance_batch("r1", "b0").device_ids[0]

    codes = {
        "unknown_device": svc.report_upgrade(
            "ghost", "r1", Stage.DOWNLOAD, True
        ).code,
        "unknown_release": svc.report_upgrade(
            allocated, "nope", Stage.DOWNLOAD, True
        ).code,
        "other_group": svc.report_upgrade(
            "g2-0", "r1", Stage.DOWNLOAD, True
        ).code,
        "not_allocated_yet": svc.report_upgrade(
            "g1-1", "r1", Stage.DOWNLOAD, True  # 候选但尚未进批次
        ).code,
        "low_version": svc.report_upgrade(
            "d-low", "r1", Stage.DOWNLOAD, True  # 第一批唯一被分配的是 d-low
        ).code,
    }
    log.info("reject reasons before/at allocation: %s", codes)
    assert codes["unknown_device"] is RejectCode.DEVICE_NOT_FOUND
    assert codes["unknown_release"] is RejectCode.RELEASE_NOT_FOUND
    assert codes["other_group"] is RejectCode.NOT_IN_TARGET_GROUP
    assert codes["not_allocated_yet"] is RejectCode.NO_ACTIVE_ASSIGNMENT
    assert codes["low_version"] is RejectCode.VERSION_BELOW_MINIMUM

    # 暂停 / 回滚中 / 已回滚 三种原因互不相同
    assert svc.pause_release("r1").ok
    paused = svc.report_upgrade(allocated, "r1", Stage.DOWNLOAD, True)
    assert paused.code is RejectCode.RELEASE_PAUSED

    assert svc.rollback_release("r1").state == ReleaseState.ROLLING_BACK
    rolling = svc.report_upgrade(allocated, "r1", Stage.DOWNLOAD, True)
    assert rolling.code is RejectCode.RELEASE_ROLLING_BACK

    assert svc.settle_rollback("r1").state == ReleaseState.ROLLED_BACK
    done = svc.report_upgrade(allocated, "r1", Stage.DOWNLOAD, True)
    assert done.code is RejectCode.RELEASE_FINISHED


def test_low_version_rejected_after_allocation(env):
    svc, clock = env
    svc.create_release("r1", "fw1", "g1", [6], failure_threshold=1.0)
    svc.advance_batch("r1", "b0")  # 5 台 g1 设备（含 d-low）
    r = svc.report_upgrade("d-low", "r1", Stage.DOWNLOAD, True)
    assert r.code is RejectCode.VERSION_BELOW_MINIMUM
    assert "0.9.0" in r.message and "1.0.0" in r.message


def test_terminal_report_is_idempotent(env):
    svc, clock = env
    svc.create_release("r1", "fw1", "g1", [6], failure_threshold=1.0)
    assert "g1-0" in svc.advance_batch("r1", "b0").device_ids
    dev = "g1-0"
    svc.report_upgrade(dev, "r1", Stage.DOWNLOAD, False)

    again = svc.report_upgrade(dev, "r1", Stage.VERIFY, True)
    assert again.ok  # 幂等回放，不再改变状态
    assert again.assignment_state is AssignmentState.FAILED
    assert again.failed_stage is Stage.DOWNLOAD
    assert svc.get_device_view(dev).assignment_state is AssignmentState.FAILED


def test_rollback_state_consistency_between_view_and_release(env):
    svc, clock = env
    svc.create_release("r1", "fw1", "g1", [6], failure_threshold=1.0)
    allocated = svc.advance_batch("r1", "b0").device_ids
    devs = ["g1-0", "g1-1", "g1-2"]
    assert all(d in allocated for d in devs)
    # 一台成功，一台停在 INSTALL
    _succeed(svc, devs[0], "r1")
    svc.report_upgrade(devs[1], "r1", Stage.DOWNLOAD, True)
    svc.report_upgrade(devs[1], "r1", Stage.VERIFY, True)

    # 回滚中：设备仍归属于 r1，但新上报被拒
    assert svc.rollback_release("r1").state == ReleaseState.ROLLING_BACK
    for d in devs:
        v = svc.get_device_view(d)
        assert v.active_release_id == "r1"
    rep = svc.report_upgrade(devs[2], "r1", Stage.DOWNLOAD, True)
    assert rep.code is RejectCode.RELEASE_ROLLING_BACK

    # 完成回滚：发布终态固定，设备释放归属；重复 settle 结论一致
    s1 = svc.settle_rollback("r1")
    s2 = svc.settle_rollback("r1")
    assert s1.state == s2.state == ReleaseState.ROLLED_BACK
    release = svc.store.get_release("r1")
    assert release.state is ReleaseState.ROLLED_BACK
    for d in devs:
        v1 = svc.get_device_view(d)
        v2 = svc.get_device_view(d)
        assert v1.active_release_id is None
        assert v2.active_release_id is None
    # 已成功设备版本保留；未完成设备版本不变
    assert svc.get_device_view(devs[0]).firmware_version == "2.0.0"
    assert svc.get_device_view(devs[1]).firmware_version == "1.0.0"

    # 回滚后设备可加入新的发布（占用已释放）
    svc.create_release("r2", "fw1", "g1", [6], failure_threshold=1.0)
    rb = svc.advance_batch("r2", "b0")
    assert set(devs).issubset(set(rb.device_ids))
    log.info(
        "rollback consistency verified: r1=%s, r2 batch=%s",
        release.state.value, rb.device_ids,
    )


def test_rollback_pending_release_terminal_without_assignments(env):
    svc, clock = env
    svc.create_release("r1", "fw1", "g1", [2, 4], failure_threshold=1.0)
    r = svc.rollback_release("r1")  # 从未推进
    assert r.state == ReleaseState.ROLLED_BACK
    assert svc.advance_batch("r1", "x").code is RejectCode.RELEASE_NOT_RUNNING


def test_concurrent_reports_failures_trigger_threshold_once(env):
    svc, clock = env
    # 第一批 5 台（含 d-low），第二批 1 台；阈值 0.5
    svc.create_release("r1", "fw1", "g1", [5, 1], failure_threshold=0.5)
    allocated = svc.advance_batch("r1", "b0").device_ids
    devs = ["g1-0", "g1-1", "g1-2", "g1-3"]
    assert all(d in allocated for d in devs)

    # 4 台并发上报：3 失败 1 成功（终态）
    plan = {devs[0]: False, devs[1]: False, devs[2]: False, devs[3]: True}
    barrier = threading.Barrier(4)

    # 4 台并发：3 台 DOWNLOAD 直接失败；1 台完成四阶段成功（均出终态）
    def worker(dev: str, ok: bool) -> None:
        barrier.wait()
        if not ok:
            svc.report_upgrade(dev, "r1", Stage.DOWNLOAD, False)
        else:
            for stage in (
                Stage.DOWNLOAD, Stage.VERIFY, Stage.INSTALL, Stage.REBOOT
            ):
                svc.report_upgrade(dev, "r1", stage, True)

    threads = [
        threading.Thread(target=worker, args=(d, ok_))
        for d, ok_ in plan.items()
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 下一次推进必然自动暂停且只发生一次，原因固定为失败阈值
    r = svc.advance_batch("r1", "b1")
    assert r.auto_paused and r.pause_reason == "FAILURE_THRESHOLD"
    release = svc.store.get_release("r1")
    assert release.state is ReleaseState.PAUSED
    assert release.pause_reason.value == "FAILURE_THRESHOLD"
    assert "3/4" in release.pause_detail
    assert len(release.allocations) == 1  # 第二批绝未分配
    log.info("concurrent reports => auto pause basis=%s", release.pause_detail)


def test_rollback_after_rollout_complete_is_possible(env):
    """批次已发完（ROLLOUT_COMPLETE）但设备仍在途时，仍可回滚。"""
    svc, clock = env
    svc.create_release("r1", "fw1", "g1", [6], failure_threshold=1.0)
    allocated = svc.advance_batch("r1", "b0").device_ids
    release = svc.store.get_release("r1")
    assert release.state == ReleaseState.ROLLOUT_COMPLETE

    # 一台成功，其余在途；仍允许回滚
    _succeed(svc, "g1-0", "r1")
    r = svc.rollback_release("r1")
    assert r.state == ReleaseState.ROLLING_BACK
    # 在途设备上报被拒
    rep = svc.report_upgrade("g1-1", "r1", Stage.DOWNLOAD, True)
    assert rep.code is RejectCode.RELEASE_ROLLING_BACK
    assert svc.settle_rollback("r1").state == ReleaseState.ROLLED_BACK


def test_paused_view_and_release_record_stay_consistent(env):
    """暂停后设备视图与发布记录反映同一结论，重复查询一致。"""
    svc, clock = env
    svc.create_release("r1", "fw1", "g1", [6], failure_threshold=1.0)
    svc.advance_batch("r1", "b0")
    svc.report_upgrade("g1-0", "r1", Stage.DOWNLOAD, True)
    svc.pause_release("r1")

    release = svc.store.get_release("r1")
    for dev_id in ("g1-0", "g1-1"):
        v1 = svc.get_device_view(dev_id)
        v2 = svc.get_device_view(dev_id)
        assert v1.active_release_id == v2.active_release_id == "r1"
        assert v1.assignment_state == v2.assignment_state
    v = svc.get_device_view("g1-0")
    assert v.assignment_stage is Stage.DOWNLOAD
    assert release.state is ReleaseState.PAUSED
    assert release.pause_reason.value == "MANUAL"
    # 视图中的归属与存储占用一致
    assert svc.store.owner_release("g1-0") == "r1"

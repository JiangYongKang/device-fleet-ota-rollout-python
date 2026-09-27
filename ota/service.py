"""OTA 领域服务：在线判定、灰度发布、回滚、升级结果上报。

所有“检查 + 写入”都在 store 的同一把锁内完成，保证并发原子性；
拒绝一律返回带 RejectCode 的结果对象，调用方可以精确区分原因。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .clock import Clock
from .errors import OtaError, RejectCode
from .models import (
    Assignment,
    AssignmentState,
    BatchAllocation,
    Device,
    Firmware,
    PauseReason,
    Release,
    ReleaseEvent,
    ReleaseState,
    Stage,
    STAGE_ORDER,
)
from .storage import InMemoryStore

logger = logging.getLogger("ota.service")


@dataclass
class HeartbeatResult:
    accepted: bool
    device_id: str
    last_heartbeat_at: float | None
    online: bool
    code: RejectCode | None = None
    message: str = ""


@dataclass
class DeviceView:
    device_id: str
    group: str
    firmware_version: str
    online: bool
    last_heartbeat_at: float | None
    active_release_id: str | None
    assignment_state: AssignmentState | None
    assignment_stage: Stage | None


@dataclass
class AdvanceResult:
    ok: bool
    release_id: str
    state: ReleaseState
    batch_index: int | None = None
    device_ids: list[str] = field(default_factory=list)
    auto_paused: bool = False
    pause_reason: str | None = None
    code: RejectCode | None = None
    message: str = ""


@dataclass
class StateChangeResult:
    ok: bool
    release_id: str
    state: ReleaseState
    pause_reason: str | None = None
    code: RejectCode | None = None
    message: str = ""


@dataclass
class ReportResult:
    ok: bool
    device_id: str
    release_id: str
    assignment_state: AssignmentState
    stage: Stage | None
    failed_stage: Stage | None = None
    code: RejectCode | None = None
    message: str = ""


class OtaService:
    def __init__(
        self,
        store: InMemoryStore,
        clock: Clock,
        heartbeat_timeout_seconds: float = 30.0,
    ) -> None:
        self.store = store
        self.clock = clock
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds

    # ================================================================
    # 设备注册 / 心跳 / 视图
    # ================================================================
    def register_device(
        self, device_id: str, group: str, firmware_version: str
    ) -> Device:
        now = self.clock.now()
        with self.store.lock:
            existing = self.store.get_device(device_id)
            if existing is not None:
                # 重复注册：返回已存在记录，不改变心跳与版本
                return existing
            device = Device(
                device_id=device_id,
                group=group,
                firmware_version=firmware_version,
                registered_at=now,
            )
            self.store.put_device(device)
            logger.info(
                "register_device input={device_id=%r group=%r version=%r} "
                "=> created at %s",
                device_id, group, firmware_version, now,
            )
            return device

    def heartbeat(self, device_id: str, ts: float | None = None) -> HeartbeatResult:
        """心跳上报。

        - ts 为 None 时取注入时钟当前时间；
        - 乱序/旧心跳（ts < 已记录时间）被拒绝（STALE_HEARTBEAT），
          绝不用更旧的数据覆盖过期时间；
        - 同一把锁内完成比较与写入，重复/并发上报幂等且安全；
        - 在线判定严格使用 now - last < timeout，临界相等算离线。
        """
        now = self.clock.now() if ts is None else ts
        with self.store.lock:
            device = self.store.get_device(device_id)
            if device is None:
                return HeartbeatResult(
                    accepted=False,
                    device_id=device_id,
                    last_heartbeat_at=None,
                    online=False,
                    code=RejectCode.DEVICE_NOT_FOUND,
                    message=f"device {device_id!r} not registered",
                )

            last = device.last_heartbeat_at
            if last is not None and ts is not None and ts < last:
                online = device.is_online(self.clock.now(), self.heartbeat_timeout_seconds)
                logger.info(
                    "heartbeat REJECT input={device_id=%r ts=%s} basis={last=%s "
                    "timeout=%ss now=%s online=%s} => STALE_HEARTBEAT",
                    device_id, ts, last, self.heartbeat_timeout_seconds,
                    self.clock.now(), online,
                )
                return HeartbeatResult(
                    accepted=False,
                    device_id=device_id,
                    last_heartbeat_at=last,
                    online=online,
                    code=RejectCode.STALE_HEARTBEAT,
                    message=(
                        f"heartbeat ts {ts} is older than recorded {last}; "
                        "expiry must not be overwritten by stale data"
                    ),
                )

            device.last_heartbeat_at = now
            online = device.is_online(self.clock.now(), self.heartbeat_timeout_seconds)
            logger.info(
                "heartbeat accept input={device_id=%r ts=%s} basis={last->%s "
                "timeout=%ss now=%s online=%s}",
                device_id, now, now, self.heartbeat_timeout_seconds,
                self.clock.now(), online,
            )
            return HeartbeatResult(
                accepted=True,
                device_id=device_id,
                last_heartbeat_at=now,
                online=online,
            )

    def get_device_view(self, device_id: str) -> DeviceView:
        """设备视图：在线状态 + 当前版本 + 当前归属发布与升级状态。

        视图完全由当前存储状态与当前时钟推导，重复查询结论一致。
        """
        now = self.clock.now()
        with self.store.lock:
            device = self.store.get_device(device_id)
            if device is None:
                raise OtaError(
                    RejectCode.DEVICE_NOT_FOUND,
                    f"device {device_id!r} not registered",
                )
            online = device.is_online(now, self.heartbeat_timeout_seconds)
            owner_id = self.store.owner_release(device_id)
            assignment_state: AssignmentState | None = None
            assignment_stage: Stage | None = None
            if owner_id is not None:
                release = self.store.get_release(owner_id)
                if release is not None:
                    assignment = release.assignments.get(device_id)
                    if assignment is not None:
                        assignment_state = assignment.state
                        assignment_stage = assignment.stage
            return DeviceView(
                device_id=device_id,
                group=device.group,
                firmware_version=device.firmware_version,
                online=online,
                last_heartbeat_at=device.last_heartbeat_at,
                active_release_id=owner_id,
                assignment_state=assignment_state,
                assignment_stage=assignment_stage,
            )

    # ================================================================
    # 固件
    # ================================================================
    def create_firmware(
        self,
        firmware_id: str,
        version: str,
        min_device_version: str,
        description: str = "",
    ) -> Firmware:
        now = self.clock.now()
        with self.store.lock:
            if self.store.get_firmware(firmware_id) is not None:
                raise OtaError(
                    RejectCode.FIRMWARE_ALREADY_EXISTS,
                    f"firmware {firmware_id!r} already exists",
                )
            if self.store.firmware_exists_for_version(version):
                raise OtaError(
                    RejectCode.FIRMWARE_VERSION_CONFLICT,
                    f"firmware version {version!r} already exists",
                )
            firmware = Firmware(
                firmware_id=firmware_id,
                version=version,
                min_device_version=min_device_version,
                created_at=now,
                description=description,
            )
            self.store.put_firmware(firmware)
            logger.info(
                "create_firmware input={firmware_id=%r version=%r min=%r} "
                "=> created",
                firmware_id, version, min_device_version,
            )
            return firmware

    # ================================================================
    # 发布
    # ================================================================
    def create_release(
        self,
        release_id: str,
        firmware_id: str,
        target_group: str | None,
        batch_sizes: list[int],
        failure_threshold: float,
    ) -> Release:
        """创建灰度发布。

        - target_group 为 None 表示全量分组；
        - batch_sizes 在创建时快照候选设备名单（后续注册不影响本次发布）；
        - failure_threshold 取值 [0,1]，表示“已出终态设备”中的失败比例。
        """
        now = self.clock.now()
        if not batch_sizes or any(b <= 0 for b in batch_sizes):
            raise OtaError(
                RejectCode.BATCH_PLAN_EMPTY,
                "batch_sizes must be a non-empty list of positive integers",
            )
        if not 0.0 <= failure_threshold <= 1.0:
            raise ValueError("failure_threshold must be within [0, 1]")

        with self.store.lock:
            if self.store.get_release(release_id) is not None:
                raise OtaError(
                    RejectCode.RELEASE_ALREADY_EXISTS,
                    f"release {release_id!r} already exists",
                )
            firmware = self.store.get_firmware(firmware_id)
            if firmware is None:
                raise OtaError(
                    RejectCode.FIRMWARE_NOT_FOUND,
                    f"firmware {firmware_id!r} not found",
                )

            candidate_ids = sorted(
                d.device_id
                for d in self.store.devices.values()
                if target_group is None or d.group == target_group
            )
            release = Release(
                release_id=release_id,
                firmware_id=firmware_id,
                target_group=target_group,
                batch_sizes=list(batch_sizes),
                failure_threshold=failure_threshold,
                created_at=now,
                candidate_ids=candidate_ids,
                updated_at=now,
            )
            release.events.append(
                ReleaseEvent(
                    at=now,
                    kind="CREATED",
                    detail=(
                        f"group={target_group} batches={batch_sizes} "
                        f"threshold={failure_threshold} candidates={len(candidate_ids)}"
                    ),
                )
            )
            self.store.put_release(release)
            logger.info(
                "create_release input={release_id=%r firmware=%r group=%r "
                "batches=%s threshold=%s} => candidates=%s",
                release_id, firmware_id, target_group, batch_sizes,
                failure_threshold, candidate_ids,
            )
            return release

    def advance_batch(self, release_id: str, request_id: str) -> AdvanceResult:
        """推进下一个灰度批次。

        幂等/并发语义（全部在同一把锁内原子完成）：
        - 同一个 request_id 重复或并发调用 => 返回同一批次结果，绝不重复分配；
        - 不同 request_id 并发调用 => 严格串行，各推进一个批次，不跳过批次；
        - 已暂停（手动或失败阈值）/回滚中/终态 => 拒绝并给出可区分原因；
        - 推进前先检查失败比例，达到阈值则自动暂停，不再分配新设备。
        """
        now = self.clock.now()
        with self.store.lock:
            release = self.store.get_release(release_id)
            if release is None:
                return AdvanceResult(
                    ok=False, release_id=release_id, state=ReleaseState.PENDING,
                    code=RejectCode.RELEASE_NOT_FOUND,
                    message=f"release {release_id!r} not found",
                )

            # 1) 幂等：同一 request_id 直接回放已记录的批次
            if request_id in release.advance_requests:
                idx = release.advance_requests[request_id]
                allocation = release.allocations[idx]
                logger.info(
                    "advance idempotent input={release=%s request_id=%s} "
                    "basis=recorded batch_index=%s devices=%s state=%s",
                    release_id, request_id, idx, allocation.device_ids,
                    release.state.value,
                )
                return AdvanceResult(
                    ok=True,
                    release_id=release_id,
                    state=release.state,
                    batch_index=idx,
                    device_ids=list(allocation.device_ids),
                )

            # 2) 状态门禁
            if release.state == ReleaseState.PAUSED:
                reason = release.pause_reason.value if release.pause_reason else "UNKNOWN"
                logger.info(
                    "advance REJECT input={release=%s} basis=PAUSED reason=%s detail=%s",
                    release_id, reason, release.pause_detail,
                )
                return AdvanceResult(
                    ok=False, release_id=release_id, state=release.state,
                    code=RejectCode.RELEASE_NOT_RUNNING,
                    message=f"release is paused ({reason}): {release.pause_detail}",
                )
            if release.state in (
                ReleaseState.ROLLING_BACK,
                ReleaseState.ROLLED_BACK,
                ReleaseState.ROLLOUT_COMPLETE,
                ReleaseState.COMPLETED,
            ):
                logger.info(
                    "advance REJECT input={release=%s} basis=state=%s",
                    release_id, release.state.value,
                )
                return AdvanceResult(
                    ok=False, release_id=release_id, state=release.state,
                    code=RejectCode.RELEASE_NOT_RUNNING,
                    message=f"release is {release.state.value}",
                )
            # state 为 PENDING 或 IN_PROGRESS 才允许推进

            # 3) 失败阈值自动停（基于“自上次放行以来新增终态”的增量统计）
            succeeded, failed, terminal = release.terminal_stats()
            new_s = succeeded - release.checkpoint_succeeded
            new_f = failed - release.checkpoint_failed
            new_terminal = new_s + new_f
            if (
                new_terminal > 0
                and new_f / new_terminal >= release.failure_threshold
            ):
                release.state = ReleaseState.PAUSED
                release.pause_reason = PauseReason.FAILURE_THRESHOLD
                detail = (
                    f"failure ratio since last checkpoint "
                    f"{new_f}/{new_terminal}={new_f / new_terminal:.3f} "
                    f"reached threshold {release.failure_threshold:.3f} "
                    f"(cumulative {failed}/{terminal})"
                )
                release.pause_detail = detail
                release.updated_at = now
                release.events.append(
                    ReleaseEvent(at=now, kind="AUTO_PAUSED", detail=detail)
                )
                logger.info(
                    "advance auto-pause input={release=%s} basis=%s",
                    release_id, detail,
                )
                return AdvanceResult(
                    ok=False, release_id=release_id, state=release.state,
                    auto_paused=True,
                    pause_reason=PauseReason.FAILURE_THRESHOLD.value,
                    code=RejectCode.RELEASE_NOT_RUNNING,
                    message=detail,
                )

            # 4) 先登记 request_id（保证并发重入直接幂等），再挑选设备。
            #    以“本批次放行之前”的累计终态数作为阈值检查点：
            #    此后这些已分配设备新产生的终态计入下一次判定。
            baseline_succeeded, baseline_failed = succeeded, failed
            next_idx = release.next_batch_index()
            wanted = release.batch_sizes[next_idx]
            release.advance_requests[request_id] = next_idx

            chosen: list[str] = []
            skipped_busy: list[str] = []
            for candidate in release.candidate_ids:
                if len(chosen) >= wanted:
                    break
                if candidate in release.assignments:
                    continue  # 本发布已分配过，绝不重复
                if not self.store.lock_device(candidate, release_id):
                    skipped_busy.append(candidate)  # 被其他进行中发布占用
                    continue
                chosen.append(candidate)

            now2 = self.clock.now()
            for device_id in chosen:
                device = self.store.get_device(device_id)
                assignment = Assignment(
                    device_id=device_id,
                    release_id=release_id,
                    batch_index=next_idx,
                    assigned_at=now2,
                    version_at_assignment=device.firmware_version,
                    updated_at=now2,
                )
                self.store.add_assignment(release, assignment)

            allocation = BatchAllocation(
                batch_index=next_idx,
                device_ids=chosen,
                allocated_at=now2,
            )
            self.store.add_allocation(release, allocation)
            # 放行一个批次 => 以放行前累计值为新检查点
            release.checkpoint_succeeded = baseline_succeeded
            release.checkpoint_failed = baseline_failed
            # 最后一个批次已发完 => ROLLOUT_COMPLETE（仍接受设备上报）；
            # 否则保持 IN_PROGRESS
            if release.next_batch_index() >= len(release.batch_sizes):
                release.state = ReleaseState.ROLLOUT_COMPLETE
                self._append_event(
                    release,
                    "ROLLOUT_COMPLETE",
                    f"all {len(release.batch_sizes)} batches allocated; "
                    f"{len(chosen)} devices in this batch",
                )
            else:
                release.state = ReleaseState.IN_PROGRESS
                release.updated_at = now2
            self._maybe_complete(release)
            release.events.append(
                ReleaseEvent(
                    at=now2,
                    kind="BATCH_ALLOCATED",
                    detail=(
                        f"batch_index={next_idx} requested={wanted} "
                        f"allocated={len(chosen)} busy_skipped={skipped_busy}"
                    ),
                )
            )
            logger.info(
                "advance allocated input={release=%s request_id=%s batch=%s} "
                "basis={wanted=%s chosen=%s busy_skipped=%s}",
                release_id, request_id, next_idx, wanted, chosen, skipped_busy,
            )
            return AdvanceResult(
                ok=True,
                release_id=release_id,
                state=release.state,
                batch_index=next_idx,
                device_ids=chosen,
            )

    def _append_event(self, release: Release, kind: str, detail: str) -> None:
        release.updated_at = self.clock.now()
        release.events.append(
            ReleaseEvent(at=release.updated_at, kind=kind, detail=detail)
        )

    def _maybe_complete(self, release: Release) -> bool:
        """ROLLOUT_COMPLETE 后全部设备出终态 => 收敛到 COMPLETED。"""
        if release.state != ReleaseState.ROLLOUT_COMPLETE:
            return False
        if any(
            a.state in (AssignmentState.PENDING, AssignmentState.IN_PROGRESS)
            for a in release.assignments.values()
        ):
            return False
        release.state = ReleaseState.COMPLETED
        for device_id in release.assignments:
            self.store.unlock_device(device_id, release.release_id)
        self._append_event(
            release,
            "COMPLETED",
            f"all {len(release.assignments)} assignments reached terminal state",
        )
        logger.info(
            "auto-complete release=%s after last terminal report",
            release.release_id,
        )
        return True

    def pause_release(self, release_id: str) -> StateChangeResult:
        """手动暂停。仅 IN_PROGRESS/PENDING 可暂停；重复暂停视为幂等成功。"""
        with self.store.lock:
            release = self.store.get_release(release_id)
            if release is None:
                return StateChangeResult(
                    ok=False, release_id=release_id, state=ReleaseState.PENDING,
                    code=RejectCode.RELEASE_NOT_FOUND,
                    message=f"release {release_id!r} not found",
                )
            if release.state == ReleaseState.PAUSED:
                return StateChangeResult(
                    ok=True, release_id=release_id, state=release.state,
                    pause_reason=release.pause_reason.value
                    if release.pause_reason else None,
                    message="already paused",
                )
            if release.state not in (
                ReleaseState.IN_PROGRESS,
                ReleaseState.PENDING,
                ReleaseState.ROLLOUT_COMPLETE,
            ):
                return StateChangeResult(
                    ok=False, release_id=release_id, state=release.state,
                    code=RejectCode.RELEASE_NOT_RUNNING,
                    message=f"cannot pause release in state {release.state.value}",
                )
            release.state = ReleaseState.PAUSED
            release.pause_reason = PauseReason.MANUAL
            release.pause_detail = "manually paused"
            self._append_event(release, "PAUSED", "manually paused")
            logger.info("pause release=%s => PAUSED(MANUAL)", release_id)
            return StateChangeResult(
                ok=True, release_id=release_id, state=release.state,
                pause_reason=PauseReason.MANUAL.value,
            )

    def resume_release(self, release_id: str) -> StateChangeResult:
        """继续：仅手动暂停的发布可继续；失败阈值自动暂停不允许直接继续。

        自动暂停后若要继续，必须显式“重新放行”（rearm_release），
        避免设备继续失败时被悄悄放开。
        """
        with self.store.lock:
            release = self.store.get_release(release_id)
            if release is None:
                return StateChangeResult(
                    ok=False, release_id=release_id, state=ReleaseState.PENDING,
                    code=RejectCode.RELEASE_NOT_FOUND,
                    message=f"release {release_id!r} not found",
                )
            if release.state != ReleaseState.PAUSED:
                return StateChangeResult(
                    ok=False, release_id=release_id, state=release.state,
                    code=RejectCode.RELEASE_NOT_PAUSED,
                    message=f"release is {release.state.value}, not paused",
                )
            if release.pause_reason is PauseReason.FAILURE_THRESHOLD:
                return StateChangeResult(
                    ok=False, release_id=release_id, state=release.state,
                    pause_reason=PauseReason.FAILURE_THRESHOLD.value,
                    code=RejectCode.RELEASE_NOT_RUNNING,
                    message=(
                        "paused by FAILURE_THRESHOLD; call rearm_release() "
                        "explicitly before resume"
                    ),
                )
            release.state = ReleaseState.IN_PROGRESS
            release.pause_reason = None
            release.pause_detail = None
            self._append_event(release, "RESUMED", "resumed after manual pause")
            logger.info("resume release=%s => IN_PROGRESS", release_id)
            return StateChangeResult(
                ok=True, release_id=release_id, state=release.state,
            )

    def rearm_release(self, release_id: str) -> StateChangeResult:
        """显式清除失败阈值自动暂停标记并继续（需要人工确认的动作）。"""
        with self.store.lock:
            release = self.store.get_release(release_id)
            if release is None:
                return StateChangeResult(
                    ok=False, release_id=release_id, state=ReleaseState.PENDING,
                    code=RejectCode.RELEASE_NOT_FOUND,
                    message=f"release {release_id!r} not found",
                )
            if release.state != ReleaseState.PAUSED:
                return StateChangeResult(
                    ok=False, release_id=release_id, state=release.state,
                    code=RejectCode.RELEASE_NOT_PAUSED,
                    message=f"release is {release.state.value}, not paused",
                )
            detail = release.pause_detail
            succeeded, failed, _ = release.terminal_stats()
            release.checkpoint_succeeded = succeeded
            release.checkpoint_failed = failed
            release.state = ReleaseState.IN_PROGRESS
            release.pause_reason = None
            release.pause_detail = None
            self._append_event(
                release, "REARMED", f"manual rearm after auto-pause: {detail}"
            )
            logger.info("rearm release=%s => IN_PROGRESS (was %s)", release_id, detail)
            return StateChangeResult(
                ok=True, release_id=release_id, state=release.state,
            )

    def rollback_release(self, release_id: str) -> StateChangeResult:
        """进入回滚中：拒绝新的升级结果上报（RELEASE_ROLLING_BACK）。

        - PENDING（未分配任何设备）直接终态 ROLLED_BACK 并释放占用；
        - IN_PROGRESS / PAUSED => ROLLING_BACK，设备占用保持，
          等待 settle_rollback 完成回滚；
        - 重复调用幂等。
        """
        now = self.clock.now()
        with self.store.lock:
            release = self.store.get_release(release_id)
            if release is None:
                return StateChangeResult(
                    ok=False, release_id=release_id, state=ReleaseState.PENDING,
                    code=RejectCode.RELEASE_NOT_FOUND,
                    message=f"release {release_id!r} not found",
                )
            if release.state in (
                ReleaseState.ROLLING_BACK,
                ReleaseState.ROLLED_BACK,
            ):
                return StateChangeResult(
                    ok=True, release_id=release_id, state=release.state,
                    message="rollback already requested",
                )
            if release.state == ReleaseState.COMPLETED:
                return StateChangeResult(
                    ok=False, release_id=release_id, state=release.state,
                    code=RejectCode.RELEASE_NOT_RUNNING,
                    message="completed release cannot be rolled back",
                )

            if not release.assignments:
                release.state = ReleaseState.ROLLED_BACK
                self._append_event(
                    release,
                    "ROLLED_BACK",
                    "no devices allocated; rolled back immediately",
                )
                logger.info("rollback release=%s => ROLLED_BACK (no assignments)", release_id)
                return StateChangeResult(
                    ok=True, release_id=release_id, state=release.state,
                )

            release.state = ReleaseState.ROLLING_BACK
            release.pause_reason = None
            release.pause_detail = None
            self._append_event(
                release,
                "ROLLBACK_STARTED",
                f"{len(release.assignments)} devices under rollback",
            )
            logger.info("rollback release=%s => ROLLING_BACK", release_id)
            return StateChangeResult(
                ok=True, release_id=release_id, state=release.state,
            )

    def settle_rollback(self, release_id: str) -> StateChangeResult:
        """完成回滚：置终态 ROLLED_BACK 并释放全部设备占用。

        幂等：重复调用返回相同终态；之后设备视图 active_release_id 为 None，
        发布记录终态固定为 ROLLED_BACK，重复查询结论一致。
        """
        with self.store.lock:
            release = self.store.get_release(release_id)
            if release is None:
                return StateChangeResult(
                    ok=False, release_id=release_id, state=ReleaseState.PENDING,
                    code=RejectCode.RELEASE_NOT_FOUND,
                    message=f"release {release_id!r} not found",
                )
            if release.state == ReleaseState.ROLLED_BACK:
                return StateChangeResult(
                    ok=True, release_id=release_id, state=release.state,
                    message="already rolled back",
                )
            if release.state != ReleaseState.ROLLING_BACK:
                return StateChangeResult(
                    ok=False, release_id=release_id, state=release.state,
                    code=RejectCode.RELEASE_NOT_RUNNING,
                    message=(
                        f"release is {release.state.value}; "
                        "call rollback_release() first"
                    ),
                )

            device_ids = list(release.assignments)
            for device_id in device_ids:
                self.store.unlock_device(device_id, release_id)
            release.state = ReleaseState.ROLLED_BACK
            self._append_event(
                release,
                "ROLLED_BACK",
                f"rollback settled; {len(device_ids)} devices released",
            )
            logger.info(
                "settle_rollback release=%s => ROLLED_BACK released=%s",
                release_id, device_ids,
            )
            return StateChangeResult(
                ok=True, release_id=release_id, state=release.state,
            )

    # ================================================================
    # 升级结果上报
    # ================================================================
    @staticmethod
    def _version_key(version: str) -> tuple[int, ...]:
        """把 '1.2.10' 解析为可比较的整数元组 (1, 2, 10)。"""
        parts = []
        for p in version.split("."):
            num = "".join(ch for ch in p if ch.isdigit())
            parts.append(int(num) if num else 0)
        return tuple(parts) or (0,)

    def _reject_report(
        self,
        device_id: str,
        release_id: str,
        code: RejectCode,
        message: str,
        state: AssignmentState,
        stage: Stage | None,
    ) -> ReportResult:
        logger.info(
            "report REJECT input={device=%s release=%s} basis=%s: %s",
            device_id, release_id, code.value, message,
        )
        return ReportResult(
            ok=False,
            device_id=device_id,
            release_id=release_id,
            assignment_state=state,
            stage=stage,
            code=code,
            message=message,
        )

    def report_upgrade(
        self,
        device_id: str,
        release_id: str,
        stage: Stage,
        success: bool,
        message: str = "",
    ) -> ReportResult:
        """设备上报某升级阶段的结果。

        拒绝原因严格区分（按以下顺序判定）：
        1. RELEASE_NOT_FOUND / DEVICE_NOT_FOUND
        2. RELEASE_ROLLING_BACK（回滚中）/ RELEASE_PAUSED（暂停中）/
           RELEASE_FINISHED（已完成或已回滚终态）
        3. ALREADY_TERMINAL：该设备在本次发布中已有终态，重复上报幂等回放
        4. NOT_IN_TARGET_GROUP：设备不在发布创建时快照的目标名单内
        5. NO_ACTIVE_ASSIGNMENT：在名单内但尚未被分配到任何批次
        6. VERSION_BELOW_MINIMUM：当前版本低于固件要求的最低设备版本
        7. STAGE_OUT_OF_ORDER：阶段必须 DOWNLOAD→VERIFY→INSTALL→REBOOT

        成功完成 REBOOT => SUCCEEDED 终态并把设备当前版本更新为目标固件；
        任一阶段失败 => FAILED 终态并记录 failed_stage。
        """
        now = self.clock.now()
        with self.store.lock:
            release = self.store.get_release(release_id)
            if release is None:
                return self._reject_report(
                    device_id, release_id, RejectCode.RELEASE_NOT_FOUND,
                    f"release {release_id!r} not found",
                    AssignmentState.PENDING, None,
                )
            device = self.store.get_device(device_id)
            if device is None:
                return self._reject_report(
                    device_id, release_id, RejectCode.DEVICE_NOT_FOUND,
                    f"device {device_id!r} not registered",
                    AssignmentState.PENDING, None,
                )

            # 发布状态门禁（回滚中与回滚完成是两种不同原因）
            if release.state == ReleaseState.ROLLING_BACK:
                return self._reject_report(
                    device_id, release_id, RejectCode.RELEASE_ROLLING_BACK,
                    "release is rolling back; upgrade reports are rejected",
                    AssignmentState.PENDING, None,
                )
            if release.state == ReleaseState.PAUSED:
                return self._reject_report(
                    device_id, release_id, RejectCode.RELEASE_PAUSED,
                    f"release is paused "
                    f"({release.pause_reason.value if release.pause_reason else 'UNKNOWN'}); "
                    "reports are rejected until resumed",
                    AssignmentState.PENDING, None,
                )
            if release.state in (ReleaseState.ROLLED_BACK, ReleaseState.COMPLETED):
                return self._reject_report(
                    device_id, release_id, RejectCode.RELEASE_FINISHED,
                    f"release is in terminal state {release.state.value}",
                    AssignmentState.PENDING, None,
                )

            assignment = release.assignments.get(device_id)

            # 终态幂等：已有终态时回放相同结论，不改变状态
            if assignment is not None and assignment.state in (
                AssignmentState.SUCCEEDED,
                AssignmentState.FAILED,
            ):
                logger.info(
                    "report idempotent input={device=%s release=%s} "
                    "basis=terminal state=%s failed_stage=%s",
                    device_id, release_id, assignment.state.value,
                    assignment.failed_stage,
                )
                return ReportResult(
                    ok=True,
                    device_id=device_id,
                    release_id=release_id,
                    assignment_state=assignment.state,
                    stage=assignment.stage,
                    failed_stage=assignment.failed_stage,
                    message="assignment already terminal; replay",
                )

            # 无分配记录：区分“不在目标分组”与“尚未被分配”
            if assignment is None:
                if device_id not in release.candidate_ids:
                    return self._reject_report(
                        device_id, release_id, RejectCode.NOT_IN_TARGET_GROUP,
                        f"device group {device.group!r} not in release target "
                        f"{release.target_group!r} (snapshot at creation)",
                        AssignmentState.PENDING, None,
                    )
                return self._reject_report(
                    device_id, release_id, RejectCode.NO_ACTIVE_ASSIGNMENT,
                    "device is a candidate but has not been allocated a batch yet",
                    AssignmentState.PENDING, None,
                )

            # 最低版本门槛
            firmware = self.store.get_firmware(release.firmware_id)
            min_key = self._version_key(firmware.min_device_version)
            cur_key = self._version_key(device.firmware_version)
            if cur_key < min_key:
                return self._reject_report(
                    device_id, release_id, RejectCode.VERSION_BELOW_MINIMUM,
                    f"device version {device.firmware_version} < required "
                    f"{firmware.min_device_version} for firmware {firmware.version}",
                    assignment.state, assignment.stage,
                )

            # 阶段顺序：DOWNLOAD -> VERIFY -> INSTALL -> REBOOT
            reported_idx = STAGE_ORDER.index(stage)
            last_idx = (
                -1 if assignment.stage is None
                else STAGE_ORDER.index(assignment.stage)
            )

            # 重放“当前阶段”：成功幂等；重报失败拒绝（不允许把成功改成失败）
            if reported_idx == last_idx:
                if success:
                    return ReportResult(
                        ok=True, device_id=device_id, release_id=release_id,
                        assignment_state=assignment.state, stage=stage,
                        message="duplicate stage report; idempotent replay",
                    )
                return self._reject_report(
                    device_id, release_id, RejectCode.STAGE_OUT_OF_ORDER,
                    f"stage {stage.value} already reported successfully",
                    assignment.state, assignment.stage,
                )

            # 必须严格推进到下一阶段（跳阶段或回退都拒绝）
            if reported_idx != last_idx + 1:
                expected = STAGE_ORDER[last_idx + 1]
                return self._reject_report(
                    device_id, release_id, RejectCode.STAGE_OUT_OF_ORDER,
                    f"stage {stage.value} out of order; expected {expected.value} "
                    f"(order: {[s.value for s in STAGE_ORDER]})",
                    assignment.state, assignment.stage,
                )

            # 应用本次上报
            assignment.stage = stage
            assignment.last_message = message
            assignment.updated_at = now
            if not success:
                assignment.state = AssignmentState.FAILED
                assignment.failed_stage = stage
                self._maybe_complete(release)
                logger.info(
                    "report accepted input={device=%s release=%s stage=%s "
                    "success=False} => FAILED at %s (release state=%s)",
                    device_id, release_id, stage.value, stage.value,
                    release.state.value,
                )
                return ReportResult(
                    ok=True, device_id=device_id, release_id=release_id,
                    assignment_state=AssignmentState.FAILED, stage=stage,
                    failed_stage=stage, message=message,
                )

            if stage == Stage.REBOOT:
                assignment.state = AssignmentState.SUCCEEDED
                device.firmware_version = firmware.version
                self._maybe_complete(release)
                logger.info(
                    "report accepted input={device=%s release=%s stage=REBOOT "
                    "success=True} => SUCCEEDED; device version -> %s "
                    "(release state=%s)",
                    device_id, release_id, firmware.version, release.state.value,
                )
            else:
                assignment.state = AssignmentState.IN_PROGRESS
                logger.info(
                    "report accepted input={device=%s release=%s stage=%s "
                    "success=True} => IN_PROGRESS",
                    device_id, release_id, stage.value,
                )
            return ReportResult(
                ok=True, device_id=device_id, release_id=release_id,
                assignment_state=assignment.state, stage=stage,
                message=message,
            )

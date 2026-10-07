"""固件发布：批次灰度推进、暂停、继续、回滚。

状态流转：
    PENDING --advance--> IN_PROGRESS --pause--> PAUSED --resume--> IN_PROGRESS
    IN_PROGRESS --(失败比例达标)--> AUTO_PAUSED --resume--> IN_PROGRESS
    IN_PROGRESS/PAUSED/AUTO_PAUSED/PENDING --rollback--> ROLLED_BACK
    IN_PROGRESS --(全批次设备终态)--> COMPLETED

推进规则（幂等）：
- 目标分组在创建时按 device_id 排序快照，再按 batch_size 切分，顺序确定可复现；
- 一次 advance 至多打开一个批次；上一批次未全部终态时推进为空操作；
- 全部批次已打开后重复/并发 advance 不再分配任何设备；
- 设备同一时刻只能属于一个进行中的发布：分配时若设备已被其它进行中发布占用，
  该设备记入 skipped_device_ids，不重复分配；
- 失败比例 = 失败数 / (成功数 + 失败数)，仅统计已上报终态设备；
  设备上报 FAIL 使比例 >= failure_threshold 时自动暂停，pause_reason 标明触发原因。
"""

from .decision import log_decision
from .errors import OtaError, RejectReason
from .models import (
    PAUSE_STATES,
    TERMINAL_RELEASE_STATES,
    TERMINAL_UPGRADE_STATUSES,
    Batch,
    BatchState,
    PauseReason,
    Release,
    ReleaseState,
    ReleaseView,
    RolloutRecord,
    UpgradeStatus,
)
from .store import Store


class ReleaseService:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ================= 发布生命周期 =================

    def create_release(
        self,
        release_id: str,
        firmware_id: str,
        target_group: str,
        batch_size: int,
        failure_threshold: float,
    ) -> ReleaseView:
        with self.store.lock:
            if self.store.get_release(release_id) is not None:
                raise OtaError(
                    RejectReason.ILLEGAL_RELEASE_STATE,
                    f"release already exists: {release_id}",
                )
            firmware = self.store.get_firmware(firmware_id)
            if firmware is None:
                raise OtaError(RejectReason.FIRMWARE_NOT_FOUND, f"firmware_id={firmware_id}")
            if batch_size < 1:
                raise OtaError(
                    RejectReason.ILLEGAL_RELEASE_STATE, f"batch_size must >=1, got {batch_size}"
                )
            if not 0.0 <= float(failure_threshold) <= 1.0:
                raise OtaError(
                    RejectReason.ILLEGAL_RELEASE_STATE,
                    f"failure_threshold must be in [0,1], got {failure_threshold}",
                )

            targets = sorted(
                d.device_id for d in self.store.list_devices() if d.group == target_group
            )
            batches: list[Batch] = []
            for i in range(0, len(targets), batch_size):
                batches.append(Batch(index=len(batches), device_ids=targets[i : i + batch_size]))

            now = self.store.clock.now()
            release = Release(
                release_id=release_id,
                firmware_id=firmware_id,
                target_group=target_group,
                batch_size=batch_size,
                failure_threshold=float(failure_threshold),
                state=ReleaseState.PENDING,
                batches=batches,
                next_batch_to_open=0,
                created_at=now,
                updated_at=now,
            )
            self.store.add_release(release, targets)
            log_decision(
                "create_release",
                {"release_id": release_id, "firmware_id": firmware_id,
                 "target_group": target_group, "batch_size": batch_size,
                 "failure_threshold": failure_threshold},
                "created_pending",
                targets=targets,
                batch_count=len(batches),
            )
            return self._view(release)

    def advance(self, release_id: str) -> ReleaseView:
        with self.store.lock:
            release = self._require_release(release_id)

            if release.state == ReleaseState.ROLLED_BACK:
                raise OtaError(
                    RejectReason.ILLEGAL_RELEASE_STATE,
                    "release already rolled back, cannot advance",
                )
            if release.state in PAUSE_STATES:
                raise OtaError(RejectReason.RELEASE_PAUSED, f"release_id={release_id}")
            if release.state == ReleaseState.COMPLETED:
                log_decision("advance", {"release_id": release_id}, "noop_completed")
                return self._view(release)

            idx = release.next_batch_to_open

            # 上一批次必须全部终态，防止跳过批次
            if idx > 0:
                prev = release.batches[idx - 1]
                if prev.state != BatchState.DONE:
                    log_decision(
                        "advance",
                        {"release_id": release_id, "attempt_batch": idx},
                        "blocked_previous_batch_inflight",
                        previous_batch=prev.index,
                    )
                    return self._view(release)

            if idx >= len(release.batches):
                self._refresh_progress(release)
                log_decision(
                    "advance",
                    {"release_id": release_id},
                    "noop_all_batches_opened",
                    state=release.state.value,
                )
                return self._view(release)

            batch = release.batches[idx]
            assigned_now: list[str] = []
            firmware = self.store.get_firmware(release.firmware_id)
            target_version = firmware.version if firmware is not None else None
            for device_id in batch.device_ids:
                occupied_by = self.store.get_active(device_id)
                if occupied_by == release_id:
                    continue  # 幂等：已属于本发布
                if occupied_by is not None:
                    occupier = self.store.get_release(occupied_by)
                    if occupier is None or occupier.state in TERMINAL_RELEASE_STATES:
                        # 占用指针指向已收尾（完成/回滚）的发布：属于遗留脏指针。
                        # 收尾时本应释放，这里防御性清除，保证设备对新一轮真正可分配。
                        self.store.clear_active(device_id)
                        occupied_by = None
                if occupied_by is not None:
                    # 已被另一条进行中发布占用：不重复分配，记录“被谁占用”以便解释。
                    # 不把该设备从批次中抹掉：占用方收尾后，后续新一轮发布会重新分它。
                    if device_id not in release.skipped_device_ids:
                        release.skipped_device_ids.append(device_id)
                    log_decision(
                        "advance_assign",
                        {"release_id": release_id, "device_id": device_id, "batch": idx},
                        "skipped_busy_in_another_release",
                        occupied_by=occupied_by,
                    )
                    continue
                device = self.store.get_device(device_id)
                record = RolloutRecord(
                    device_id=device_id,
                    release_id=release_id,
                    batch_index=idx,
                    status=UpgradeStatus.PENDING,
                    # 版本基线在“真正分配”的一刻按设备当时实际生效版本快照，
                    # 每轮独立：上一轮的结果不会串到新一轮。
                    from_version=device.firmware_version,
                    target_version=target_version,
                    assigned_at=self.store.clock.now(),
                    updated_at=self.store.clock.now(),
                )
                self.store.put_record(record)
                self.store.set_active(device_id, release_id)
                assigned_now.append(device_id)

            if assigned_now:
                # 有真实分配：批次打开，等待这些设备到达终态后再收口
                batch.state = BatchState.OPEN
            else:
                # 本批设备全部被其它进行中发布占用（或重复推进时已无待分配项）：
                # 没有设备需要等待，立即收口，保证不漏不卡后续批次
                batch.state = BatchState.DONE
            release.next_batch_to_open = idx + 1
            release.state = ReleaseState.IN_PROGRESS
            release.updated_at = self.store.clock.now()
            self._refresh_progress(release)
            log_decision(
                "advance",
                {"release_id": release_id, "opened_batch": idx},
                "batch_opened" if assigned_now else "batch_fully_skipped_done",
                assigned=assigned_now,
                batch_state=batch.state.value,
            )
            return self._view(release)

    def pause(self, release_id: str) -> ReleaseView:
        with self.store.lock:
            release = self._require_release(release_id)
            if release.state in (ReleaseState.IN_PROGRESS, ReleaseState.PENDING):
                release.state = ReleaseState.PAUSED
                release.pause_reason = PauseReason.MANUAL
                release.updated_at = self.store.clock.now()
                decision = "paused_manual"
            elif release.state == ReleaseState.AUTO_PAUSED:
                # 自动暂停原因必须保留可解释，手动 pause 不覆盖触发原因
                decision = "unchanged_auto_paused"
            elif release.state == ReleaseState.PAUSED:
                decision = "unchanged_paused"
            elif release.state == ReleaseState.COMPLETED:
                raise OtaError(RejectReason.ILLEGAL_RELEASE_STATE, "release completed")
            else:
                raise OtaError(RejectReason.ILLEGAL_RELEASE_STATE, "release rolled back")
            log_decision("pause", {"release_id": release_id}, decision,
                         state=release.state.value, pause_reason=release.pause_reason)
            return self._view(release)

    def resume(self, release_id: str) -> ReleaseView:
        with self.store.lock:
            release = self._require_release(release_id)
            if release.state in PAUSE_STATES:
                release.state = ReleaseState.IN_PROGRESS
                release.updated_at = self.store.clock.now()
                decision = "resumed"
            elif release.state in (ReleaseState.IN_PROGRESS, ReleaseState.PENDING):
                decision = "unchanged_active"
            else:
                raise OtaError(
                    RejectReason.ILLEGAL_RELEASE_STATE,
                    f"cannot resume from state {release.state.value}",
                )
            log_decision("resume", {"release_id": release_id}, decision,
                         state=release.state.value)
            return self._view(release)

    def rollback(self, release_id: str) -> ReleaseView:
        with self.store.lock:
            release = self._require_release(release_id)
            if release.state == ReleaseState.ROLLED_BACK:
                log_decision("rollback", {"release_id": release_id}, "noop_already_rolled_back")
                return self._view(release)
            if release.state == ReleaseState.COMPLETED:
                raise OtaError(RejectReason.ILLEGAL_RELEASE_STATE, "release completed")

            # 原子回滚：所有已分配设备恢复到发布前版本并解除占用，记录最终状态
            for rec in self.store.list_records(release_id):
                device = self.store.get_device(rec.device_id)
                if device is not None and rec.from_version is not None:
                    device.firmware_version = rec.from_version
                rec.status = UpgradeStatus.ROLLED_BACK
                rec.updated_at = self.store.clock.now()
            for batch in release.batches:
                if batch.state == BatchState.OPEN:
                    batch.state = BatchState.DONE
            release.state = ReleaseState.ROLLED_BACK
            release.updated_at = self.store.clock.now()
            # 回滚同样收尾：设备解除占用，可被后续新一轮发布重新分配
            self._release_devices(release)
            log_decision(
                "rollback",
                {"release_id": release_id},
                "rolled_back_devices_restored_and_released",
                affected=self._counts(release),
            )
            return self._view(release)

    # ================= 查询 =================

    def get_release(self, release_id: str) -> ReleaseView:
        with self.store.lock:
            return self._view(self._require_release(release_id))

    def list_releases(self) -> list[ReleaseView]:
        with self.store.lock:
            return [self._view(r) for r in self.store.list_releases()]

    # ================= 内部工具 =================

    def settle_progress(self, release_id: str) -> None:
        """供上报路径调用：在写操作后收口批次并判断是否完成。"""
        release = self.store.get_release(release_id)
        if release is not None:
            self._refresh_progress(release)

    def _require_release(self, release_id: str) -> Release:
        release = self.store.get_release(release_id)
        if release is None:
            raise OtaError(RejectReason.RELEASE_NOT_FOUND, f"release_id={release_id}")
        return release

    def _refresh_progress(self, release: Release) -> None:
        """把已全部终态的开放批次收口为 DONE，并判断是否完成。"""
        for batch in release.batches:
            if batch.state != BatchState.OPEN:
                continue
            assigned = [d for d in batch.device_ids
                        if self.store.get_record(release.release_id, d) is not None]
            if not assigned:
                batch.state = BatchState.DONE
                continue
            if all(
                self.store.get_record(release.release_id, d).status in TERMINAL_UPGRADE_STATUSES
                for d in assigned
            ):
                batch.state = BatchState.DONE

        # 全部批次已打开且全部 DONE => COMPLETED
        if (
            release.state in (ReleaseState.IN_PROGRESS, ReleaseState.PENDING)
            and release.next_batch_to_open >= len(release.batches)
            and all(b.state == BatchState.DONE for b in release.batches)
        ):
            release.state = ReleaseState.COMPLETED
            release.updated_at = self.store.clock.now()
            # 发布正常收尾：设备重新变为可分配状态，后续针对同一批设备的新一轮
            # 发布才能真正把它们分进去（而不是看到一个已结束发布还“占着”设备）。
            self._release_devices(release)
            log_decision(
                "completion",
                {"release_id": release.release_id},
                "all_terminal_completed_devices_released",
            )

    def _release_devices(self, release: Release) -> None:
        """解除本发布对设备的“进行中发布”占用。

        仅清除确实指向本发布的 active 指针，绝不误清设备之后参与的另一条发布。
        完成与回滚两条收尾路径共用，保证：任一收尾方式之后，设备都可给下一轮使用。
        """
        for rec in self.store.list_records(release.release_id):
            if self.store.get_active(rec.device_id) == release.release_id:
                self.store.clear_active(rec.device_id)

    def _counts(self, release: Release) -> dict[str, int]:
        succeeded = failed = rolled_back = inflight = 0
        for rec in self.store.list_records(release.release_id):
            if rec.status == UpgradeStatus.SUCCEEDED:
                succeeded += 1
            elif rec.status == UpgradeStatus.FAILED:
                failed += 1
            elif rec.status == UpgradeStatus.ROLLED_BACK:
                rolled_back += 1
            else:
                inflight += 1
        return {
            "assigned": succeeded + failed + rolled_back + inflight,
            "succeeded": succeeded,
            "failed": failed,
            "rolled_back": rolled_back,
            "inflight": inflight,
        }

    def _view(self, release: Release) -> ReleaseView:
        # 只读视图：不产生任何状态变更，保证重复/并发查询结论严格一致。
        # 批次收口与发布完成只在写操作（advance/report/rollback）内部进行。
        counts = self._counts(release)
        terminal = counts["succeeded"] + counts["failed"]
        ratio = (counts["failed"] / terminal) if terminal else 0.0
        firmware = self.store.get_firmware(release.firmware_id)
        return ReleaseView(
            release_id=release.release_id,
            firmware_id=release.firmware_id,
            target_version=firmware.version if firmware else None,
            target_group=release.target_group,
            batch_size=release.batch_size,
            failure_threshold=release.failure_threshold,
            state=release.state,
            pause_reason=release.pause_reason,
            total_targets=len(self.store.get_targets(release.release_id)),
            assigned=counts["assigned"],
            succeeded=counts["succeeded"],
            failed=counts["failed"],
            rolled_back=counts["rolled_back"],
            pending_or_inflight=counts["inflight"],
            failure_ratio=ratio,
            next_batch_to_open=release.next_batch_to_open,
            batches=[
                {
                    "index": b.index,
                    "state": b.state.value,
                    "device_ids": list(b.device_ids),
                }
                for b in release.batches
            ],
            skipped_device_ids=list(release.skipped_device_ids),
            skipped_devices=self._skipped_view(release),
        )

    def _skipped_view(self, release: Release) -> list[dict]:
        """跳过明细：每台设备当下被哪条进行中发布占着（纯只读、可复现）。

        - occupied_by 为当前真实占用该设备的进行中发布；若占用方已收尾释放，
          设备对本/后续发布重新可分配，still_blocked=False 且 occupied_by=None；
        - skipped_at_release_id 保留最初跳过时的占用方，便于解释历史来源。
        """
        out: list[dict] = []
        for device_id in release.skipped_device_ids:
            occupier_id = self.store.get_active(device_id)
            occupier = (
                self.store.get_release(occupier_id)
                if occupier_id is not None else None
            )
            still_blocked = (
                occupier is not None
                and occupier.state not in TERMINAL_RELEASE_STATES
            )
            occupier_fw = (
                self.store.get_firmware(occupier.firmware_id)
                if occupier is not None else None
            )
            out.append({
                "device_id": device_id,
                "occupied_by_release_id": occupier_id if still_blocked else None,
                "occupier_state": occupier.state.value if still_blocked else None,
                "occupier_target_version": (
                    occupier_fw.version if still_blocked and occupier_fw else None
                ),
                "still_blocked": still_blocked,
            })
        return out

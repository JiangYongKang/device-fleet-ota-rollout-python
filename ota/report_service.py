"""设备升级结果上报。

拒绝原因（彼此可区分，绝不静默接受）：
- device_not_found / release_not_found：设备或所属发布不存在；
- device_not_in_target_group：设备不属于任何进行中发布（未被分配到本次升级）；
- below_min_version：设备当前固件版本低于固件要求的最低版本；
- release_paused：发布被手动/自动暂停（暂停后上报被拒绝，保证两侧状态一致）；
- release_rolling_back：发布已回滚（回滚中的设备上报被拒绝）；
- release_finished：发布已完成；
- stale_release_report：旧一轮发布的迟到上报——设备当前已属于另一条进行中的
  发布线（或上报显式指定了非当前进行中的发布），不得作用于任何一轮的当前状态；
- invalid_stage：未知阶段；
- stale_stage：阶段顺序回退（例如已到 REBOOT 又报 VERIFY）。

轮次路由（连续多轮升级时的隔离口径）：
- 设备每次上报可显式带 release_id，指明这是“哪一轮”的上报；
  服务端严格按该 release_id 取该轮记录，绝不落到设备当前的其他轮次上；
- 未带 release_id 时按设备当前进行中的发布（active 指针）路由；
  设备没有进行中发布时，回看最近一轮记录并按其最终状态给出可区分结论；
- 旧一轮的迟到上报：重复同一终态一律幂等返回（不改动任何状态、不回写版本）；
  其余阶段按旧轮发布状态（已完成/已回滚/已暂停）或 stale_release_report 拒绝，
  因而不会污染新一轮记录，也不会把已升到新版本的设备改回旧版本。

阶段规则：DOWNLOAD < VERIFY < INSTALL < REBOOT < SUCCESS/FAIL；
同阶段重复上报幂等；SUCCESS/FAIL 为终态，之后只允许重复同一终态。
成功升级后设备当前固件版本更新为目标版本；失败记录 failure_reason。
每次 FAIL 后按 失败数/(成功数+失败数) 计算比例，达到阈值自动暂停并记录原因。
"""

from .decision import log_decision
from .device_service import DeviceService
from .errors import OtaError, RejectReason
from .models import (
    STAGE_ORDER,
    STAGE_TO_UPGRADE_STATUS,
    DeviceView,
    PauseReason,
    Release,
    ReleaseState,
    ReportStage,
    RolloutRecord,
    UpgradeStatus,
)
from .release_service import ReleaseService
from .store import Store
from .version_util import is_version_at_least


class ReportService:
    def __init__(self, store: Store) -> None:
        self.store = store

    def report_result(
        self,
        device_id: str,
        stage: ReportStage,
        failure_reason: str | None = None,
        release_id: str | None = None,
    ) -> DeviceView:
        with self.store.lock:
            # 1) 设备存在
            device = self.store.get_device(device_id)
            if device is None:
                raise OtaError(RejectReason.DEVICE_NOT_FOUND, f"device_id={device_id}")

            # 2) 阶段合法
            if not isinstance(stage, ReportStage):
                try:
                    stage = ReportStage(stage)
                except ValueError:
                    raise OtaError(RejectReason.INVALID_STAGE, f"stage={stage!r}")

            # 3) 定位“这一次上报属于哪一轮发布”及其记录
            release, record, current_line = self._locate_route(device_id, release_id)

            # 4) 重复同一终态（SUCCESS/SUCCESS 或 FAIL/FAIL）始终幂等：
            #    即使发布随后被暂停/完成/回滚、或设备已进入下一轮，重传也得到
            #    同样结论，且绝不改动任何状态、绝不回写设备固件版本。
            same_terminal = (
                (record.status == UpgradeStatus.SUCCEEDED and stage == ReportStage.SUCCESS)
                or (record.status == UpgradeStatus.FAILED and stage == ReportStage.FAIL)
            )
            if record.status in (UpgradeStatus.SUCCEEDED, UpgradeStatus.FAILED) and same_terminal:
                log_decision(
                    "report",
                    {"device_id": device_id, "release_id": release.release_id,
                     "stage": stage.value, "current_line": current_line},
                    f"idempotent_already_{record.status.value}",
                )
                return self._device_view(device_id)

            # 5) 已终态却又改报其它阶段（非同一终态重传）：
            #    - 当前进行中的轮次：阶段不允许回退/串改 => stale_stage；
            #    - 非当前轮次（迟到上报）：按下一条按轮次状态给出可区分拒绝。
            if record.status in (UpgradeStatus.SUCCEEDED, UpgradeStatus.FAILED):
                if current_line:
                    raise OtaError(
                        RejectReason.STALE_STAGE,
                        f"already {record.status.value}, cannot report {stage.value}",
                    )
                self._reject_stale_round(device_id, release, record, stage)

            # 6) 非当前轮次的迟到上报：拒绝并给出可区分原因，绝不写入任何一轮。
            if not current_line:
                self._reject_stale_round(device_id, release, record, stage)

            # 7) 当前轮次：发布状态决定可区分的拒绝原因
            if release.state == ReleaseState.ROLLED_BACK:
                raise OtaError(
                    RejectReason.RELEASE_ROLLING_BACK,
                    f"release_id={release.release_id} rolled back",
                )
            if release.state in (ReleaseState.PAUSED, ReleaseState.AUTO_PAUSED):
                raise OtaError(
                    RejectReason.RELEASE_PAUSED,
                    f"release_id={release.release_id} state={release.state.value}",
                )
            if release.state == ReleaseState.COMPLETED:
                raise OtaError(
                    RejectReason.RELEASE_FINISHED,
                    f"release_id={release.release_id} completed",
                )

            # 8) 最低版本门槛
            firmware = self.store.get_firmware(release.firmware_id)
            if firmware is None:
                raise OtaError(RejectReason.FIRMWARE_NOT_FOUND, f"firmware_id={release.firmware_id}")
            if not is_version_at_least(device.firmware_version, firmware.min_device_version):
                raise OtaError(
                    RejectReason.BELOW_MIN_VERSION,
                    f"device={device.firmware_version} minimum={firmware.min_device_version}",
                )

            # 9) 阶段单调性：不能回退
            if record.last_stage is not None and STAGE_ORDER[stage] < STAGE_ORDER[record.last_stage]:
                raise OtaError(
                    RejectReason.STALE_STAGE,
                    f"stage {stage.value} <= last {record.last_stage.value}",
                )

            # 10) 应用上报
            record.last_stage = stage
            record.reported_count += 1
            record.updated_at = self.store.clock.now()
            new_status = STAGE_TO_UPGRADE_STATUS[stage]
            record.status = new_status

            auto_paused = False
            triggered_ratio = None
            if stage == ReportStage.FAIL:
                record.failure_reason = failure_reason or "unspecified_failure"
                triggered_ratio = self._failure_ratio(release.release_id)
                if triggered_ratio is not None and triggered_ratio >= release.failure_threshold:
                    release.state = ReleaseState.AUTO_PAUSED
                    release.pause_reason = PauseReason.FAILURE_THRESHOLD
                    release.updated_at = self.store.clock.now()
                    auto_paused = True
            elif stage == ReportStage.SUCCESS:
                record.failure_reason = None
                device.firmware_version = firmware.version

            log_decision(
                "report",
                {"device_id": device_id, "release_id": release.release_id, "stage": stage.value,
                 "failure_reason": failure_reason, "from_version": record.from_version},
                "auto_paused_failure_threshold" if auto_paused else "accepted",
                upgrade_status=new_status.value,
                device_version=device.firmware_version,
                failure_ratio=triggered_ratio,
                threshold=release.failure_threshold,
            )

            # 未触发自动暂停时才收口批次/判定完成；自动暂停优先保留暂停态
            if not auto_paused:
                ReleaseService(self.store).settle_progress(release.release_id)

            return self._device_view(device)

    # ---------------- 轮次路由 ----------------

    def _locate_route(
        self, device_id: str, explicit_release_id: str | None
    ) -> tuple[Release, RolloutRecord, bool]:
        """返回 (发布, 该轮记录, 是否设备当前进行中的发布线)。

        current_line=True 表示该上报允许按正常流程写入；False 表示这是一条
        针对已结束/非当前轮次的迟到上报，只能幂等或被拒绝，绝不写状态。
        """
        active_id = self.store.get_active(device_id)

        if explicit_release_id is not None:
            release = self.store.get_release(explicit_release_id)
            if release is None:
                raise OtaError(
                    RejectReason.RELEASE_NOT_FOUND, f"release_id={explicit_release_id}"
                )
            record = self.store.get_record(explicit_release_id, device_id)
            if record is None:
                raise OtaError(
                    RejectReason.DEVICE_NOT_IN_TARGET_GROUP,
                    f"device_id={device_id} has no rollout record in {explicit_release_id}",
                )
            return release, record, active_id == explicit_release_id

        # 未显式指定轮次：优先路由到设备当前进行中的发布
        if active_id is not None:
            release = self.store.get_release(active_id)
            if release is None:
                raise OtaError(RejectReason.RELEASE_NOT_FOUND, f"release_id={active_id}")
            record = self.store.get_record(active_id, device_id)
            if record is None:
                raise OtaError(
                    RejectReason.DEVICE_NOT_IN_TARGET_GROUP,
                    f"device_id={device_id} has no rollout record in {active_id}",
                )
            return release, record, True

        # 设备当前没有进行中发布：回看最近一轮记录，按其最终状态给出结论
        latest = self.store.latest_record_for_device(device_id)
        if latest is None:
            raise OtaError(
                RejectReason.DEVICE_NOT_IN_TARGET_GROUP,
                f"device_id={device_id} not assigned to any release",
            )
        latest_release = self.store.get_release(latest.release_id)
        if latest_release is None:
            raise OtaError(RejectReason.RELEASE_NOT_FOUND, f"release_id={latest.release_id}")
        return latest_release, latest, False

    def _reject_stale_round(
        self,
        device_id: str,
        release: Release,
        record: RolloutRecord,
        stage: ReportStage,
    ) -> None:
        """旧一轮迟到上报（非同一终态幂等）的可区分拒绝，绝不改状态。"""
        if release.state == ReleaseState.ROLLED_BACK:
            reason = RejectReason.RELEASE_ROLLING_BACK
            detail = f"release_id={release.release_id} rolled back"
        elif release.state in (ReleaseState.PAUSED, ReleaseState.AUTO_PAUSED):
            reason = RejectReason.RELEASE_PAUSED
            detail = f"release_id={release.release_id} state={release.state.value}"
        elif release.state == ReleaseState.COMPLETED:
            reason = RejectReason.RELEASE_FINISHED
            detail = f"release_id={release.release_id} completed"
        else:
            # 目标轮次仍处于活动态但设备当前属于另一条进行中发布线：
            # 迟到上报不属于当前轮次，拒绝串写。
            reason = RejectReason.STALE_RELEASE_REPORT
            detail = (
                f"device_id={device_id} stale report for {release.release_id} "
                f"stage={stage.value} current={self.store.get_active(device_id)}"
            )
        log_decision(
            "report",
            {"device_id": device_id, "release_id": release.release_id, "stage": stage.value},
            f"rejected_{reason.value}",
            record_status=record.status.value,
            release_state=release.state.value,
        )
        raise OtaError(reason, detail)

    # ---------------- 内部 ----------------

    # ---------------- 内部 ----------------

    def _failure_ratio(self, release_id: str) -> float | None:
        failed = succeeded = 0
        for rec in self.store.list_records(release_id):
            if rec.status == UpgradeStatus.FAILED:
                failed += 1
            elif rec.status == UpgradeStatus.SUCCEEDED:
                succeeded += 1
        terminal = failed + succeeded
        return (failed / terminal) if terminal else None

    def _device_view(self, device):
        if isinstance(device, str):
            device = self.store.get_device(device)
        return DeviceService(self.store)._view(device)

"""设备升级结果上报。

拒绝原因（彼此可区分，绝不静默接受）：
- device_not_found / release_not_found：设备或所属发布不存在；
- device_not_in_target_group：设备不属于任何进行中发布（未被分配到本次升级）；
- below_min_version：设备当前固件版本低于固件要求的最低版本；
- release_paused：发布被手动/自动暂停（暂停后上报被拒绝，保证两侧状态一致）；
- release_rolling_back：发布已回滚（回滚中的设备上报被拒绝）；
- release_finished：发布已完成；
- invalid_stage：未知阶段；
- stale_stage：阶段顺序回退（例如已到 REBOOT 又报 VERIFY）。

阶段规则：DOWNLOAD < VERIFY < INSTALL < REBOOT < SUCCESS/FAIL；
同阶段重复上报幂等；SUCCESS/FAIL 为终态，之后只允许重复同一终态。
成功升级后设备当前固件版本更新为目标版本；失败记录 failure_reason。
每次 FAIL 后按 失败数/(成功数+失败数) 计算比例，达到阈值自动暂停并记录原因。
"""

from .decision import log_decision
from .errors import OtaError, RejectReason
from .models import (
    DeviceView,
    PauseReason,
    ReleaseState,
    ReportStage,
    STAGE_ORDER,
    STAGE_TO_UPGRADE_STATUS,
    UpgradeStatus,
)
from .device_service import DeviceService
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

            # 3) 设备必须属于某个进行中发布（即在目标分组且已被批次分配）。
            #    回滚后 active 指针虽已清除，但最近记录指向已回滚发布时，
            #    必须优先给出“回滚中”这一可区分原因。
            release_id = self.store.get_active(device_id)
            release = None
            record = None
            if release_id is None:
                latest = self.store.latest_record_for_device(device_id)
                if latest is not None:
                    latest_release = self.store.get_release(latest.release_id)
                    if latest_release is not None and latest_release.state == ReleaseState.ROLLED_BACK:
                        raise OtaError(
                            RejectReason.RELEASE_ROLLING_BACK,
                            f"release_id={latest.release_id} rolled back",
                        )
                raise OtaError(
                    RejectReason.DEVICE_NOT_IN_TARGET_GROUP,
                    f"device_id={device_id} not assigned to any active release",
                )
            release = self.store.get_release(release_id)
            if release is None:
                raise OtaError(RejectReason.RELEASE_NOT_FOUND, f"release_id={release_id}")
            record = self.store.get_record(release_id, device_id)
            if record is None:
                raise OtaError(
                    RejectReason.DEVICE_NOT_IN_TARGET_GROUP,
                    f"device_id={device_id} has no rollout record in {release_id}",
                )

            # 4) 已处于终态的设备：
            #    - 重复同一终态（SUCCESS/SUCCESS 或 FAIL/FAIL）一律幂等接受，
            #      即使发布随后被暂停/完成/回滚，重传也得到同样结论；
            #    - 尝试其它阶段则按发布当前状态给出可区分原因（暂停/完成/回滚），
            #      仅当发布仍在进行时才回落为 stale_stage。
            is_terminal = record.status in (UpgradeStatus.SUCCEEDED, UpgradeStatus.FAILED)
            same_terminal = (
                (record.status == UpgradeStatus.SUCCEEDED and stage == ReportStage.SUCCESS)
                or (record.status == UpgradeStatus.FAILED and stage == ReportStage.FAIL)
            )
            if is_terminal and same_terminal:
                log_decision(
                    "report", {"device_id": device_id, "stage": stage.value},
                    f"idempotent_already_{record.status.value}",
                )
                return self._device_view(device_id)
            if is_terminal:
                if release.state == ReleaseState.ROLLED_BACK:
                    raise OtaError(RejectReason.RELEASE_ROLLING_BACK,
                                   f"release_id={release_id} rolled back")
                if release.state in (ReleaseState.PAUSED, ReleaseState.AUTO_PAUSED):
                    raise OtaError(RejectReason.RELEASE_PAUSED,
                                   f"release_id={release_id} state={release.state.value}")
                if release.state == ReleaseState.COMPLETED:
                    raise OtaError(RejectReason.RELEASE_FINISHED,
                                   f"release_id={release_id} completed")
                raise OtaError(RejectReason.STALE_STAGE,
                               f"already {record.status.value}, cannot report {stage.value}")

            # 5) 非终态设备：发布状态决定可区分的拒绝原因
            if release.state == ReleaseState.ROLLED_BACK:
                raise OtaError(
                    RejectReason.RELEASE_ROLLING_BACK,
                    f"release_id={release_id} rolled back",
                )
            if release.state in (ReleaseState.PAUSED, ReleaseState.AUTO_PAUSED):
                raise OtaError(
                    RejectReason.RELEASE_PAUSED,
                    f"release_id={release_id} state={release.state.value}",
                )
            if release.state == ReleaseState.COMPLETED:
                raise OtaError(
                    RejectReason.RELEASE_FINISHED, f"release_id={release_id} completed"
                )

            # 6) 最低版本门槛
            firmware = self.store.get_firmware(release.firmware_id)
            if firmware is None:
                raise OtaError(RejectReason.FIRMWARE_NOT_FOUND, f"firmware_id={release.firmware_id}")
            if not is_version_at_least(device.firmware_version, firmware.min_device_version):
                raise OtaError(
                    RejectReason.BELOW_MIN_VERSION,
                    f"device={device.firmware_version} minimum={firmware.min_device_version}",
                )

            # 7) 阶段单调性：不能回退
            if record.last_stage is not None and STAGE_ORDER[stage] < STAGE_ORDER[record.last_stage]:
                raise OtaError(
                    RejectReason.STALE_STAGE,
                    f"stage {stage.value} <= last {record.last_stage.value}",
                )

            # 8) 应用上报
            record.last_stage = stage
            record.reported_count += 1
            record.updated_at = self.store.clock.now()
            new_status = STAGE_TO_UPGRADE_STATUS[stage]
            record.status = new_status

            auto_paused = False
            triggered_ratio = None
            if stage == ReportStage.FAIL:
                record.failure_reason = failure_reason or "unspecified_failure"
                triggered_ratio = self._failure_ratio(release_id)
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
                {"device_id": device_id, "release_id": release_id, "stage": stage.value,
                 "failure_reason": failure_reason},
                "auto_paused_failure_threshold" if auto_paused else "accepted",
                upgrade_status=new_status.value,
                device_version=device.firmware_version,
                failure_ratio=triggered_ratio,
                threshold=release.failure_threshold,
            )

            # 未触发自动暂停时才收口批次/判定完成；自动暂停优先保留暂停态
            if not auto_paused:
                ReleaseService(self.store).settle_progress(release_id)

            return self._device_view(device)

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

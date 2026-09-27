"""设备机群 OTA 的核心数据模型（进程内本地存储）。"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---- 设备相关 ----

class DeviceStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"


# ---- 升级结果上报阶段 ----

class ReportStage(str, Enum):
    DOWNLOAD = "download"
    VERIFY = "verify"
    INSTALL = "install"
    REBOOT = "reboot"
    SUCCESS = "success"
    FAIL = "fail"


# 各阶段的先后顺序；SUCCESS/FAIL 为终态
STAGE_ORDER: dict[ReportStage, int] = {
    ReportStage.DOWNLOAD: 0,
    ReportStage.VERIFY: 1,
    ReportStage.INSTALL: 2,
    ReportStage.REBOOT: 3,
    ReportStage.SUCCESS: 4,
    ReportStage.FAIL: 4,
}


# ---- 设备在某次发布中的升级状态 ----

class UpgradeStatus(str, Enum):
    PENDING = "pending"        # 已分配但尚未开始上报
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    INSTALLING = "installing"
    REBOOTING = "rebooting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"


STAGE_TO_UPGRADE_STATUS: dict[ReportStage, UpgradeStatus] = {
    ReportStage.DOWNLOAD: UpgradeStatus.DOWNLOADING,
    ReportStage.VERIFY: UpgradeStatus.VERIFYING,
    ReportStage.INSTALL: UpgradeStatus.INSTALLING,
    ReportStage.REBOOT: UpgradeStatus.REBOOTING,
    ReportStage.SUCCESS: UpgradeStatus.SUCCEEDED,
    ReportStage.FAIL: UpgradeStatus.FAILED,
}

TERMINAL_UPGRADE_STATUSES = frozenset({
    UpgradeStatus.SUCCEEDED,
    UpgradeStatus.FAILED,
    UpgradeStatus.ROLLED_BACK,
})


# ---- 发布任务相关 ----

class ReleaseState(str, Enum):
    PENDING = "pending"      # 已创建，尚未推进任何批次
    IN_PROGRESS = "in_progress"
    PAUSED = "paused"        # 手动暂停
    AUTO_PAUSED = "auto_paused"  # 失败比例触发的自动暂停
    ROLLED_BACK = "rolled_back"
    COMPLETED = "completed"


ACTIVE_RELEASE_STATES = frozenset({
    ReleaseState.PENDING,
    ReleaseState.IN_PROGRESS,
    ReleaseState.PAUSED,
    ReleaseState.AUTO_PAUSED,
})

PAUSE_STATES = frozenset({ReleaseState.PAUSED, ReleaseState.AUTO_PAUSED})


class PauseReason(str, Enum):
    MANUAL = "manual"
    FAILURE_THRESHOLD = "failure_threshold"


class BatchState(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    DONE = "done"


@dataclass
class Firmware:
    firmware_id: str
    version: str
    min_device_version: str          # 设备当前版本不得低于该版本
    description: str = ""


@dataclass
class Device:
    device_id: str
    group: str
    firmware_version: str
    last_heartbeat_ts: Optional[float] = None
    heartbeat_count: int = 0


@dataclass
class DeviceView:
    """设备视图：任何时刻重新查询都应得到同样结论。"""
    device_id: str
    group: str
    firmware_version: str
    online: bool
    last_heartbeat_ts: Optional[float]
    heartbeat_count: int
    active_release_id: Optional[str]
    upgrade_status: Optional[UpgradeStatus]
    target_version: Optional[str]
    last_stage: Optional[ReportStage]
    failure_reason: Optional[str]


@dataclass
class RolloutRecord:
    """单台设备在一次发布中的升级记录。"""
    device_id: str
    release_id: str
    batch_index: int
    status: UpgradeStatus = UpgradeStatus.PENDING
    last_stage: Optional[ReportStage] = None
    failure_reason: Optional[str] = None
    from_version: Optional[str] = None
    reported_count: int = 0
    assigned_at: Optional[float] = None
    updated_at: Optional[float] = None


@dataclass
class Batch:
    index: int
    device_ids: list[str] = field(default_factory=list)
    state: BatchState = BatchState.PENDING


@dataclass
class Release:
    release_id: str
    firmware_id: str
    target_group: str
    batch_size: int
    failure_threshold: float           # 0~1，已上报设备中失败占比达到该值则自动暂停
    state: ReleaseState = ReleaseState.PENDING
    pause_reason: Optional[PauseReason] = None
    batches: list[Batch] = field(default_factory=list)
    next_batch_to_open: int = 0        # 下一个待打开的批次序号
    skipped_device_ids: list[str] = field(default_factory=list)
    created_at: Optional[float] = None
    updated_at: Optional[float] = None


@dataclass
class ReleaseView:
    release_id: str
    firmware_id: str
    target_version: Optional[str]
    target_group: str
    batch_size: int
    failure_threshold: float
    state: ReleaseState
    pause_reason: Optional[PauseReason]
    total_targets: int
    assigned: int
    succeeded: int
    failed: int
    rolled_back: int
    pending_or_inflight: int
    failure_ratio: float
    next_batch_to_open: int
    batches: list[dict]
    skipped_device_ids: list[str]

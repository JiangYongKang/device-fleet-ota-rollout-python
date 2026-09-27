"""领域模型与状态枚举（纯数据，不含业务逻辑）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .errors import RejectCode  # re-export convenience  # noqa: F401


class OnlineState(str, Enum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"


class ReleaseState(str, Enum):
    PENDING = "PENDING"                    # 已创建，尚未推进任何批次
    IN_PROGRESS = "IN_PROGRESS"            # 灰度推进中
    PAUSED = "PAUSED"                      # 暂停（手动 / 失败阈值自动）
    ROLLING_BACK = "ROLLING_BACK"          # 回滚中：拒绝新升级结果
    ROLLED_BACK = "ROLLED_BACK"            # 回滚完成（终态）
    ROLLOUT_COMPLETE = "ROLLOUT_COMPLETE"  # 批次全部发完，仍有设备在途
    COMPLETED = "COMPLETED"                # 批次发完且全部设备终态（终态）


# 仍接受升级结果上报的状态
REPORTABLE_STATES = frozenset(
    {ReleaseState.IN_PROGRESS, ReleaseState.ROLLOUT_COMPLETE}
)


class PauseReason(str, Enum):
    MANUAL = "MANUAL"
    FAILURE_THRESHOLD = "FAILURE_THRESHOLD"


class Stage(str, Enum):
    DOWNLOAD = "DOWNLOAD"
    VERIFY = "VERIFY"
    INSTALL = "INSTALL"
    REBOOT = "REBOOT"


# 阶段顺序：上报必须按此顺序单调推进
STAGE_ORDER: tuple[Stage, ...] = (
    Stage.DOWNLOAD,
    Stage.VERIFY,
    Stage.INSTALL,
    Stage.REBOOT,
)


class AssignmentState(str, Enum):
    PENDING = "PENDING"        # 已分配，尚未收到任何上报
    IN_PROGRESS = "IN_PROGRESS"
    SUCCEEDED = "SUCCEEDED"    # 终态：REBOOT 成功
    FAILED = "FAILED"          # 终态：任一阶段失败


@dataclass
class Device:
    device_id: str
    group: str
    firmware_version: str
    registered_at: float
    last_heartbeat_at: float | None = None

    def is_online(self, now: float, timeout_seconds: float) -> bool:
        """在线判定：从未心跳 => 离线；超时（含临界相等）=> 离线。"""
        if self.last_heartbeat_at is None:
            return False
        return now - self.last_heartbeat_at < timeout_seconds


@dataclass
class Firmware:
    firmware_id: str
    version: str
    min_device_version: str
    created_at: float
    description: str = ""


@dataclass
class Assignment:
    device_id: str
    release_id: str
    batch_index: int
    assigned_at: float
    version_at_assignment: str
    state: AssignmentState = AssignmentState.PENDING
    stage: Stage | None = None
    failed_stage: Stage | None = None
    last_message: str = ""
    updated_at: float = 0.0


@dataclass
class BatchAllocation:
    batch_index: int
    device_ids: list[str]
    allocated_at: float


@dataclass
class ReleaseEvent:
    """发布生命周期事件，保证最终状态可解释、可复现。"""

    at: float
    kind: str
    detail: str


@dataclass
class Release:
    release_id: str
    firmware_id: str
    target_group: str | None
    batch_sizes: list[int]
    failure_threshold: float
    created_at: float
    candidate_ids: list[str]
    state: ReleaseState = ReleaseState.PENDING
    pause_reason: PauseReason | None = None
    pause_detail: str | None = None
    updated_at: float = 0.0
    allocations: list[BatchAllocation] = field(default_factory=list)
    assignments: dict[str, Assignment] = field(default_factory=dict)
    # request_id -> batch_index：推进接口的幂等记录
    advance_requests: dict[str, int] = field(default_factory=dict)
    # 失败比例检查点：自上次放行以来新增的终态成功/失败数
    checkpoint_succeeded: int = 0
    checkpoint_failed: int = 0
    events: list[ReleaseEvent] = field(default_factory=list)

    def next_batch_index(self) -> int:
        return len(self.allocations)

    def terminal_stats(self) -> tuple[int, int, int]:
        """返回 (成功数, 失败数, 已出终态数)。"""
        succeeded = sum(
            1 for a in self.assignments.values()
            if a.state == AssignmentState.SUCCEEDED
        )
        failed = sum(
            1 for a in self.assignments.values()
            if a.state == AssignmentState.FAILED
        )
        return succeeded, failed, succeeded + failed

"""可区分的拒绝原因与错误类型。

所有异常输入都通过显式的拒绝码返回，保证调用方与日志
可以精确区分“为什么不被接受”，而不是静默成功。
"""

from __future__ import annotations

from enum import Enum


class RejectCode(str, Enum):
    # ---- 通用 ----
    DEVICE_NOT_FOUND = "DEVICE_NOT_FOUND"
    FIRMWARE_NOT_FOUND = "FIRMWARE_NOT_FOUND"
    RELEASE_NOT_FOUND = "RELEASE_NOT_FOUND"
    RELEASE_ALREADY_EXISTS = "RELEASE_ALREADY_EXISTS"

    # ---- 心跳 ----
    STALE_HEARTBEAT = "STALE_HEARTBEAT"  # 携带时间戳比已记录的更旧

    # ---- 发布推进 ----
    RELEASE_NOT_RUNNING = "RELEASE_NOT_RUNNING"  # 非进行中，不可推进
    RELEASE_NOT_PAUSED = "RELEASE_NOT_PAUSED"  # 非暂停，不可继续
    RELEASE_ALREADY_FINISHED = "RELEASE_ALREADY_FINISHED"  # 批次已全部发完
    BATCH_PLAN_EMPTY = "BATCH_PLAN_EMPTY"

    # ---- 升级结果上报 ----
    NOT_IN_TARGET_GROUP = "NOT_IN_TARGET_GROUP"
    VERSION_BELOW_MINIMUM = "VERSION_BELOW_MINIMUM"
    RELEASE_PAUSED = "RELEASE_PAUSED"
    RELEASE_ROLLING_BACK = "RELEASE_ROLLING_BACK"
    RELEASE_FINISHED = "RELEASE_FINISHED"
    NO_ACTIVE_ASSIGNMENT = "NO_ACTIVE_ASSIGNMENT"
    STAGE_OUT_OF_ORDER = "STAGE_OUT_OF_ORDER"
    ALREADY_TERMINAL = "ALREADY_TERMINAL"

    # ---- 固件 ----
    FIRMWARE_ALREADY_EXISTS = "FIRMWARE_ALREADY_EXISTS"
    FIRMWARE_VERSION_CONFLICT = "FIRMWARE_VERSION_CONFLICT"


class OtaError(Exception):
    """领域错误基类：携带可区分的拒绝码与解释信息。"""

    def __init__(self, code: RejectCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:  # pragma: no cover - 调试辅助
        return f"{self.code.value}: {self.message}"

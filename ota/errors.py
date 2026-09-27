"""可区分的业务拒绝原因。"""

from enum import Enum


class OtaError(Exception):
    def __init__(self, reason: "RejectReason", detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)


class RejectReason(str, Enum):
    DEVICE_NOT_FOUND = "device_not_found"
    DEVICE_ALREADY_REGISTERED = "device_already_registered"
    RELEASE_NOT_FOUND = "release_not_found"
    FIRMWARE_NOT_FOUND = "firmware_not_found"
    DEVICE_IN_ANOTHER_ACTIVE_RELEASE = "device_in_another_active_release"
    DEVICE_NOT_IN_TARGET_GROUP = "device_not_in_target_group"
    BELOW_MIN_VERSION = "below_min_version"
    RELEASE_PAUSED = "release_paused"
    RELEASE_ROLLING_BACK = "release_rolling_back"
    RELEASE_FINISHED = "release_finished"
    INVALID_STAGE = "invalid_stage"
    STALE_STAGE = "stale_stage"
    ILLEGAL_RELEASE_STATE = "illegal_release_state"
    BATCH_NOT_OPEN = "batch_not_open"

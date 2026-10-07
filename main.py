"""设备机群固件升级（OTA）HTTP 入口。

进程内本地存储模拟设备与固件数据，不依赖任何外部服务或真实凭据。
所有判定使用可注入时钟（SystemClock / FakeClock），结果确定、可复现。
"""

import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ota import (
    DeviceService,
    FakeClock,
    ReleaseService,
    ReportService,
    Store,
    SystemClock,
)
from ota.errors import OtaError
from ota.models import Firmware, ReportStage

# 进程内单例存储。设置 OTA_FAKE_CLOCK=1 时使用从 0 开始的可控时钟（便于演示/测试）
_clock = FakeClock(0.0) if os.getenv("OTA_FAKE_CLOCK") == "1" else SystemClock()
store = Store(_clock)
device_service = DeviceService(store)
release_service = ReleaseService(store)
report_service = ReportService(store)

app = FastAPI(title="device-fleet-ota", version="0.1.0")


@app.exception_handler(OtaError)
def ota_error_handler(_request, exc: OtaError):
    # 业务拒绝统一返回 409 + 可区分 reason，调用方据此分支处理
    return JSONResponse(
        status_code=409,
        content={"error": exc.reason.value, "detail": exc.detail},
    )


@app.get("/")
def read_root():
    return {"service": "device-fleet-ota", "status": "ok"}


@app.get("/health")
def health():
    return {"status": "ok", "clock_now": store.clock.now()}


# ---- device ----
@app.post("/devices/{device_id}/register")
def register_device(device_id: str, group: str, firmware_version: str):
    return _device_dict(device_service.register(device_id, group, firmware_version))


@app.post("/devices/{device_id}/heartbeat")
def heartbeat(device_id: str, ts: float | None = None):
    return _device_dict(device_service.heartbeat(device_id, ts))


@app.get("/devices/{device_id}")
def get_device(device_id: str):
    return _device_dict(device_service.get_device(device_id))


@app.get("/devices/{device_id}/history")
def get_device_history(device_id: str):
    # 设备参与过的历次发布：每轮的版本基线、目标版本与最终结果，按轮次升序
    return [_history_dict(v) for v in device_service.release_history(device_id)]


@app.get("/devices")
def list_devices():
    return [_device_dict(v) for v in device_service.list_devices()]


# ---- firmware ----
@app.post("/firmwares/{firmware_id}")
def add_firmware(firmware_id: str, version: str, min_device_version: str, description: str = ""):
    firmware = Firmware(
        firmware_id=firmware_id,
        version=version,
        min_device_version=min_device_version,
        description=description,
    )
    store.add_firmware(firmware)
    return {"firmware_id": firmware_id, "version": version,
            "min_device_version": min_device_version}


# ---- release ----
@app.post("/releases/{release_id}")
def create_release(release_id: str, firmware_id: str, target_group: str,
                   batch_size: int, failure_threshold: float):
    return _release_dict(release_service.create_release(
        release_id, firmware_id, target_group, batch_size, failure_threshold))


@app.post("/releases/{release_id}/advance")
def advance_release(release_id: str):
    return _release_dict(release_service.advance(release_id))


@app.post("/releases/{release_id}/pause")
def pause_release(release_id: str):
    return _release_dict(release_service.pause(release_id))


@app.post("/releases/{release_id}/resume")
def resume_release(release_id: str):
    return _release_dict(release_service.resume(release_id))


@app.post("/releases/{release_id}/rollback")
def rollback_release(release_id: str):
    return _release_dict(release_service.rollback(release_id))


@app.get("/releases/{release_id}")
def get_release(release_id: str):
    return _release_dict(release_service.get_release(release_id))


@app.get("/releases")
def list_releases():
    return [_release_dict(v) for v in release_service.list_releases()]


# ---- report ----
@app.post("/devices/{device_id}/report")
def report_upgrade_result(
    device_id: str,
    stage: ReportStage,
    failure_reason: str | None = None,
    release_id: str | None = None,
):
    # release_id 可选：用于连续多轮升级时明确“这是哪一轮”的上报，
    # 防止旧一轮迟到上报被写到新一轮记录上。缺省按设备当前进行中的发布路由。
    return _device_dict(
        report_service.report_result(device_id, stage, failure_reason, release_id)
    )


# ---- serializers ----
def _device_dict(v):
    return {
        "device_id": v.device_id,
        "group": v.group,
        "firmware_version": v.firmware_version,
        "online": v.online,
        "last_heartbeat_ts": v.last_heartbeat_ts,
        "heartbeat_count": v.heartbeat_count,
        "active_release_id": v.active_release_id,
        "upgrade_status": v.upgrade_status.value if v.upgrade_status else None,
        "target_version": v.target_version,
        "last_stage": v.last_stage.value if v.last_stage else None,
        "failure_reason": v.failure_reason,
    }


def _release_dict(v):
    return {
        "release_id": v.release_id,
        "firmware_id": v.firmware_id,
        "target_version": v.target_version,
        "target_group": v.target_group,
        "batch_size": v.batch_size,
        "failure_threshold": v.failure_threshold,
        "state": v.state.value,
        "pause_reason": v.pause_reason.value if v.pause_reason else None,
        "total_targets": v.total_targets,
        "assigned": v.assigned,
        "succeeded": v.succeeded,
        "failed": v.failed,
        "rolled_back": v.rolled_back,
        "pending_or_inflight": v.pending_or_inflight,
        "failure_ratio": v.failure_ratio,
        "next_batch_to_open": v.next_batch_to_open,
        "batches": v.batches,
        "skipped_device_ids": v.skipped_device_ids,
        "skipped_devices": v.skipped_devices,
    }


def _history_dict(v):
    return {
        "release_id": v.release_id,
        "firmware_id": v.firmware_id,
        "from_version": v.from_version,
        "target_version": v.target_version,
        "batch_index": v.batch_index,
        "status": v.status.value,
        "last_stage": v.last_stage.value if v.last_stage else None,
        "failure_reason": v.failure_reason,
        "release_state": v.release_state.value if v.release_state else None,
        "active": v.active,
        "assigned_at": v.assigned_at,
        "updated_at": v.updated_at,
    }

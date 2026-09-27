"""FastAPI HTTP 适配层：把请求映射到 OtaService。

进程内单例存储 + 可注入时钟（默认单调时钟）；
错误统一以带 reject_code 的 JSON 返回，保证 HTTP 调用方
也能区分拒绝原因。
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .clock import Clock, SystemClock
from .errors import OtaError
from .models import Stage
from .service import OtaService
from .storage import InMemoryStore

router = APIRouter()


# ---------------------------------------------------------------- schemas
class RegisterIn(BaseModel):
    device_id: str
    group: str
    firmware_version: str


class HeartbeatIn(BaseModel):
    ts: float | None = None


class FirmwareIn(BaseModel):
    firmware_id: str
    version: str
    min_device_version: str
    description: str = ""


class ReleaseIn(BaseModel):
    release_id: str
    firmware_id: str
    target_group: str | None = None
    batch_sizes: list[int] = Field(min_length=1)
    failure_threshold: float = Field(ge=0.0, le=1.0)


class AdvanceIn(BaseModel):
    request_id: str


class ReportIn(BaseModel):
    device_id: str
    stage: Stage
    success: bool
    message: str = ""


def create_app(service: OtaService | None = None, clock: Clock | None = None) -> FastAPI:
    app = FastAPI(title="Device Fleet OTA (in-process)")
    if service is None:
        service = OtaService(InMemoryStore(), clock or SystemClock())
    app.state.service = service
    app.include_router(router)

    @app.exception_handler(OtaError)
    async def _on_ota_error(_request, exc: OtaError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "reject_code": exc.code.value,
                "message": exc.message,
            },
        )

    return app


def _svc(request: Request) -> OtaService:
    return request.app.state.service


# ---------------------------------------------------------------- devices
@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/devices", status_code=201)
def register_device(body: RegisterIn, request: Request):
    device = _svc(request).register_device(
        body.device_id, body.group, body.firmware_version
    )
    return {
        "device_id": device.device_id,
        "group": device.group,
        "firmware_version": device.firmware_version,
        "registered_at": device.registered_at,
    }


@router.post("/devices/{device_id}/heartbeat")
def heartbeat(device_id: str, body: HeartbeatIn, request: Request):
    r = _svc(request).heartbeat(device_id, body.ts)
    return {
        "accepted": r.accepted,
        "device_id": r.device_id,
        "online": r.online,
        "last_heartbeat_at": r.last_heartbeat_at,
        "reject_code": r.code.value if r.code else None,
        "message": r.message,
    }


@router.get("/devices/{device_id}")
def device_view(device_id: str, request: Request):
    v = _svc(request).get_device_view(device_id)
    return {
        "device_id": v.device_id,
        "group": v.group,
        "firmware_version": v.firmware_version,
        "online": v.online,
        "last_heartbeat_at": v.last_heartbeat_at,
        "active_release_id": v.active_release_id,
        "assignment_state": v.assignment_state.value if v.assignment_state else None,
        "assignment_stage": v.assignment_stage.value if v.assignment_stage else None,
    }


# ---------------------------------------------------------------- firmware
@router.post("/firmwares", status_code=201)
def create_firmware(body: FirmwareIn, request: Request):
    fw = _svc(request).create_firmware(
        body.firmware_id,
        body.version,
        body.min_device_version,
        body.description,
    )
    return {
        "firmware_id": fw.firmware_id,
        "version": fw.version,
        "min_device_version": fw.min_device_version,
        "created_at": fw.created_at,
    }


# ---------------------------------------------------------------- releases
@router.post("/releases", status_code=201)
def create_release(body: ReleaseIn, request: Request):
    r = _svc(request).create_release(
        body.release_id,
        body.firmware_id,
        body.target_group,
        body.batch_sizes,
        body.failure_threshold,
    )
    return {
        "release_id": r.release_id,
        "state": r.state.value,
        "candidate_count": len(r.candidate_ids),
        "batch_sizes": r.batch_sizes,
    }


@router.post("/releases/{release_id}/advance")
def advance(release_id: str, body: AdvanceIn, request: Request):
    r = _svc(request).advance_batch(release_id, body.request_id)
    return {
        "ok": r.ok,
        "state": r.state.value,
        "batch_index": r.batch_index,
        "device_ids": r.device_ids,
        "auto_paused": r.auto_paused,
        "pause_reason": r.pause_reason,
        "reject_code": r.code.value if r.code else None,
        "message": r.message,
    }


@router.post("/releases/{release_id}/pause")
def pause(release_id: str, request: Request):
    r = _svc(request).pause_release(release_id)
    return {
        "ok": r.ok,
        "state": r.state.value,
        "pause_reason": r.pause_reason,
        "reject_code": r.code.value if r.code else None,
        "message": r.message,
    }


@router.post("/releases/{release_id}/resume")
def resume(release_id: str, request: Request):
    r = _svc(request).resume_release(release_id)
    return {
        "ok": r.ok,
        "state": r.state.value,
        "reject_code": r.code.value if r.code else None,
        "message": r.message,
    }


@router.post("/releases/{release_id}/rearm")
def rearm(release_id: str, request: Request):
    r = _svc(request).rearm_release(release_id)
    return {
        "ok": r.ok,
        "state": r.state.value,
        "reject_code": r.code.value if r.code else None,
        "message": r.message,
    }


@router.post("/releases/{release_id}/rollback")
def rollback(release_id: str, request: Request):
    r = _svc(request).rollback_release(release_id)
    return {
        "ok": r.ok,
        "state": r.state.value,
        "reject_code": r.code.value if r.code else None,
        "message": r.message,
    }


@router.post("/releases/{release_id}/rollback/settle")
def settle_rollback(release_id: str, request: Request):
    r = _svc(request).settle_rollback(release_id)
    return {
        "ok": r.ok,
        "state": r.state.value,
        "reject_code": r.code.value if r.code else None,
        "message": r.message,
    }


@router.get("/releases/{release_id}")
def release_detail(release_id: str, request: Request):
    svc = _svc(request)
    release = svc.store.get_release(release_id)
    if release is None:
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "reject_code": "RELEASE_NOT_FOUND",
                "message": f"release {release_id!r} not found",
            },
        )
    succeeded, failed, terminal = release.terminal_stats()
    return {
        "release_id": release.release_id,
        "state": release.state.value,
        "pause_reason": release.pause_reason.value if release.pause_reason else None,
        "pause_detail": release.pause_detail,
        "firmware_id": release.firmware_id,
        "target_group": release.target_group,
        "batch_sizes": release.batch_sizes,
        "allocated_batches": len(release.allocations),
        "stats": {
            "succeeded": succeeded,
            "failed": failed,
            "terminal": terminal,
            "assigned": len(release.assignments),
        },
        "events": [
            {"at": e.at, "kind": e.kind, "detail": e.detail}
            for e in release.events
        ],
    }


# ---------------------------------------------------------------- reports
@router.post("/releases/{release_id}/reports")
def report_upgrade(release_id: str, body: ReportIn, request: Request):
    r = _svc(request).report_upgrade(
        body.device_id,
        release_id,
        body.stage,
        body.success,
        body.message,
    )
    return {
        "ok": r.ok,
        "device_id": r.device_id,
        "assignment_state": r.assignment_state.value,
        "stage": r.stage.value if r.stage else None,
        "failed_stage": r.failed_stage.value if r.failed_stage else None,
        "reject_code": r.code.value if r.code else None,
        "message": r.message,
    }

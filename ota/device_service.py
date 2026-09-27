"""设备注册与心跳上报。

在线判定规则：
- 设备注册后在第一次心跳前视为离线；
- 若 now - last_heartbeat_ts >= heartbeat_timeout_seconds，则判为离线，否则在线；
- 心跳携带的时间戳（缺省取时钟当前值）必须比已记录的时间戳更新才会生效；
  旧心跳（乱序/迟到/并发竞争失败的旧值）会被忽略，因此既不会把已超时设备
  误判为在线，也不会让过期时间覆盖新时间。
"""

from .decision import log_decision
from .errors import OtaError, RejectReason
from .models import (
    Device,
    DeviceView,
    TERMINAL_UPGRADE_STATUSES,
    UpgradeStatus,
)
from .store import Store

DEFAULT_HEARTBEAT_TIMEOUT = 30.0


class DeviceService:
    def __init__(self, store: Store, heartbeat_timeout: float = DEFAULT_HEARTBEAT_TIMEOUT) -> None:
        self.store = store
        self.heartbeat_timeout = heartbeat_timeout

    # ---------- public ----------

    def register(self, device_id: str, group: str, firmware_version: str) -> DeviceView:
        with self.store.lock:
            if self.store.get_device(device_id) is not None:
                raise OtaError(
                    RejectReason.DEVICE_ALREADY_REGISTERED,
                    f"device_id={device_id}",
                )
            device = Device(
                device_id=device_id,
                group=group,
                firmware_version=firmware_version,
                last_heartbeat_ts=None,
                heartbeat_count=0,
            )
            self.store.add_device(device)
            log_decision(
                "register",
                {"device_id": device_id, "group": group, "firmware_version": firmware_version},
                "registered(offline_until_first_heartbeat)",
            )
            return self._view(device)

    def heartbeat(self, device_id: str, ts: float | None = None) -> DeviceView:
        with self.store.lock:
            device = self.store.get_device(device_id)
            if device is None:
                raise OtaError(RejectReason.DEVICE_NOT_FOUND, f"device_id={device_id}")
            incoming = self.store.clock.now() if ts is None else float(ts)
            now = self.store.clock.now()
            if device.last_heartbeat_ts is not None and incoming <= device.last_heartbeat_ts:
                # 乱序/重复/并发中的旧心跳：忽略，绝不让过期时间覆盖新值
                log_decision(
                    "heartbeat",
                    {"device_id": device_id, "incoming_ts": incoming,
                     "recorded_ts": device.last_heartbeat_ts, "now": now},
                    "ignored_stale",
                )
                return self._view(device)
            device.last_heartbeat_ts = incoming
            device.heartbeat_count += 1
            online = self._is_online(device, now)
            log_decision(
                "heartbeat",
                {"device_id": device_id, "incoming_ts": incoming, "now": now,
                 "timeout": self.heartbeat_timeout},
                "accepted" if online else "accepted_but_already_offline",
                heartbeat_count=device.heartbeat_count,
            )
            return self._view(device)

    def get_device(self, device_id: str) -> DeviceView:
        with self.store.lock:
            device = self.store.get_device(device_id)
            if device is None:
                raise OtaError(RejectReason.DEVICE_NOT_FOUND, f"device_id={device_id}")
            return self._view(device)

    def list_devices(self) -> list[DeviceView]:
        with self.store.lock:
            return [self._view(d) for d in self.store.list_devices()]

    # ---------- internals ----------

    def _is_online(self, device: Device, now: float | None = None) -> bool:
        if device.last_heartbeat_ts is None:
            return False
        now = self.store.clock.now() if now is None else now
        return (now - device.last_heartbeat_ts) < self.heartbeat_timeout

    def _view(self, device: Device) -> DeviceView:
        now = self.store.clock.now()
        online = self._is_online(device, now)
        active_release_id = self.store.get_active(device.device_id)
        upgrade_status: UpgradeStatus | None = None
        target_version: str | None = None
        last_stage = None
        failure_reason: str | None = None
        release_id = active_release_id
        rec = None
        if release_id is not None:
            rec = self.store.get_record(release_id, device.device_id)
        else:
            # 已无进行中发布（如回滚清除占用）：回看最近一次发布记录的最终状态，
            # 使设备视图与发布记录在重新查询时保持一致结论。
            latest = self.store.latest_record_for_device(device.device_id)
            if latest is not None and latest.status in TERMINAL_UPGRADE_STATUSES:
                release_id = latest.release_id
                rec = latest
        if rec is not None:
            upgrade_status = rec.status
            last_stage = rec.last_stage
            failure_reason = rec.failure_reason
        if release_id is not None:
            release = self.store.get_release(release_id)
            if release is not None:
                fw = self.store.get_firmware(release.firmware_id)
                if fw is not None:
                    target_version = fw.version
        return DeviceView(
            device_id=device.device_id,
            group=device.group,
            firmware_version=device.firmware_version,
            online=online,
            last_heartbeat_ts=device.last_heartbeat_ts,
            heartbeat_count=device.heartbeat_count,
            active_release_id=active_release_id,
            upgrade_status=upgrade_status,
            target_version=target_version,
            last_stage=last_stage,
            failure_reason=failure_reason,
        )

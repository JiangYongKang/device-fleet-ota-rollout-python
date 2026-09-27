"""进程内本地存储：带锁的字典容器，不做业务判断。

所有跨结构的业务不变式都由 service 在持有同一把 RLock 的情况下完成；
store 方法本身不再加锁（RLock 可重入），只提供确定性的访问顺序。
"""

import threading
from typing import Optional

from .clock import Clock
from .models import Device, Firmware, Release, RolloutRecord


class Store:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.lock = threading.RLock()
        self.devices: dict[str, Device] = {}
        self.firmwares: dict[str, Firmware] = {}
        self.releases: dict[str, Release] = {}
        self.records: dict[tuple[str, str], RolloutRecord] = {}
        self.device_active_release: dict[str, str] = {}
        self.targets_snapshot: dict[str, list[str]] = {}

    # ---- device / firmware ----
    def add_device(self, device: Device) -> None:
        self.devices[device.device_id] = device

    def get_device(self, device_id: str) -> Optional[Device]:
        return self.devices.get(device_id)

    def list_devices(self) -> list[Device]:
        return [self.devices[k] for k in sorted(self.devices)]

    def add_firmware(self, firmware: Firmware) -> None:
        self.firmwares[firmware.firmware_id] = firmware

    def get_firmware(self, firmware_id: str) -> Optional[Firmware]:
        return self.firmwares.get(firmware_id)

    def list_firmwares(self) -> list[Firmware]:
        return [self.firmwares[k] for k in sorted(self.firmwares)]

    # ---- release ----
    def add_release(self, release: Release, targets: list[str]) -> None:
        self.releases[release.release_id] = release
        # 目标设备在创建时即快照并排序，保证批次切分确定、可复现
        self.targets_snapshot[release.release_id] = list(targets)

    def get_release(self, release_id: str) -> Optional[Release]:
        return self.releases.get(release_id)

    def list_releases(self) -> list[Release]:
        return [self.releases[k] for k in sorted(self.releases)]

    def get_targets(self, release_id: str) -> list[str]:
        return list(self.targets_snapshot.get(release_id, ()))

    # ---- rollout record ----
    def get_record(self, release_id: str, device_id: str) -> Optional[RolloutRecord]:
        return self.records.get((release_id, device_id))

    def put_record(self, record: RolloutRecord) -> None:
        self.records[(record.release_id, record.device_id)] = record

    def list_records(self, release_id: str) -> list[RolloutRecord]:
        out = [r for (rid, _d), r in self.records.items() if rid == release_id]
        return sorted(out, key=lambda r: (r.batch_index, r.device_id))

    def latest_record_for_device(self, device_id: str) -> Optional[RolloutRecord]:
        """该设备最近一次发布记录（用于回滚清除 active 后仍能回看最终状态）。"""
        recs = [r for (rid, d), r in self.records.items() if d == device_id]
        if not recs:
            return None
        return sorted(
            recs,
            key=lambda r: (
                r.updated_at if r.updated_at is not None else -1.0,
                r.release_id,
            ),
        )[-1]

    # ---- active release membership ----
    def get_active(self, device_id: str) -> Optional[str]:
        return self.device_active_release.get(device_id)

    def set_active(self, device_id: str, release_id: str) -> None:
        self.device_active_release[device_id] = release_id

    def clear_active(self, device_id: str) -> None:
        self.device_active_release.pop(device_id, None)

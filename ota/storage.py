"""进程内本地存储：单把可重入锁保护所有共享状态。

存储层只负责线程安全的数据访问，不做业务判定；
业务规则全部在 service 层，以便单独测试与复现。
"""

from __future__ import annotations

import threading

from .models import Assignment, BatchAllocation, Device, Firmware, Release


class InMemoryStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.devices: dict[str, Device] = {}
        self.firmwares: dict[str, Firmware] = {}
        self.releases: dict[str, Release] = {}
        # device_id -> release_id：该设备当前归属的“进行中”发布
        self.device_locks: dict[str, str] = {}

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    # ---- device ----
    def get_device(self, device_id: str) -> Device | None:
        return self.devices.get(device_id)

    def put_device(self, device: Device) -> None:
        self.devices[device.device_id] = device

    # ---- firmware ----
    def get_firmware(self, firmware_id: str) -> Firmware | None:
        return self.firmwares.get(firmware_id)

    def put_firmware(self, firmware: Firmware) -> None:
        self.firmwares[firmware.firmware_id] = firmware

    def firmware_exists_for_version(self, version: str) -> bool:
        return any(f.version == version for f in self.firmwares.values())

    # ---- release ----
    def get_release(self, release_id: str) -> Release | None:
        return self.releases.get(release_id)

    def put_release(self, release: Release) -> None:
        self.releases[release.release_id] = release

    # ---- device lock（同一设备同时只能属于一个进行中发布）----
    def lock_device(self, device_id: str, release_id: str) -> bool:
        """尝试把设备标记为 release_id 占用；已被占用则返回 False。"""
        existing = self.device_locks.get(device_id)
        if existing is not None:
            return existing == release_id
        self.device_locks[device_id] = release_id
        return True

    def unlock_device(self, device_id: str, release_id: str) -> None:
        if self.device_locks.get(device_id) == release_id:
            del self.device_locks[device_id]

    def owner_release(self, device_id: str) -> str | None:
        return self.device_locks.get(device_id)

    def release_devices(self, release_id: str) -> list[str]:
        return [
            d for d, owner in self.device_locks.items() if owner == release_id
        ]

    def add_assignment(self, release: Release, assignment: Assignment) -> None:
        release.assignments[assignment.device_id] = assignment

    def add_allocation(self, release: Release, allocation: BatchAllocation) -> None:
        release.allocations.append(allocation)

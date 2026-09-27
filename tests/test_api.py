"""HTTP 层端到端冒烟：拒绝码在 JSON 中同样可区分。"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from ota.api import create_app
from ota.clock import FakeClock
from ota.service import OtaService
from ota.storage import InMemoryStore

log = logging.getLogger("ota.test.api")


@pytest.fixture
def client():
    clock = FakeClock(start=1000.0)
    svc = OtaService(InMemoryStore(), clock, heartbeat_timeout_seconds=30.0)
    return TestClient(create_app(service=svc, clock=clock)), svc, clock


def test_full_flow_over_http(client):
    c, svc, clock = client

    # 注册 + 心跳
    assert c.post("/devices", json={
        "device_id": "d0", "group": "g1", "firmware_version": "1.0.0"
    }).status_code == 201
    assert c.post("/devices/d0/heartbeat", json={}).json()["online"] is True
    clock.advance(31)
    assert c.get("/devices/d0").json()["online"] is False  # 超时

    # 固件 + 发布
    assert c.post("/firmwares", json={
        "firmware_id": "fw", "version": "2.0.0", "min_device_version": "1.0.0"
    }).status_code == 201
    assert c.post("/releases", json={
        "release_id": "r1", "firmware_id": "fw", "target_group": "g1",
        "batch_sizes": [1], "failure_threshold": 0.5,
    }).status_code == 201
    adv = c.post("/releases/r1/advance", json={"request_id": "a1"}).json()
    assert adv["ok"] and adv["device_ids"] == ["d0"]

    # 同 request_id 重复推进 => 幂等回放同一批次
    assert c.post("/releases/r1/advance", json={"request_id": "a1"}).json()[
        "batch_index"
    ] == 0

    # 四阶段成功
    for stage in ("DOWNLOAD", "VERIFY", "INSTALL", "REBOOT"):
        rep = c.post("/releases/r1/reports", json={
            "device_id": "d0", "stage": stage, "success": True,
        }).json()
        assert rep["ok"], rep
    assert c.get("/devices/d0").json()["firmware_version"] == "2.0.0"

    detail = c.get("/releases/r1").json()
    assert detail["state"] == "COMPLETED"
    assert detail["stats"]["succeeded"] == 1
    log.info("release detail events=%s", [e["kind"] for e in detail["events"]])


def test_http_reject_codes_distinguishable(client):
    c, svc, clock = client
    c.post("/devices", json={"device_id": "d0", "group": "g1",
                             "firmware_version": "1.0.0"})
    c.post("/firmwares", json={"firmware_id": "fw", "version": "2.0.0",
                               "min_device_version": "1.0.0"})
    c.post("/releases", json={"release_id": "r1", "firmware_id": "fw",
                              "target_group": "g1", "batch_sizes": [1],
                              "failure_threshold": 0.5})

    # 未分配设备上报
    r = c.post("/releases/r1/reports", json={
        "device_id": "d0", "stage": "DOWNLOAD", "success": True
    }).json()
    assert r["reject_code"] == "NO_ACTIVE_ASSIGNMENT"

    c.post("/releases/r1/advance", json={"request_id": "a1"})
    c.post("/releases/r1/pause")
    paused = c.post("/releases/r1/reports", json={
        "device_id": "d0", "stage": "DOWNLOAD", "success": True
    }).json()
    assert paused["reject_code"] == "RELEASE_PAUSED"

    c.post("/releases/r1/rollback")
    rolling = c.post("/releases/r1/reports", json={
        "device_id": "d0", "stage": "DOWNLOAD", "success": True
    }).json()
    assert rolling["reject_code"] == "RELEASE_ROLLING_BACK"

    c.post("/releases/r1/rollback/settle")
    finished = c.post("/releases/r1/reports", json={
        "device_id": "d0", "stage": "DOWNLOAD", "success": True
    }).json()
    assert finished["reject_code"] == "RELEASE_FINISHED"

    # 不存在的设备
    assert c.get("/devices/nope").status_code == 400

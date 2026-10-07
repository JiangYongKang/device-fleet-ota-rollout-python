# 设备机群固件升级（OTA）服务

一套进程内运行的设备机群 OTA 能力：使用**本地内存存储**模拟设备与固件数据，
**不依赖任何外部服务、数据库或真实凭据**。所有时间判定都走可注入时钟
（`SystemClock` / `FakeClock`），因此在线判定、灰度推进、失败阈值与回滚在
各种异常和并发场景下都能得到**一致、可解释、可复现**的结果。

## 运行与测试

```bash
# 启动 HTTP 服务（FastAPI）
python main.py                 # 或: uvicorn main:app --reload

# 运行全部单元测试（日志会打印每次判定的输入与依据）
python -m unittest discover -s tests -v
```

设置环境变量 `OTA_FAKE_CLOCK=1` 可让 HTTP 服务使用从 0 开始的可控时钟，
便于手工演示与断言；单元测试则直接注入 `FakeClock`。

## 模块结构

| 文件 | 职责 |
| --- | --- |
| `ota/clock.py` | 时钟抽象（系统时钟 / 可控假时钟） |
| `ota/store.py` | 带 `RLock` 的进程内存储（设备、固件、发布、升级记录、占用索引） |
| `ota/device_service.py` | 设备注册、心跳上报、在线判定、设备视图 |
| `ota/release_service.py` | 发布创建、批次灰度推进、暂停 / 继续 / 回滚 |
| `ota/report_service.py` | 升级结果上报（四阶段 + 成功/失败）与可区分拒绝原因 |
| `ota/version_util.py` | 语义版本比较（最低版本门槛） |
| `ota/errors.py` | 业务错误与可区分的 `RejectReason` |
| `ota/decision.py` | 判定日志（打印输入与判定依据） |
| `main.py` | FastAPI HTTP 入口 |

所有跨数据结构的业务不变式都在持有**同一把可重入锁**的服务方法内完成，
存储层不做业务判断，从而在单进程内串行化所有并发的心跳与推进。

---

## 1. 在线状态判定规则

- 设备**注册后、首次心跳前**视为 **离线**。
- 记设备最近一次心跳时间为 `last_heartbeat_ts`，超时阈值为
  `heartbeat_timeout_seconds`（默认 **30s**）：
  - `now - last_heartbeat_ts < 30s` ⇒ **在线**；
  - `now - last_heartbeat_ts >= 30s` ⇒ **离线**（边界即超时）。
- 心跳可携带时间戳，缺省取时钟当前值。**只有当新时间戳严格大于已记录时间戳
  时才生效**；旧心跳（乱序、迟到、重复、并发竞争失败的旧值）一律忽略：
  - 不会把已经超时的设备误判成在线；
  - 不会用过期时间覆盖更新的时间戳；
  - 相同时间戳的重复心跳不计入、不改变状态（幂等）。

验证：`tests/test_device_online.py`
（`test_stale_heartbeat_does_not_overwrite_or_revive`、
`test_concurrent_heartbeats_oldest_never_overwrites`、
`test_concurrent_identical_heartbeats_counted_once`）。

---

## 2. 灰度发布与回滚的状态流转

### 发布状态机

```
PENDING ──advance──▶ IN_PROGRESS ──pause──▶ PAUSED ──resume──▶ IN_PROGRESS
   │                    │  ▲                    │
   │                    │  └──────resume────────┤
   │                    │
   │                    ├──(设备上报 FAIL 使失败比例≥阈值)──▶ AUTO_PAUSED
   │                    │                                   │ resume
   │                    │                                   ▼
   │                    └──────────────────────────────▶ IN_PROGRESS
   │
   ├──rollback──▶ ROLLED_BACK   （PENDING / IN_PROGRESS / PAUSED / AUTO_PAUSED 均可回滚）
   │
   └──(所有批次已打开且批次内设备全部终态)──▶ COMPLETED
```

### 批次灰度推进（advance）

- 创建发布时，目标分组内的设备 **按 `device_id` 排序并快照**，再按
  `batch_size` 顺序切批，批次划分确定、可复现，不受后续注册/心跳影响。
- **一次 `advance` 至多打开一个批次**；上一批次尚有设备未到终态时，
  推进为空操作（返回当前视图），因此**不会跳过或漏掉任何批次**。
- 全部批次打开后重复 / 并发 `advance` 不再分配任何设备（幂等）。
- **同一台设备同一时刻只能属于一个进行中的发布**：
  - 分配时若设备已被另一个进行中的发布占用，则该设备被记入
    `skipped_device_ids`，同时 `skipped` 字段逐台给出
    `{"device_id", "occupied_by"}`（占用它的进行中发布），查询时能直接看出
    “这台设备被哪条发布线占着”，**绝不重复分配**；
  - 批次内设备全部被占用时该批次立即收口，后续批次仍可继续推进。
- 并发安全由同一把锁保证：多线程同时 `advance`，最多只有一个批次被打开、
  每台设备只产生一条升级记录。

### 暂停 / 继续 / 回滚

- `pause`：手动暂停（`pause_reason=manual`）；若已是失败阈值触发的自动暂停，
  保留触发原因不被覆盖。
- `resume`：从手动 / 自动暂停恢复到进行中；对进行中的发布调用为空操作。
- `rollback`：**原子**地把该发布下所有已分配设备的固件版本恢复到发布前版本，
  设备升级状态统一置为 `ROLLED_BACK`，解除设备的“进行中发布”占用，并关闭
  开放批次、把发布置为 `ROLLED_BACK`。重复回滚幂等，回滚后禁止再推进。

### 一致性保证

回滚或暂停后，**设备视图与发布记录反映同一最终状态**，重复查询结论不变：

- 设备视图中的 `firmware_version` / `upgrade_status` 与发布侧每条
  `RolloutRecord` 一致；
- 即使回滚清除了设备的 active 指针，设备视图仍会回看最近一次发布记录，
  显示最终的 `rolled_back`；
- 查询接口是**只读**的，不产生任何状态变更；批次收口 / 发布完成只发生在
  写操作（推进、上报、回滚）内部。

验证：`tests/test_release_batching.py`、`tests/test_rollback_consistency.py`。

---

## 2.1 设备跨发布复用与连续升级

同一批设备会一轮一轮地升级（先 v2、稳定后再 v3，中间可能穿插回滚与重试），
占用与释放规则如下：

### 占用与释放

- 设备被某个批次分配后即被该发布**占用**（`device_active_release` 指针），
  同一时刻至多属于一个进行中的发布；
- 发布**收尾**时释放其全部设备占用：
  - `COMPLETED`（所有批次打开且设备全部终态）——完成判定与释放占用是
    同一次写操作内的原子动作；
  - `ROLLED_BACK`——回滚在恢复版本的同时解除占用；
- 仍处于 `PENDING / IN_PROGRESS / PAUSED / AUTO_PAUSED` 的发布**不释放**设备，
  新发布推进时这些设备会被跳过并在 `skipped` 中注明占用方；
- 释放后设备立即可被下一轮发布分配，因此“发布已结束但新发布一台设备都
  分不到”的情况不会出现；多条发布线并发推进时，每台设备在全系统至多一条
  升级记录，占用指针与记录所属发布始终一致。

### 连续升级的版本基线口径

- 每条升级记录在**分配时刻**快照设备当时实际生效的固件版本为
  `from_version`（基线），判断最低版本门槛与回滚恢复都以该快照为准；
- 上一轮的结果不会串到新一轮：记录按 `(release_id, device_id)` 隔离，
  新一轮分配时重新取设备当前版本作为基线（例如 v1→v2 完成后再推 v3，
  第二轮基线就是 `2.0.0`）；
- **旧一轮迟到的上报**（下载/校验/安装/重启/最终结果）不会污染新一轮：
  上报可显式携带 `release_id`，指向已结束（`release_finished`）或已回滚
  （`release_rolling_back`）的旧发布时会被**区分拒绝**，不写入新一轮记录，
  也不会把已升到新版本的设备改回去；旧发布的同一终态重传仍然幂等，
  但只读不改任何状态。未携带 `release_id` 时按设备当前占用指针路由。

### 升级历史查询

`GET /devices/{id}/history`（或服务层
`DeviceService.get_upgrade_history`）返回设备参与过的**历次发布**：
每轮的 `release_id`、`target_version`、`from_version`、最终 `status`、
`failure_reason`、批次号与时间戳。条目按 `(assigned_at, release_id)`
排序；查询是纯读操作，重复 / 并发查询结论严格一致；设备从未参与发布时
返回空列表。兼容范围：历史只反映本进程内存中发生过的发布，服务重启即清空。

验证：`tests/test_continuous_rollout.py`（正常结束后复用、进行中不抢占、
回滚后解除阻挡、连续两轮基线、迟到上报区分拒绝、并发不重复分配、历史查询）。

---

## 3. 升级结果上报与可区分的拒绝原因

设备分阶段上报，阶段严格区分且**单调推进**：

```
download(下载) < verify(校验) < install(安装) < reboot(重启) < success / fail
```

- 同阶段重复上报幂等；阶段回退（如已到 reboot 又报 verify）被拒绝；
- `success` 将设备当前固件版本更新为目标版本，`fail` 记录 `failure_reason`；
- `success` / `fail` 为终态。重复上报**同一终态**始终幂等（即使发布随后被
  暂停 / 完成 / 回滚，重传也得到相同结论）；终态后改报其它阶段，则按发布
  当前状态给出可区分原因。

异常上报**绝不静默接受**，统一返回 HTTP `409` 与可区分的 `error`：

| `RejectReason` | 含义 |
| --- | --- |
| `device_not_found` | 设备不存在 |
| `device_already_registered` | 设备已注册 |
| `release_not_found` / `firmware_not_found` | 发布 / 固件不存在 |
| `device_in_another_active_release` | 设备正被另一个进行中的发布占用（推进时跳过；或上报指向未分配它的发布） |
| `device_not_in_target_group` | 设备未被任何进行中发布分配（不在目标分组 / 未排到批次） |
| `below_min_version` | 设备当前固件版本低于固件要求的最低版本 |
| `release_paused` | 发布被手动或自动暂停 |
| `release_rolling_back` | 发布正在 / 已经回滚 |
| `release_finished` | 发布已完成 |
| `stale_stage` | 上报阶段顺序回退 |
| `invalid_stage` / `illegal_release_state` / `batch_not_open` | 非法阶段 / 非法发布状态 / 批次未开放 |

验证：`tests/test_report_rejections.py`
（`test_all_rejection_reasons_are_distinct_strings` 断言这些原因字符串彼此不同）。

---

## 4. 失败阈值的含义与验证方式

- 阈值 `failure_threshold ∈ [0,1]` 在创建发布时指定。
- **失败比例**只统计已上报**终态**的设备（下载中等中间阶段与未开始的设备不计入）：

  ```
  failure_ratio = failed / (succeeded + failed)
  ```

- 每当设备上报 `FAIL` 后重算比例；当 `failure_ratio >= failure_threshold` 时，
  发布**自动暂停**为 `AUTO_PAUSED`，并记录 `pause_reason=failure_threshold`，
  发布视图同时暴露 `failure_ratio`、`failed`、`succeeded`，可直接解释“因为
  失败比例达到阈值而停住”。
  - 阈值 `0.0` ⇒ 任意一台失败立即暂停；
  - 阈值 `1.0` ⇒ 只有全部终态设备都失败时才暂停。
- 自动暂停后，新的上报以 `release_paused` 被拒绝；`resume` 后可继续。
  手动 `pause` 不会覆盖 `failure_threshold` 这一触发原因。

验证：`tests/test_failure_threshold.py`
（`test_threshold_triggered_at_boundary` 验证 `0.5` 边界即触发、
`test_below_threshold_keeps_release_running` 验证 `0.25 < 0.5` 不触发、
`test_zero_threshold_pauses_on_first_failure`、
`test_threshold_does_not_fire_on_inflight_or_pending`）。

---

## HTTP 接口速览

| 方法与路径 | 说明 |
| --- | --- |
| `POST /devices/{id}/register?group=&firmware_version=` | 注册设备 |
| `POST /devices/{id}/heartbeat?ts=` | 心跳上报（ts 可空） |
| `GET /devices/{id}` / `GET /devices` | 查询设备在线状态与升级状态 |
| `GET /devices/{id}/history` | 查询设备参与过的历次发布与每轮结果 |
| `POST /firmwares/{id}?version=&min_device_version=` | 登记固件 |
| `POST /releases/{id}?firmware_id=&target_group=&batch_size=&failure_threshold=` | 创建发布 |
| `POST /releases/{id}/advance` | 灰度推进一个批次（幂等） |
| `POST /releases/{id}/pause` / `resume` / `rollback` | 暂停 / 继续 / 回滚 |
| `GET /releases/{id}` / `GET /releases` | 查询发布与批次统计 |
| `POST /devices/{id}/report?stage=&failure_reason=&release_id=` | 上报升级结果（`release_id` 可空；携带时可区分旧一轮迟到的上报） |

`stage` 取值：`download` / `verify` / `install` / `reboot` / `success` / `fail`。

### 最小调用示例

```bash
curl -X POST 'http://127.0.0.1:8000/devices/d0/register?group=g&firmware_version=1.0.0'
curl -X POST 'http://127.0.0.1:8000/devices/d0/heartbeat'
curl -X POST 'http://127.0.0.1:8000/firmwares/fw?version=2.0.0&min_device_version=1.0.0'
curl -X POST 'http://127.0.0.1:8000/releases/r1?firmware_id=fw&target_group=g&batch_size=2&failure_threshold=0.5'
curl -X POST 'http://127.0.0.1:8000/releases/r1/advance'
curl -X POST 'http://127.0.0.1:8000/devices/d0/report?stage=success'
curl 'http://127.0.0.1:8000/releases/r1'
# 发布完成后同一批设备可直接进入下一轮；可随时回看设备历次升级
curl 'http://127.0.0.1:8000/devices/d0/history'
```

### 本地验证连续升级行为

```bash
# 跑全部单测（日志会打印每次判定的输入与依据，便于复现）
python -m unittest discover -s tests -v

# 只跑连续升级 / 跨发布复用场景
python -m unittest tests.test_continuous_rollout -v
```

> 说明：本服务为单机进程内模拟，设备、发布数据保存在内存中，重启即清空；
> 其目标是把 OTA 的判定语义、异常分支与并发不变式做到可解释、可复现，
> 而非提供持久化或跨进程能力。

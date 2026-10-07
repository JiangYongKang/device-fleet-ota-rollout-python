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
    `skipped_device_ids`，**绝不重复分配**；
  - 跳过不再是“笼统一句”：发布视图的 `skipped_devices` 给出每台设备当下的
    占用方 `occupied_by_release_id`、占用方状态 `occupier_state`、
    占用方目标版本 `occupier_target_version`，以及 `still_blocked`
    （当前是否仍被占）。占用方收尾后再查询，该设备 `still_blocked=false`、
    `occupied_by_release_id=null`，可明确看出“挡住它的发布已经结束”；
  - 批次内设备全部被占用时该批次立即收口，后续批次仍可继续推进。
- 并发安全由同一把锁保证：多线程同时 `advance`（即使是多条不同发布同时推进），
  每台设备也只会被其中一条发布线占用、只产生一条升级记录，不重复分配、不漏设备。

### 设备在发布之间的占用与释放（连续运营的关键规则）

设备是否“可被新一轮发布分配”，只看它当下是否被一条**进行中**的发布占用：

- **进行中**的发布状态为 `pending / in_progress / paused / auto_paused`，
  这些发布占着设备，其它发布推进到该设备时只能跳过并记录占用方；
- 发布一旦**收尾**——无论是正常走到 `completed`（所有批次设备到达终态）
  还是被 `rollback` 置为 `rolled_back`——都会**解除其对设备的占用**，
  设备重新变为可分配状态。因此同一批设备可以一轮接一轮地升级
  （先推 v2，稳定后再推 v3），中间也可以穿插回滚与重试；
- 收尾释放只清除“确实指向本发布”的占用指针，绝不会误清设备后来参与的
  另一条进行中发布；分配路径还会对指向已收尾发布的遗留指针做防御性回收。
- 注意“跳过”的口径：设备在某轮只是被跳过（busy）而**未被真正分配**时，
  该轮不会为它产生升级记录，也不会出现在它的升级历史里；等占用方收尾后，
  应针对这批设备**再建一轮发布**，新发布会把它们真正分进去
  （而不是出现“发布显示结束了，却一台设备都没升级”）。

验证：`tests/test_release_batching.py`、
`tests/test_consecutive_releases.py`
（`test_devices_reassigned_to_new_round_after_completion`、
`test_active_round_blocks_new_round_and_occupier_is_visible`、
`test_concurrent_two_releases_never_double_assign`、
`test_rollback_releases_devices_for_next_round`、
`test_finished_old_release_no_longer_blocks_and_skip_resolves`）。

### 连续升级时的版本基线口径

- 每台设备在**被真正分配进某轮发布的那一刻**，会快照两份版本到该轮
  `RolloutRecord`：
  - `from_version`：当时设备**实际生效**的固件版本（这一轮的版本基线）；
  - `target_version`：该轮目标固件版本（同样快照，固件元数据之后变更或删除
    都不影响历史结论）。
- 因此连续多轮升级各轮基线彼此独立：v1→v2 轮的基线是 `1.0.0`，设备成功升到
  `2.0.0` 后再开 v2→v3 轮，该轮基线就是当时实际生效的 `2.0.0`；
  **上一轮的结果不会串到新一轮**。
- 回滚也按“本轮基线”恢复：v3 轮回滚只把设备恢复到该轮 `from_version=2.0.0`，
  不会错误地退回到更早的 `1.0.0`；v2 轮的成功结论保持不变。

### 升级历史查询

- `GET /devices/{device_id}/history`（服务方法
  `DeviceService.release_history(device_id)`）返回设备参与过的**历次发布**，
  按设备被分配进发布的时间升序（`assigned_at`，平手再按 `release_id`），
  每一项包含：`release_id`、`firmware_id`、`from_version`（该轮基线）、
  `target_version`（该轮目标版本）、`batch_index`、`status`、`last_stage`、
  `failure_reason`、`release_state`、`active`、`assigned_at`、`updated_at`。
- **语义与兼容范围**：
  - 历史只包含设备**真正被分配**过的发布；仅被跳过（busy）的轮次不在其中，
    那部分信息在发布视图的 `skipped_devices` 里可查；
  - 已完成 / 已回滚的轮次 `active=false`，但完整保留最终状态与发布状态，
    可长期回看；设备当前进行中的那一轮 `active=true`（至多一条）；
  - 所有字段来自分配时快照与最终记录，**重复查询结论严格一致**；
    查询接口只读，不改变任何状态；
  - 设备视图 `GET /devices/{id}` 在没有进行中发布时，仍会回看最近一轮记录
    展示其最终状态（如 `succeeded` / `rolled_back`），这是对既有单轮视图的
    兼容行为；跨轮次的完整结论以 history 接口为准。

验证：`tests/test_consecutive_releases.py`
（`test_consecutive_upgrades_use_correct_version_baseline`、
`test_rollback_baseline_restores_per_round_independently`、
`test_release_history_is_complete_stable_and_marks_active_round`、
`test_history_excludes_merely_skipped_round`）。

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

## 3. 升级结果上报与可区分的拒绝原因

设备分阶段上报，阶段严格区分且**单调推进**：

```
download(下载) < verify(校验) < install(安装) < reboot(重启) < success / fail
```

- 同阶段重复上报幂等；阶段回退（如已到 reboot 又报 verify）被拒绝；
- `success` 将设备当前固件版本更新为目标版本，`fail` 记录 `failure_reason`；
- `success` / `fail` 为终态。重复上报**同一终态**始终幂等（即使发布随后被
  暂停 / 完成 / 回滚、或设备已进入下一轮，重传也得到相同结论且不改任何状态）；
  终态后改报其它阶段，则按发布当前状态给出可区分原因。

### 连续多轮升级时的上报归属（防止迟到上报串轮次）

设备端上报时可以携带 `release_id` 查询参数，明确“这是哪一轮”的上报：

- **带 `release_id`**：服务端严格按该轮记录判定与写入，绝不会把上报落到设备
  当前参与的另一条发布线上；
- **不带 `release_id`**：按设备当前进行中的发布（active 指针）路由；设备当前
  没有进行中发布时，回看最近一轮记录并按其最终状态给出结论（向后兼容）。
- **旧一轮的迟到上报**（下载 / 校验 / 安装 / 重启 / 最终结果在该轮收尾后才到）：
  - 重复同一终态 ⇒ 幂等返回，**不改写设备固件版本、不改写新一轮记录**，
    因而不会把已经升到新版本的设备“改回去”；
  - 其它阶段 ⇒ 按旧轮状态给出可区分拒绝：旧轮已完成 → `release_finished`、
    已回滚 → `release_rolling_back`、被暂停 → `release_paused`；
    若上报指向的轮次仍在活动态、但设备当前已属于另一条进行中发布线，则以
    `stale_release_report` 拒绝。迟到上报永远不会写入新一轮的状态。

异常上报**绝不静默接受**，统一返回 HTTP `409` 与可区分的 `error`：

| `RejectReason` | 含义 |
| --- | --- |
| `device_not_found` | 设备不存在 |
| `device_already_registered` | 设备已注册 |
| `release_not_found` / `firmware_not_found` | 发布 / 固件不存在 |
| `device_in_another_active_release` | 设备已属于另一个进行中的发布（推进分配时跳过） |
| `device_not_in_target_group` | 设备未被任何进行中发布分配（不在目标分组 / 未排到批次） |
| `below_min_version` | 设备当前固件版本低于固件要求的最低版本 |
| `release_paused` | 发布被手动或自动暂停 |
| `release_rolling_back` | 发布正在 / 已经回滚 |
| `release_finished` | 发布已完成 |
| `stale_stage` | 上报阶段顺序回退 |
| `stale_release_report` | 旧一轮发布的迟到上报：设备当前已属于另一条进行中发布线（或显式指定了非当前轮次），不写入任何一轮 |
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

## 5. 本地验证设备跨发布复用与连续升级

推荐直接跑单测，日志会逐条打印 `action / inputs / decision`，能看出每次推进、
分配跳过、上报接受或拒绝的**输入与判定依据**，方便复现：

```bash
python -m unittest discover -s tests -v                       # 全部用例
python -m unittest tests.test_consecutive_releases -v         # 仅连续升级场景
```

`tests/test_consecutive_releases.py` 覆盖：

| 用例 | 验证点 |
| --- | --- |
| `test_devices_reassigned_to_new_round_after_completion` | 一轮正常结束后同一批设备再次进入新一轮并被真正分配 |
| `test_active_round_blocks_new_round_and_occupier_is_visible` | 前一轮进行中时新一轮不抢占，且能看出被哪条发布占着 |
| `test_concurrent_two_releases_never_double_assign` | 两条发布并发推进时设备不被双线占用、不重复分配 |
| `test_rollback_releases_devices_for_next_round` | 回滚解除占用，下一轮可继续使用同一批设备 |
| `test_finished_old_release_no_longer_blocks_and_skip_resolves` | 结束后旧发布不再挡设备，跳过明细转为 `still_blocked=false` |
| `test_consecutive_upgrades_use_correct_version_baseline` | 连续两轮 v2→v3 的版本基线取值正确 |
| `test_rollback_baseline_restores_per_round_independently` | v3 轮回滚只回到本轮基线 2.0.0，不串到更早版本 |
| `test_late_report_from_old_*` | 旧一轮迟到上报被区分拒绝 / 幂等，不污染新一轮、不改回版本 |
| `test_release_history_*` | 升级历史完整、可重复查询结论一致、标记当前进行轮 |

也可用 HTTP 手工走一遍两轮升级（先 `OTA_FAKE_CLOCK=1 python main.py` 启动）：

```bash
# 注册设备 + 登记 v2/v3 固件
curl -X POST 'http://127.0.0.1:8000/devices/d0/register?group=g&firmware_version=1.0.0'
curl -X POST 'http://127.0.0.1:8000/firmwares/fw2?version=2.0.0&min_device_version=1.0.0'
curl -X POST 'http://127.0.0.1:8000/firmwares/fw3?version=3.0.0&min_device_version=2.0.0'

# 第一轮 v2：推进并成功收尾
curl -X POST 'http://127.0.0.1:8000/releases/r1?firmware_id=fw2&target_group=g&batch_size=10&failure_threshold=1.0'
curl -X POST 'http://127.0.0.1:8000/releases/r1/advance'
curl -X POST 'http://127.0.0.1:8000/devices/d0/report?stage=success&release_id=r1'
curl 'http://127.0.0.1:8000/releases/r1'        # state=completed，设备已释放

# 第二轮 v3：同一台设备被真正分配，基线为 2.0.0
curl -X POST 'http://127.0.0.1:8000/releases/r2?firmware_id=fw3&target_group=g&batch_size=10&failure_threshold=1.0'
curl -X POST 'http://127.0.0.1:8000/releases/r2/advance'        # assigned=1
curl -X POST 'http://127.0.0.1:8000/devices/d0/report?stage=success&release_id=r2'

# 查看历次发布；旧一轮 r1 的迟到上报被 409 release_finished 区分拒绝
curl 'http://127.0.0.1:8000/devices/d0/history'
curl -i -X POST 'http://127.0.0.1:8000/devices/d0/report?stage=download&release_id=r1'
```

---

## HTTP 接口速览

| 方法与路径 | 说明 |
| --- | --- |
| `POST /devices/{id}/register?group=&firmware_version=` | 注册设备 |
| `POST /devices/{id}/heartbeat?ts=` | 心跳上报（ts 可空） |
| `GET /devices/{id}` / `GET /devices` | 查询设备在线状态与升级状态 |
| `GET /devices/{id}/history` | 查询设备参与过的历次发布（基线/目标版本/最终结果，按轮次升序） |
| `POST /firmwares/{id}?version=&min_device_version=` | 登记固件 |
| `POST /releases/{id}?firmware_id=&target_group=&batch_size=&failure_threshold=` | 创建发布 |
| `POST /releases/{id}/advance` | 灰度推进一个批次（幂等） |
| `POST /releases/{id}/pause` / `resume` / `rollback` | 暂停 / 继续 / 回滚 |
| `GET /releases/{id}` / `GET /releases` | 查询发布与批次统计（含 `skipped_devices` 占用明细） |
| `POST /devices/{id}/report?stage=&failure_reason=&release_id=` | 上报升级结果（`release_id` 可选，用于多轮升级时指定轮次） |

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
```

> 说明：本服务为单机进程内模拟，设备、发布数据保存在内存中，重启即清空；
> 其目标是把 OTA 的判定语义、异常分支与并发不变式做到可解释、可复现，
> 而非提供持久化或跨进程能力。

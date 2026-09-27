# 设备机群固件升级（OTA）服务

进程内本地存储模拟设备与固件数据的 OTA 服务，**不依赖任何外部服务或真实凭据**。
聚焦三件在异常情况下也必须一致、可解释、可复现的事：

1. **在线判定**：按心跳超时判定，重复 / 乱序 / 并发心跳不会把已超时设备误判在线；
2. **灰度发布**：按批次推进，可暂停、继续、回滚；推进幂等，设备不重不漏，
   失败比例达到阈值自动停住并说明触发原因；
3. **升级结果上报与回滚一致性**：阶段可区分，拒绝原因可区分，
   暂停 / 回滚后“设备视图”和“发布记录”结论一致且重复查询不变。

## 目录结构

```
ota/
  clock.py      可注入时钟（SystemClock / FakeClock），测试可复现时间
  models.py     领域模型与状态枚举（纯数据）
  errors.py     可区分的拒绝码 RejectCode
  storage.py    进程内存储：单把可重入锁保护全部共享状态
  service.py    领域服务：在线判定 / 灰度 / 回滚 / 上报（全部业务规则在此）
  api.py        FastAPI HTTP 适配层
tests/          26 个单测（含并发场景），日志打印输入与判定依据
main.py         uvicorn 入口
```

运行测试：

```bash
pytest                                   # 静默摘要
pytest --log-cli-level=INFO              # 打印每个用例的输入与判定依据（复现用）
```

## 一、在线状态判定规则

- 设备注册后在收到首次心跳前**一律离线**。
- 记最近一次被接受的心跳时间为 `last_heartbeat_at`，超时阈值为
  `heartbeat_timeout_seconds`（默认 30s），判定式为严格小于：

  ```
  online  ⇔  now - last_heartbeat_at < timeout
  ```

  **临界相等（恰好 30s）算离线**，避免边界歧义。
- 在线状态是“查询时由当前时钟推导”的，不做后台扫描、不写冗余字段，
  因此同一时刻重复查询结论必然一致。
- 心跳携带的时间戳 `ts` 必须 **不早于** 已记录值：
  - `ts >= last`：接受并推进过期时间；完全相同的重复上报幂等；
  - `ts < last`（乱序 / 重传旧包 / 并发中后到的旧值）：拒绝，
    返回 `STALE_HEARTBEAT`，**绝不用旧数据覆盖过期时间**。
- 所有“比较 + 写入”在同一把锁内完成。并发心跳下无论线程调度如何，
  记录值只会向所有上报中的最大值推进（见
  `test_concurrent_heartbeats_never_go_backwards`）。

## 二、灰度发布的状态流转

```
            create                advance(批次1..n)
 PENDING ───────────► IN_PROGRESS ───────────────► ROLLOUT_COMPLETE
   │                     │  │                           │
   │ pause               │  │ pause                     │ pause
   ▼                     ▼  ▼                           ▼
 PAUSED ◄── auto(阈值) ──┘  PAUSED                  PAUSED
   ▲      resume（手动暂停）  ▲
   └──── rearm（阈值暂停后人工放行）
   │
   │（未分配任何设备直接回滚）
   ▼
 ROLLED_BACK（终态）

 IN_PROGRESS / PAUSED / ROLLOUT_COMPLETE
   └── rollback ──► ROLLING_BACK ── settle ──► ROLLED_BACK（终态）

 ROLLOUT_COMPLETE ──（全部设备出终态）──► COMPLETED（终态）
```

- **PENDING**：已创建，候选设备名单在创建时按目标分组快照
  （之后注册的设备不影响本次发布）。
- **IN_PROGRESS**：至少一个批次已分配。
- **PAUSED**：两种原因可区分——`MANUAL`（手动暂停）与
  `FAILURE_THRESHOLD`（失败比例自动暂停，附带具体比例明细）。
- **ROLLOUT_COMPLETE**：计划内全部批次已发放完，但仍有设备在途；
  设备仍可上报，仍可回滚。
- **COMPLETED**：批次发完 **且** 每台设备都已出终态（成功 / 失败），
  由最后一次终态上报自动收敛。
- **ROLLING_BACK / ROLLED_BACK**：回滚中拒绝新升级结果上报
  （`RELEASE_ROLLING_BACK`）；settle 后释放全部设备占用，终态固定。

### 推进（advance）的并发与幂等保证

- 每次推进必须带 `request_id`：
  - 同一 `request_id` 重复 / 并发调用 → 回放**同一个批次**，绝不二次分配；
  - 不同 `request_id` 并发调用 → 在锁内严格串行，每次推进恰好一个批次，
    **不跳过、不重复**。
- 每台设备同一时间只能属于一个“进行中”发布（`device_locks` 占用表）。
  分配批次时已被其他发布占用的候选设备被跳过（事件中记录 `busy_skipped`），
  本次批次按可用设备分配；设备回滚释放后可进入新发布。
- 暂停 / 回滚中 / 终态下推进返回 `RELEASE_NOT_RUNNING` 等可区分拒绝码。

## 三、失败阈值的含义

- 阈值 `failure_threshold ∈ [0,1]`，在**下一次 advance 分配新设备之前**检查：

  ```
  自上次放行检查点以来新增失败数 / 自上次放行检查点以来新增终态数 ≥ 阈值
  ```

  终态 = 成功（REBOOT 成功）或失败（任一阶段失败）；尚在途的设备不进分母，
  避免少量早期失败被误放大。
- 命中后发布自动转 `PAUSED`，`pause_reason=FAILURE_THRESHOLD`，
  `pause_detail` 形如：

  ```
  failure ratio since last checkpoint 3/4=0.750 reached threshold 0.500
  (cumulative 3/4)
  ```

- 检查点在每次成功放行一个批次时建立（基线为放行前累计值）；
  **自动暂停不能用 resume 绕过**，必须显式调用 `rearm`（人工确认），
  rearm 以当前累计值重置检查点后才能继续推进，避免设备继续失败时被悄悄放开。
- 验证方式见：
  - `test_failure_threshold_auto_pauses`（1/2 命中 0.5，明细可复现）；
  - `test_concurrent_reports_failures_trigger_threshold_once`
    （4 台并发上报 3 失败 1 成功，下一次推进必然自动暂停且只暂停一次，
    新批次绝不分配）。

## 四、升级结果上报

阶段固定按顺序推进：`DOWNLOAD → VERIFY → INSTALL → REBOOT`。

- 每个阶段可报成功 / 失败；任一阶段失败 → 分配记录终态 `FAILED`
  并精确记录 `failed_stage`；REBOOT 成功 → `SUCCEEDED` 且设备版本
  更新为目标固件版本。
- 跳阶段、回退报更早阶段、把已成功阶段改报失败 → `STAGE_OUT_OF_ORDER`；
  重复报同阶段成功幂等回放；终态后再上报回放相同结论（`ALREADY_TERMINAL`
  语义由幂等回放承担，状态不被污染）。
- 拒绝原因严格区分、绝不静默接受：

  | 拒绝码 | 含义 |
  |---|---|
  | `DEVICE_NOT_FOUND` / `RELEASE_NOT_FOUND` | 设备或发布不存在 |
  | `NOT_IN_TARGET_GROUP` | 设备不在创建时快照的目标名单 |
  | `NO_ACTIVE_ASSIGNMENT` | 是候选但尚未被分配到任何批次 |
  | `VERSION_BELOW_MINIMUM` | 当前版本低于固件最低设备版本要求 |
  | `RELEASE_PAUSED` | 发布暂停中（含暂停原因） |
  | `RELEASE_ROLLING_BACK` | 发布回滚中 |
  | `RELEASE_FINISHED` | 发布已处于 ROLLED_BACK / COMPLETED 终态 |
  | `STAGE_OUT_OF_ORDER` | 阶段乱序 / 跳阶段 |
  | `STALE_HEARTBEAT` | 心跳时间戳比已记录值更旧 |

## 五、一致性说明

所有“检查 + 写入”在 `InMemoryStore` 的同一把 `RLock` 内原子完成。

- **设备视图**（`get_device_view`）完全由当前存储状态 + 当前时钟推导：
  在线、当前固件版本、归属发布、分配状态 / 阶段。
- 回滚 settle 后：发布终态固定为 `ROLLED_BACK`，事件流追加 `ROLLED_BACK`，
  设备 `active_release_id` 全部变为 `None`，重复查询结果相同
  （`test_rollback_state_consistency_between_view_and_release`）。
- 时间通过可注入时钟控制，测试使用 `FakeClock`，任何时序结论均可复现。

## 六、HTTP 接口速览

| 方法与路径 | 说明 |
|---|---|
| `POST /devices` | 注册设备（重复注册幂等，不改动已有数据） |
| `POST /devices/{id}/heartbeat` | 心跳上报（body 可带 `ts`） |
| `GET  /devices/{id}` | 设备视图：在线 / 版本 / 归属发布 / 升级状态 |
| `POST /firmwares` | 创建固件（含最低设备版本） |
| `POST /releases` | 创建灰度发布（批次大小列表、目标分组、失败阈值） |
| `POST /releases/{id}/advance` | 推进下一批次（body 带 `request_id`，幂等） |
| `POST /releases/{id}/pause` / `resume` / `rearm` | 暂停 / 继续 / 阈值暂停后人工放行 |
| `POST /releases/{id}/rollback` / `rollback/settle` | 进入回滚 / 完成回滚 |
| `POST /releases/{id}/reports` | 设备上报阶段结果 |
| `GET  /releases/{id}` | 发布详情：状态、暂停原因与明细、统计、完整事件流 |

启动：`python main.py`（uvicorn，端口 0 自动选择）。
错误响应统一形如 `{"ok": false, "reject_code": "...", "message": "..."}`。

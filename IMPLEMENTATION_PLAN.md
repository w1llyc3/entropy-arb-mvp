# Implementation Plan — Approval Required

基线 `45d1d1f`；本轮只文档。顺序遵循 Specification → Architecture → Rules → Implementation → Test → Monitoring；每个阶段包含测试与监控，不把它们推迟到最后。Reliability 优先，保留已经工作的模块。

## P0：文档与基线（本轮）

交付五份根目录文档、源码证据和离线测试结果。独立 `docs/phase1-reliability-audit` 分支，不动业务代码/main。完成后暂停，由用户确认范围、恢复/补偿权限后继续。记录已存在的 NameError、Windows 轮转失败与测试可移植性，不未经验证修复。

## P1：规则、契约与离线故障设施

拟新增 `entropy_arb/models.py`（operation/leg/query status）、`tests/fakes/venues.py`（接受但丢响应、eventual consistency、重复回报、partial fill）、`tests/test_execution_contract.py`。保留现有测试并拆除依赖 Unix symlink/process signal 的假设。

先给 A01 默认 executor 路由写不读取 .env 的 mock 集成用例；确认 actual route 的 NameError 被覆盖，再在实现分支局部修 scope。这不代表临时 executor 可以 live 启用。Windows rotation 在独立小改动关闭旧 header handle 后轮转，补 trades CSV 同类边界。

交易查询协议与 ID 范围/唯一性先确认。SDK 固定版本/commit、建立脱敏 fixture；只读接口与任何真实交易验证分开。CI secret-free fake venue，无实盘 SDK也能跑核心测试。未确认 client ID recovery 的 venue 不开放 live。

## P2：SQLite 与持久化状态机

拟新增 `entropy_arb/storage.py`、`entropy_arb/execution.py`、schema migrations（只含结构，无账户凭据）。实现 commands unique key/hash、proposal 原子消费、operations/legs/fills/events、version CAS、risk reservation。

先通过事务失败/磁盘满/重复 key/乱序 fill/并发消费/进程终止测试，再让 fake executor 接入；无 DB commit 无网络发送。网络/DB 非原子窗口保持 UNKNOWN。数据库锁等失败有界等待，超过界限 fail-closed。

监控：DB 可写、queue/unknown 数与 age、state transition count、duplicate commands。退出阶段条件：A02/A06 对应所有离线 crash/concurrency 用例通过。

## P3：持久化服务与统一风险/恢复

拟新增 `entropy_arb/service.py`、`entropy_arb/risk.py`、`entropy_arb/recovery.py`、`entropy_arb/process_control.py`。

保留 `engine.py` 信号/库存/盘口规划，抽离真实发送与 local JSON 状态；venue 对象、signer/nonce、锁、budget 由服务长期管理。所有 manual/auto/CLI/flatten 命令排队到同一 execution 路径。策略不直接 send_taker。

`venue_hl.py/venue_lighter.py` 局部加稳定外部 client ID、结果分类、order/fill query、fee correlation；保留已有 load_market/signing/price rounding/HTTP。`book.py/feeds.py/funding.py` 补独立 book age、connection age、monotonic timers、funding fetch timestamp/周期。最终量化后重新验证 cap、depth、edge；减少订单不向上补大。

恢复为先查询后 READY，不调用现有 CLI 自动 hedge 去清外部仓位。PAUSED 保持 confirmation/recovery，Unknown 关闭开仓；补偿独立持久化 reduce-only，需要用户批准政策。Windows 使用 process handle/identity 与合作式命令，禁止 kill(pid,0) 探测。

退出阶段条件：原有仓位 baseline、双未知、本次净差与总账户净差、乱序/滞后快照、二进程抢锁、所有 crash 窗口通过 fake tests。

## P4：保留前端、薄 API 与兼容入口

修改 `web/panel.py` 为 commands/status 适配；GET 仅只读。`web/session.py` 保留 DTO/提示，移除交易 state owner；`web/live_exec.py` 转为 service 命令客户端，不建 Engine；`web/accounts.py` 显示 service 查询投影；`web/recorder_ctl.py` 控制 service 的记录状态而非 signal 冻结。

修改 `main.py/web/__main__.py/config.py`：默认 RECORD_ONLY；live 要显式参数/动作+完整 recovery；legacy CLI 不能独立成为第二发送者。现有 auto_confirm opt-in 功能由服务 scheduler 驱动，不依赖 status GET；close/flatten 人工确认接口原样保留语义，补 operation/idempotency key。stop 不清仓。

`web/index.html` 只增 operation ID、UNKNOWN/RECOVERING/真实对账结果与重试幂等字段，不做新前端。`basis.py/dashboard.py/tools/analyze.py` 保留研究展示能力；数据不足与 funding 非同周期要标明。

退出阶段条件：浏览器关闭不影响已武装服务，GET 连续轮询零订单副作用，API 超时重复调用不二次发单，旧 record-only 与分析测试保持兼容。

## P5：迁移、验收与 PR

新增迁移导入工具与测试：停止旧发送入口（操作者执行，不自动清仓）、备份 `.web`/CSV/SQLite、创建新 DB、仅导入 strategy 配置。live_armed 不迁移为 live；历史 risk/entry/queue 作为 provenance，不作为成交证明。旧订单缺稳定 ID → 标记 LEGACY_UNVERIFIED，人工核对和查询，闭锁直到确认。

新服务运行 RECORD_ONLY + recovery dry-run；对账差异展示给用户。保持旧代码/数据备份，回退只能回到 record-only 研究路径；一旦新系统出现 real outstanding，不能自动回退到旧 live。DB schema 向前迁移失败保留备份、HALTED，不降级覆盖。

追加 README/运行手册：secret-free 启动、Windows pause/resume/stop、恢复与未知处置、权限、备份、live 审批。24h fake venue soak 与 Windows/Linux CI，审查异常日志脱敏。PR 按 P1–P5 小步提交，每个 PR 有范围、验证、未验证事项；发布/启用 live 再单独授权。

## 测试与评审顺序

1. 纯规则、schema、精度与状态转换。
2. 适配器 submit/query fixture contract（不真实下单）。
3. durable intent、幂等、多线程/多进程、fill 去重。
4. 所有 network/DB/process crash 窗口。
5. recovery + existing external positions + delayed snapshots。
6. API/manual/auto/flatten 统一执行与只读 GET。
7. Windows 实际 disposable fake service 生命周期与 PID 复用。
8. 24h fake venue soak、监控与告警、PR 独立审查。

合并不能只依赖旧测试全绿；ACCEPTANCE_CRITERIA.md 每项需映射到测试、报告与审查证据。所有未来 PR 的源代码变更都必须在用户确认 Implementation 后进行。

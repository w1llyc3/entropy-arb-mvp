# Implementation Plan — Approval Required

## P1 实施记录（2026-10-13）

用户已批准 P1；分支 `impl/p1-reliability`。原审计基线与未来 P2–P5 设计仍保留，下面的实现不是完整可靠交易系统。

- 新增 `tests/test_p1.py`：默认 executor 的 open/close/不对称 reduce 路由与异常清理完全模拟；Windows 无信号存活检测、创建时间身份/PID 复用拒绝、合作式 pause/resume/stop、RECORD_ONLY 不读 dotenv/继承密钥、默认网页 executor 不访问凭据。四个初始回归先 RED，再修复。
- 修复 `web/live_exec._route` 的局部变量 scope；默认 executor 在读取任何凭据前拒绝真实发送。`allow_legacy_live=True` 仅供隔离 fake 测试显式调用，无 HTTP 参数或环境开关连接它；持久化恢复验收前不开放实际 legacy 下单。
- CLI/Engine 默认 RECORD_ONLY；CLI `--live` 在 P1 拒绝。config 的 env_file=None 不读 .env，也不读取继承环境中的密钥。原交易代码保留，不删除功能；旧实盘命令已加 README 顶部提示。
- Windows 进程存活用 OpenProcess/WaitForSingleObject，关闭 handle；创建时间持久化用于拒绝 PID 复用。旧元数据缺身份拒绝自动接管。pause 使用 marker 只停采样，WS 继续；stop 先请求控制 loop 正常退出，再必要时终止测试/采集进程。控制目录只在面板 CLI 显式传入；不是 SQLite 或跨请求交易锁。
- recorder 与 trades CSV 轮转移出 header 文件句柄作用域；测试 fixture 不再要求 Windows symlink 权限。暂停/恢复同分钟保留聚合，避免重复 minute。
- `execution_contract.py` 定义稳定 ID intent、可信报告与 Unknown 分类；`tests/fakes/venues.py` 模拟 accepted-but-ACK-lost、延迟查询、IOC partial、重复回报、reduce-only 与 reject。fake 状态只在测试内存中，不是进程崩溃持久化，也不证明真实交易所支持 ID 去重。

测试环境 Windows / Python 3.12.10 / 隔离 `.venv-audit`，无实盘 SDK。

```powershell
.\.venv-audit\Scripts\python.exe -m pytest tests --basetemp=.venv-audit/p1-final -q --tb=short
```

结果：165 passed, 1 skipped（POSIX flock）, 1 warning（现有 Starlette/httpx 弃用提示），16.21 秒。最终暂停同分钟聚合改动后补跑 `tests/test_p1.py tests/test_recorder.py`：19 passed，0.23 秒。此后仅文档/审查记录变化。

未实现：SQLite durable intent、真实适配器规范结果/Unknown 修复、稳定外部订单 ID、交易请求全局幂等、长期单一 trading service 与 crash recovery、权威仓位恢复/资金费周期统一。上述仍属 P2/P3，不能用本轮全绿宣称实盘可靠。当前没有读取真实私钥、下实盘订单或清仓。

PR 审查正文、测试与限制已准备；GitHub 认证/发布结果在最终交付说明。不得合并 main 或启用 live 后再补恢复。

## P2 实施记录（2026-10-13，仅离线执行基础）

分支 `impl/p2-durable-execution` 基于未合并的 P1 分支；P2 PR 使用 `impl/p1-reliability` 为 base，避免重复审查 P1 改动。真实 CLI/Web 入口保持 RECORD_ONLY，无真实账户/SDK/私钥/订单。

- 新增 `entropy_arb/storage.py`：SQLite schema v1、WAL/FULL/foreign keys，commands/operations/legs/fills/events，短事务、稳定客户端 token、proposal 唯一消费、请求 payload hash、风险名义预留、持久化状态与监控计数。数据库锁超时/损坏/未知 schema 拒绝继续，不创建空账本覆盖旧证据。
- 相同请求 key/内容重放得到原 operation；同 key 或同 proposal 内容变更拒绝。同 proposal 的等价别名请求原子关联同一 operation。线程/多进程并发竞争最多一个发送 claim。
- 新增 `entropy_arb/execution.py`：默认不发送，即使传入 fake venue。显式打开模拟发送也只接受标记为 simulation 的注入对象；没有真实适配器连接、HTTP 开关或自动启用路径。
- 两腿发送前先持久化 intent 与 stable IDs，再事务提交两条 SUBMITTING。发送超时/结果落盘失败保留可查询边界；Unknown 不变成 Failed，不创建新 ID 重发。恢复只 query；只有 PREPARED 且未越过提交边界才可证实 NOT_SENT。
- 累积成交不回退；terminal 证据矛盾、重复 trade ID 内容变更或成交总量超限进入持久化恢复闭锁。fills 入库接口唯一去重，但当前 dispatcher 仅处理累积订单报告；真正关联交易所 fill/fee 查询仍属 P3。
- 订单结果明确也只进入 RECONCILING（或开仓已知不等量时 NEEDS_REDUCTION）。不会根据本次 reduce 的 signed fill 推断账户净敞口，不自动补单或清仓。没有开放 SETTLED/预留释放/再次开仓路径：必须由 P3 的权威持仓对账补齐。
- Stable client token 目前是内部 UUID，不声称已适配 Lighter 整数范围或真实交易所去重语义；这些能力需要协议验证和 P3 适配。

测试覆盖新增 30 项：100 个线程请求幂等、50 个 proposal 别名请求、4 个客户端子进程同时消费、重复执行、事务 rollback、模拟磁盘满/结果 commit 失败、DB busy、损坏与旧 schema、partial/unknown/单侧减仓、查询异常、重复成交、矛盾报告。5 类专用子进程 `os._exit` 窗口验证 SQLite 重开：提交事务内、SUBMITTING 后、单腿接受、双腿接受、单腿结果 commit 后。模拟交易所证据独立写入临时文件，不是真实接口或真实系统断电验证。

最终完整回归命令：

```powershell
.\.venv-audit\Scripts\python.exe -m pytest tests --basetemp=.venv-audit/p2-reviewed -q --tb=short
```

结果：195 passed、1 skipped（POSIX flock）、1 既有 Starlette/httpx 弃用 warning。生产 read-only 查询、权威仓位恢复、unique service owner、JSON/API 迁移、真实适配器结果分类、24h soak 与断电测试仍未实现/验证。P2 的离线全绿不能作为 live 启用依据。

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

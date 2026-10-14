# Acceptance Criteria — Evidence and Remaining Gates

## 时区与盘段展示（2026-10-14）

完整 Python 回归 **273 passed、1 skipped、1 既有 warning**；新增 25 项日历/API 测试。Node 时钟测试 **7 passed**，覆盖夏令时与跨日。浏览器已验证双时区切换/记忆、全图同步和实际参考盘段，见 [CHART_TIMEZONES.md](CHART_TIMEZONES.md)。UTC/价格/bp 与执行门槛未改变；展示日历不能作为 P5 或 live 验收证据。

## 多资产研究增量（2026-10-14）

固定十资产的 record-only 增量完整回归为 **248 passed、1 skipped、1 warning（20.54 秒）**，命令 basetemp 为 `.venv-audit/multi-final`。新增 25 项测试，另有真实公开盘口、Windows pause/resume/stop/重启及浏览器图表验证；详见 [MULTI_ASSET_RECORD_ONLY.md](MULTI_ASSET_RECORD_ONLY.md)。新增资产没有执行权限；P5 合并与实盘门槛不因本轮行情/图表通过而解除。

## P3/P4 离线验证（2026-10-14）

本段为实现证据；后续原始 AC 是最终验收目标，不代表全部已通过。默认真实发送仍关闭，具体边界见 [P3_P4_DELIVERY.md](P3_P4_DELIVERY.md)。

- AC01：test_service 的同目录/跨目录账户锁、legacy 元数据拒绝接管、外部净零仓位拒绝接管；test_ownership 在实际 Windows 子进程 `os._exit` 后验证 OS 锁释放；test_service_api 真实默认 factory 禁止凭据读取及 live/flatten。
- AC02–04：保留 P2 的线程/多进程幂等及五个进程终止窗口；新测 20 次并发确认、HTTP 重试、客户端取消、丢 ACK 后只查询同 ID、两次重启保持 NOT_SENT。
- AC05–06：新测部分成交闭锁、人工模拟 reduce-only、外部仓位/挂单、缺逐笔成交、序列倒退、过期快照、缺失/NaN 结算水位均不能解除恢复门槛；保留 P2 的乱序、重复/矛盾成交测试。
- API/调度：真实默认 factory 的 1000 次 GET 零下单副作用；fake 自动开仓再回归平仓全程无浏览器请求；pause 仍对账；重启恢复采集；cleanup 异常释放 owner；非 RTH 首次确认状态经刷新保留。
- 数据：HL 按 client ID/oid/coin/tid 关联 fixture，Lighter symbol→market_id fixture 且明确 incomplete；心跳不掩盖旧盘口。

最终命令：

```powershell
.\.venv-audit\Scripts\python.exe -m pytest tests -q --basetemp=.venv-audit/p34-final-cli --tb=short
```

Windows / Python 3.12.10；无实盘 SDK、真实账户、私钥和订单。POSIX flock 分支在 Windows 跳过，Starlette/httpx 既有弃用提示单独记录。

最终结果：**223 passed、1 skipped、1 warning，20.38 秒**。相对 P2 的 195 passed，新增 28 项 P3/P4 离线测试，包含 CLI 已恢复采集时不重复 start 的回归。代码测试通过后仅补充文档与 PR 信息。

**Requires Runtime Validation**：真实交易所契约/精度/资金费/结算最终性、24h soak、硬件断电、Windows 睡眠唤醒、浏览器人工视觉验收、真实旧账户迁移。以上不得以 fake 或本轮 pytest 替代。

以下保留原验收规格。

基线 `45d1d1f`，Phase 1 本地 Entropy ↔ Lighter SNDK。以下是未来实现验收，不是本轮已通过声明。本轮结果见 CURRENT_STATE_AUDIT.md。

## 全局门槛

所有测试默认 RECORD_ONLY、假账户、假 signer、fake venues；网络守卫禁止真实 exchange 下单，日志/fixture 禁止真实私钥。每项保留 commit、OS/Python/dependency lock、命令、断言与脱敏事件证据。严重项不允许 waiver 或只靠前端操作截图通过。Windows 与 Linux 分开报告，mock Windows 不能替代实际 Windows 验收。

### AC01 默认与单一所有权

不带 live 参数启动、打开网页、读取旧 live_armed JSON、服务重启：signer/send 调用均为 0，状态 RECORD_ONLY/RECOVERING。同账户 SNDK 两服务并发启动，最多一个 sender；另一个拒绝交易。PID 复用/僵尸元数据不得获得所有权或终止无关进程。

### AC02 重复点击与命令幂等

100 个并发请求使用同一 idempotency key/内容，得到同一 operation ID；每腿原始逻辑订单最多一笔。改 payload 同 key → 409 且零新单。不同请求 key 消费同 proposal → 一个 operation，其他返回原结果或冲突，无重复执行。响应丢失后客户端重试、进程重启后重试同样有效。auto/manual/flatten 竞争通过同一风险预留与调度器。

### AC03 持久化早于发送

fake submit 入口断言 operation、leg IDs、SUBMITTING、risk reservation 已提交。intent commit 失败、磁盘满、DB busy 超时：零发送。检测各事务窗口强制终止，恢复后没有新的替代 client ID，也不会漏掉已接受订单。

### AC04 网络超时与 Unknown

交易所接受请求但丢 ACK、返回 5xx、WS 断开、SDK 提交后异常：状态 UNKNOWN，而非 FAILED/零成交已完成。一个/两个腿 unknown、local net=0 均禁止新开仓；查询次数增加，发送次数不因超时重试增加。一次 not_found/过早 REST 空列表不可解除闭锁。查询恢复到实际 filled/canceled/rejected 后才结算。

### AC05 部分成交与双腿不平衡

两腿等量 40% 开仓 → matched 40%，不记为 full；一腿 100%/另一腿 0 或 40% → NEEDS_REDUCTION/闭锁，新 open 零发送。取消带 partial fill 的 IOC 累积量不丢失。
部分 close 即使两腿等量也不能宣称 flat 或清 entry，remaining_qty 与交易所仓位一致。
不对称 reduce-only close 使用账户剩余量，而非本次 buy-sell 差当 final net。禁止为凑最小额增大 close 超过可减量。

### AC06 崩溃与重启恢复

在 EXECUTION_STATE_MACHINE.md 每个 crash window 杀死专用 fake service 子进程，重启先 RECOVERING；所有 durable IDs 不变，unique fills 不重记。两所订单+成交+完整仓位查询确认前零新开仓。查询不可达、保留期不足、矛盾结果 → UNKNOWN/HALTED，不自动重新发原单。API 返回前崩溃仍能凭 key 获取原 operation。

### AC07 真实仓位与外部持仓边界（fake account）

启动时注入已有净零双腿仓位、单腿仓位与手工外部订单，不能用零初始化绕过 gross cap。snapshot 缺失/None/解析错误 ≠ flat。external baseline 不自动清仓；归属不明导致 HALTED。账户净敞口、operation imbalance、worst-case outstanding 预留分别可见。
REST 比 WS 落后且多次重复时不得 phantom hedge；固定时间等待不等于 authoritative settled。fresh完整快照与 fill history 收敛后才能开放。

### AC08 减少风险的补偿

本阶段默认人工确认，不在启动、pause、stop/recovery 中自动清仓。用户确认后的补偿有 child operation、durable ID、reduce-only、qty≤可减少仓位、价格/cap/rate 限制。补偿超时 UNKNOWN 同样锁住新仓；不能盲目补另一条腿。不可减少 dust 显示残余和原因，不无限发送。未来自动补偿必须单独批准并复用全部断言。

### AC09 手动与自动的一致性

手动/auto/CLI/close/flatten 最终均调用同一 service execution；同一仓位、预算、nonce、预留与 DB ledger。切换 manual↔auto 或开多个页面不重复发单。浏览器关闭后 fake 自动策略持续运行；GET /api/status、basis、health 1,000 次均零新增交易意图或发送副作用。风控变化不因 UI 缓存绕过。

### AC10 Windows 生命周期

在实际 Windows 隐藏启动 disposable fake service，状态轮询 1,000 次不终止进程；不用 os.kill(pid,0)。pause 是合作式策略禁用，行情/订单确认/对账仍进行；resume 完成恢复/freshness 检查后开放。停止进入 DRAINING、不清仓、不丢 unknown；反复 stop 幂等。强制 kill 后新进程恢复未完成操作。
Windows/Linux old-header CSV 轮转都成功，旧文件原样保留；无未关闭句柄。测试不要求 symlink 管理员权限。PID 复用用模拟 identity，不向真实不相关 PID 发信号。

### AC11 规模、行情与费用准确性

每腿经过量化/ceil/limit 后重新验证 min/cap/depth/edge，不能超过 gross position cap+在途预留。可成交深度不足直接拒绝，不能靠 walk_depth 不足量计算虚假 notional。
持续心跳但无盘口更新 → book stale；Lighter nonce gap/乱序 → clear/resync，不交易。两边数据时间差受限，时间倒退不延长 persistence。
fees 对应 operation fill/order IDs，旧 SNDK 或别 dex fill 不可匹配。费用缺失为 unknown/pending。资金费成功时间/周期保存，过期或不兼容周期不通过依赖该数据的 auto gate。

### AC12 成交去重与乱序

一个 fill 回报 10 次：累计仓位/费用/volume 只变一次。先 terminal 后迟到 partial 不回退 cum fill；矛盾数量触发 reconcile。更新 DB 中的累计量不等于无条件加量，terminal canceled with fill 保留实际量。关联失败的成交进入隔离/对账队列，不丢失也不猜归属。

### AC13 持久化、迁移与失败闭锁

SQLite foreign keys/unique/CAS/事务在并发下有效；DB 只读、锁、磁盘满、schema migration 失败均禁止发送。恢复 integrity failure 不创建空 DB 覆盖原账本。checkpoint/backup 可恢复同一已知状态，未提交事务不产生半条两腿意图。
legacy JSON 损坏、risk sent=true、entry 存在均不自动映射 SETTLED。新启动不承继 armed 状态；迁移可重复且记录 schema/provenance。历史 CSV 原样保留，不自动覆盖用户文件。

### AC14 监控与持续运行

24h fake soak 包含网络断连、重复/乱序事件、每小时受控重启、浏览器关闭、账户滞后、API 并发。零重复逻辑订单、零 Unknown→Failed 猜测、零恢复前开仓、零外部仓位自动清理。
可见 service heartbeat、readiness、unknown age、残余敞口、最后对账、预算、DB failure 与恢复原因；告警可关联 operation/client ID，日志不含密钥/token/签名 payload。监控不可通过交易 GET 驱动业务。

### AC15 当前模块回归与最终 PR 门槛

保留 existing record-only、分析、中文 UI、RTH/强制确认、人工 flatten、auto opt-in、reduce-only；改变行为通过 API 兼容测试解释。修 A01 的真实默认路由由 mock signer/account/venue 集成覆盖，不只 inject executor。
旧测试失败须按代码缺陷/平台测试问题分别解释；全套 Windows/Linux 通过，新增 crash/concurrency/recovery 严重项通过，所有未验证 venue contracts 列出。PR 提供 problem/behavior/validation/risk，未经用户确认不启用真实资金。

## 完成定义

Phase 1 Implementation 完成仅指上述离线/平台验收及独立 PR 审查达标；不会自动授权真实下单。任何真实接口、SDK、余额模式或资金费契约验证仍标记 Requires Runtime Validation，并单独报告证据与权限范围。

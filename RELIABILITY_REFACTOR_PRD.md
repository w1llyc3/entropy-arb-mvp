# P3/P4 交付状态（2026-10-14）

用户已授权实施至 P4。持久化单 owner service、SQLite v2、共享命令/风险/恢复、薄 API、CLI、后台模拟自动开平仓及现有网页接入已实现。默认 RECORD_ONLY，无真实签名或下单路径。参见 [P3_P4_DELIVERY.md](P3_P4_DELIVERY.md)。

真实适配器恢复完整性与 live 启用条件尚未满足，P5 迁移验收、长期稳定性及 Windows 睡眠/断电验证仍待执行。原 PRD 目标是最终验收要求，不能作为已完成声明。

# Reliability Refactor PRD — Phase 1

状态：待用户确认；基线 `45d1d1f`。本文件描述目标，不描述已实现能力。证据见 CURRENT_STATE_AUDIT.md。

## 目标与边界

让 Entropy io ↔ Lighter mainnet 的 SNDK 本地系统持续运行、持久化交易生命周期，在不确定订单和进程崩溃后可靠恢复。优先级：Reliability > Data Accuracy > Execution Safety > Latency > UI Convenience。

不做多交易所 Scanner、多标的、SaaS、全新前端或极端行情独立策略。不改变策略盈利模型来掩盖执行 bug，不保证收益，不把 record-only 数据当成交回测。保留已有其他 venue 源码但 Phase 1 不扩展其交易服务支持。

本轮仅五份文档；任何业务代码改动都等待确认，使用独立分支、测试、PR 审查后再合并。不会自动清仓已有账户仓位。

## 目标架构

Persistent Trading Service 是唯一交易状态拥有者与发送者。建议先单个本地 Python 进程：asyncio service + Strategy Engine + Risk Engine + Execution State Machine + SQLite，FastAPI 通过命令队列调用 service；UI 是状态投影。若面板分进程，使用 loopback IPC，不另建 Engine。启动所有权锁绑定账户与市场，防止 CLI/Web 两发送者并存。

Strategy 保留固定 band、库存阶梯、RTH、原有自动开/减仓意图。只生成提议；auto 开关只决定是否无需人工承认意图，不绕过风险与持久化。UI 刷新、GET status/basis 必须零交易副作用。浏览器关闭后服务仍可运行。

Risk 检查服务状态、未知订单、已确认仓位与在途预留、净敞口、单腿 gross position cap、规模/深度/手续费/价格保护、账户读数 freshness、持仓读数完整性、订单速率预算、日限额。缩量向上取整后必须再次检查全部约束；不能为了满足 venue minimum 超过 cap。开仓和减仓权限分开，Unknown 时不盲目补对冲腿。

Execution 统一处理手动、auto、CLI、confirmed close 和人工 flatten；所有真实发送只能经过持久化意图。适配器无业务调度职责，接受 service 分配的稳定 ID，返回规范状态和查询证据。

SQLite 记录请求、操作、两腿、成交、账户快照、风险状态、转换事件。选择 WAL、foreign_keys、synchronous=FULL、事务与唯一约束；本地磁盘，非网络共享目录。只有 service 执行状态转换，短事务不跨网络 await。磁盘/事务失败时不发送，无法保存结果则立刻闭锁并恢复。

## 核心规则

1. 默认 RECORD_ONLY。没有显式 live 操作与配置，不加载 signer，不提交订单；重启不从 legacy live_armed 自动武装。
2. 每个外部命令有持久化 idempotency_key 与规范 payload hash。相同 key/内容返回原 operation；不同内容 409。proposal 只能原子消费一次。
3. Intent 与两腿 IDs 事务提交成功后才准发送。网络与 DB 不可能组成原子事务；崩溃于提交窗口必须进入 UNKNOWN，不能声称 exactly-once 网络传送。
4. ID 在重试/查询/重启中保持稳定。未经证实的 venue 去重语义不能当幂等保证；Unknown 先查询，不用新 ID 重发。
5. UNKNOWN 与 FAILED 分开。只有确定未发送的本地验证错误或交易所明确拒单可以判定失败；超时、5xx、断线、提交后异常均按可能接受处理。
6. 服务 RECOVERING/RECONCILING/HALTED/PAUSED/DRAINING 禁止新开仓。恢复必须检查订单、成交、仓位，不能靠 JSON/净零填单推断完成。
7. 账户总净敞口和单个 operation fill imbalance 分开记录。以完整交易所快照和唯一成交 ID 对账；未知量表示 unknown，而非 0。
8. 异常补偿仅针对查询确认后的系统归属净敞口，用 reduce-only、独立持久化子操作、严格 cap/滑点/预算/超时。第一阶段默认人工确认补偿；启用自动风险减仓需要另行批准策略与验收。外部既有仓位不自动清理。
9. pause 停止新策略操作但保持 WS、订单确认、对账和监测；resume 先确认 freshness/recovery gate。stop 进入 DRAINING，再结束；强制终止时留下可恢复状态。不用 SIGSTOP 冻结交易进程。
10. legacy entry/risk/queue 仅为导入证据或展示，不能成为交易完成的真值。审计日志不得含私钥、token、签名 payload。

## 持久化数据契约（拟定）

- commands：key UNIQUE、payload_hash、kind、source、operation_id、result、created_at。
- operations：id、proposal_key UNIQUE、intent(open/close/reduce)、policy/config version、baseline snapshot、state、requested size、risk reservation、created/updated。
- legs：id、operation FK、venue/account/market、side、reduce_only、qty、limit、client_id、venue_order_id、submit state、terminal status、cum fill、version。UNIQUE(venue,account,client_id)。HL cloid/Lighter 整数编码与范围必须先验证。
- fills：venue/account/trade_id 唯一、leg FK、signed qty、price、fee/currency、venue/local time，duplicate 不重复记账。
- account_snapshots：venue/account/market、position、balance、query timestamps、freshness、completeness、source evidence。
- events：operation/leg、前后状态、reason、source、correlation_id、sanitized evidence。
- service_state/migrations：schema version、recovery gate、halt reason、daily totals/ownership metadata。数据库不存私钥。

金额和数量持久化用 decimal 字符串或整数单位，币种与 base/quote 明确；内部浮点纯函数先保留，wire 与风险边界使用精确量化。funding 需 rate、周期、fetch time，不直接相减异周期数值用于风控。

## 功能兼容与迁移

保留 record-only、现有面板、中文展示、历史分析、手动确认、RTH 提示、auto opt-in、reduce-only close、人工 flatten。迁移期间 live fail-closed；legacy CLI live 不能成为绕过 service 的入口。UI 文案可反映 RECOVERING/UNKNOWN，无需换前端。

有既有仓位时记录 external baseline，用户决定是否纳入系统管理。旧 JSON 中标记 sent 的记录也必须查询，不导入为 FILLED。历史 CSV 不覆盖/清空，清洗副本与 provenance 分离；新采集增加 symbol/venue/deployment/schema/collector identity，单写者。

## Monitoring 与交付门槛

显示 service heartbeat、owner、mode、recovery reason、unresolved 数与最老 age、两腿已确认仓位/净敞口/在途风险、数据年龄、最后对账、请求去重数、错单/限频/DB 错误、版本与日计数。健康接口分 liveness 与 trading readiness，不通过 status GET 下单。

24 小时 fake venue soak：浏览器关闭、网络故障、并发请求与重启下，零重复逻辑订单、零未知当失败、零恢复前开仓；失败转换与监控有对应证据。所有 ACCEPTANCE_CRITERIA.md 严重项通过后才讨论 live 试运行。真实 venue 契约验证与交易授权分离。

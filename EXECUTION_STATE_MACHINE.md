# P3/P4 已实现状态机说明（2026-10-14）

当前实现路径：`INTENT_COMMITTED → claim/SUBMITTING → EXECUTING/UNKNOWN/NEEDS_REDUCTION/RECONCILING → SETTLED`。terminal 订单不是交易完成；RecoveryCoordinator 必须关联逐笔成交、发送前账户基线、完整新鲜且未倒退的账户快照、发送后结算水位、无未知挂单及净敞口后，才以 leg version CAS 设置 SETTLED 并释放预留。

PREPARED 且未跨发送边界的恢复操作记 `RECOVERY_ABORT_PROVEN_UNSUBMITTED`，落 NOT_SENT；未来重启不查询未使用 ID。其他 Unknown 只查询，不换 ID 重发。外部仓位不接管。部分成交的已证实系统仓位可人工模拟 reduce-only；结果未知/水位缺失不得减仓推断。

服务经历 BOOTING、RECOVERING/RECONCILING、RECORD_ONLY 或 SIMULATION_READY、PAUSED、EXECUTING、HALTED、DRAINING、STOPPED。只有显式 fake 测试可进入 SIMULATION_READY，生产不能通过 HTTP 参数启用真实发送。完整边界见 [P3_P4_DELIVERY.md](P3_P4_DELIVERY.md)。以下保留原状态机设计。

# Execution State Machine — Proposed

待审批设计，尚未实现。参照 CURRENT_STATE_AUDIT.md 的 A02–A06，Unknown 永不等价 Failed。

## 三层状态

服务：BOOTING → RECOVERING → RECORD_ONLY 或 READY_LIVE。PAUSED 保持查询；RECONCILING 阻止开仓；HALTED 保留原因；DRAINING 不接新单；STOPPED 完成关闭。任何数据库不可写、未知订单、缺失账户快照或持仓冲突撤销 READY_LIVE。重启默认 RECORD_ONLY；live 需重新明确武装并通过恢复门槛。

双腿操作：PROPOSED → INTENT_COMMITTED → EXECUTING → RECONCILING → SETTLED / ABORTED / NEEDS_REDUCTION / HALTED。SETTLED 表示已按查询确认其实际结果与剩余量，可能是等量部分开仓，绝不默认“达到请求量”或“账户空仓”。close 有 remaining_qty，未平完不能清 entry。

腿状态分提交与成交两个维度，避免部分成交和取消冲突：

- submit_state：PREPARED、SUBMITTING、ACKNOWLEDGED、UNKNOWN、REJECTED、NOT_SENT。
- execution_status：UNCONFIRMED、OPEN、PARTIALLY_FILLED、FILLED、CANCELED、EXPIRED；另存 cum_filled/remaining 与 authoritative 标记。
- CANCELED/EXPIRED 允许 cum_filled>0，部分成交已终结的 IOC 不应误判零成交。
- UNKNOWN 保留最后已知累积成交，可在查询后进入任何已证实状态；不能被超时计时器转换为 REJECTED。

## 发送流程与事务

1. API/strategy 提交稳定 command key + proposal key + payload hash。数据库原子检查重复；同 key 返回原 operation，不生成第二笔。内容变动同 key 拒绝。
2. 单个 executor 验证 service gate、fresh books 与账户、risk caps/预留、配置版本、最终量化/滑点后双腿金额与深度。记录 baseline/account snapshot。
3. 一个事务写 operation、两条 PREPARED legs、稳定 client IDs、risk reservation、event，并消费 proposal。事务失败 → 不发送任何腿。
4. service 在事务中把准备发送的腿改为 SUBMITTING，提交后才能调用 network。两腿可并发发送；必须各自有已持久化状态。
5. ACK/filled/canceled/explicit reject 到达后，在事务中保存 venue ID、唯一 fills、状态和事件。请求返回 client operation ID，HTTP 客户端断开不取消执行。
6. 结果不确定 → UNKNOWN + 服务 RECOVERING/RECONCILING。不开新仓、不发替代开仓订单、不释放最大可能风险预留。
7. 查询终结订单、成交、仓位后收敛操作；只有权威确认后释放 reservation。以证据做 monotonic fill 更新，已终结状态被矛盾回报挑战时重新闭锁。

网络发送与 SQLite commit 无法原子化：SUBMITTING 落盘但尚未发送时崩溃，也视为可能已发送。除非可证明从未经过发送边界，否则不能重新创建 ID。稳定 ID 的查询能力是恢复前置条件，不假设交易所支持 exactly-once。

## 双腿结果处理

- 两腿明确拒绝/未发送、零成交：ABORTED，保留原因；查询证明无 outstanding 后释放预留。
- 两腿已终结、等量 full/partial fill：查询账户与成交，开仓记录实际 matched qty；部分平仓记录剩余 qty，不认为 flat。
- 一腿填、另一腿明确未填或两腿不等量：NEEDS_REDUCTION，锁住新开仓。查询总仓位与归属后提出 reduce-only 补偿，而不是把缺失腿补成新仓。
- 任一 UNKNOWN：保持未知量范围，known fill 与 worst-case exposure 同时显示。仅看到 local net=0 不能解除闭锁。
- 两腿 known 零填 canceled：没有仓位变化，不累积虚假成功成交/收益。
- reduce-only 单腿/不对称 close：不能用 buy_fill-sell_fill 当 final account net；基线 + signed fills + 新仓位快照才能确认残余。
- 已查询有外部持仓/未归属订单：HALTED 或人工处置，不擅自清仓。

## 查询适配契约

适配器需支持 submit(precommitted client ID)、query_order(client ID/venue ID)、query_fills(cursor/order)、fetch_positions 与 market metadata。查询返回 found/not_found/unknown、最终性、观察时间、成交累计量、来源；not_found 一次不等于未发送。

HL 保留 cloid orderStatus 查询，补关联 userFills。Lighter 需先验证 client_order_index 的 REST/WS 查询与归属方式；若无法可靠按 ID 恢复，不开放 live，不能用最近成交猜测。venue 不支持去重提交时所有 UNKNOWN 只查不重发。nonce 与 ID 分开，单 owner 管理 signer nonce，不持久化签名秘密。

重复回报按 venue/account/trade_id 去重；乱序 cum fill 不能回退。无 trade ID 时必须定义可验证替代键并标记契约缺口，不能用时间近似静默去重。手续费关联具体 fill，缺失则 pending，不从历史 symbol 成交补一个数字。

## 启动恢复与对账

1. 取得 OS 持有的唯一发送者锁（owner generation）；打开 SQLite、检查 schema/integrity、记录 recovery 开始。无法取得锁的实例只读，不发送。
2. 载入所有 PREPARED/SUBMITTING/ACKNOWLEDGED/UNKNOWN/nonterminal operations、预留与历史 fills。PREPARED 可证明尚未提交到网络；旧版本导入记录无此保证。
3. 公共市场读数就绪；在允许只读账户查询的配置下查询两所订单/成交/真实仓位，不加载发送权限作为查询前提。账户 query 不完整 → 恢复继续闭锁。
4. 对 SUBMITTING/UNKNOWN 查询稳定 IDs，并补 unique fills。订单未找到但查询时段/保留期/最终性不足 → UNKNOWN，不自动超时解除。
5. 对所有订单（包括 terminal）与最新完整仓位做基线、归属、signed fills 对账，考虑 REST eventual consistency。固定 5 秒等待只是节流，不是结算证据。
6. 两所一致、无未解订单、无 unexplained delta、无 external management 冲突 → recovery complete。默认 RECORD_ONLY；显式武装 live 后才 READY_LIVE。
7. 对账失败退避查询并监控告警，保持闭锁；超出操作超时改为 HALTED 待人工，不改变 Unknown 分类。

对账按钮发送服务命令，返回 reconcile job ID 与实际查询结果，不写一个成功的本地请求来冒充完成。PAUSED/HALTED 允许只读对账。

## 补偿、关闭与崩溃窗口

补偿为原 operation 的子操作，先入库、稳定新 ID、reduce_only=True。仅依据已确认且归属清楚的仓位，qty 不超过可减少敞口，不为满足 minimum 上调越过仓位。无法减少 dust 或交易所不可达 → HALTED、明确 residual，人工处置。第一版需人工确认；自动补偿另行授权，不触碰既有外部仓位。

暂停仅停止新开仓与新的策略平仓调度，已发送订单继续确认和对账；经明确授权的风险减仓/人工关闭有独立通道。停止记录 DRAINING，等待在途结果至界限；仍 unknown 时保留原 ID/预留后退出，重启恢复。禁止普通 stop 自动清仓。

故障注入窗口：intent 前、intent commit 后、SUBMITTING commit 后/网络前、首腿接受后、双腿接受后/response 前、收到 fill/DB commit 前、结果 commit 后/API response 前、部分 close 后、补偿提交中、恢复 query 中、checkpoint 中。每个窗口都必须符合 ACCEPTANCE_CRITERIA.md。
# P5 increment: cancellation requests (2026-10-16)

Cancellation state is separate from order state. SQLite v3 persists a stable
request key and target leg before cancel transport. Request states:
`PREPARED → SUBMITTING → ACKNOWLEDGED | UNKNOWN → VERIFIED`.

An acknowledgement never implies CANCELED or zero fills. Query the original
client ID after every cancel attempt, including timeout. A final order may be
FILLED due to a race; VERIFIED means final order evidence was obtained, while
full fill/account reconciliation still controls reservation release. Repeated
keys never resend; conflicting targets are rejected. Restart only queries,
including a crash before transport after the send boundary. No automatic
replacement or cancel retry policy is introduced. This increment is fake-only;
real cancellation remains disabled. See `LIVE_READINESS.md` for release gates.


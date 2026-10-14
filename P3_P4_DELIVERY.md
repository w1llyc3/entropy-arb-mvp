# P3/P4 交付与运行说明

分支：`impl/p3-p4-service`，基于未合并的 `impl/p2-durable-execution`。用户已授权开发至 P4。本文件描述最终实现，原始设计仍保留在五份 Phase 1 文档中。

## 交付范围

**Confirmed by Code**

- 默认入口 `python -m web` 和 `main.py` 使用 `TradingService`，一个长期后台 worker 串行处理命令，OS 文件锁约束同目录及同账户组合的双服务。进程退出后锁由操作系统释放，不以 PID/JSON 判断所有权。
- SQLite v2 向前迁移 v1，保留原订单、稳定客户端 ID、成交与事件；新增任务、提案、权威快照、发送前基线及对账任务。JSON/CSV 仅作历史展示，不能证明成交或解除闭锁。
- 启动先恢复，订单、逐笔成交、账户仓位一致且快照完整、新鲜、无未知挂单、无序列倒退、有发送后结算水位才释放预留。无历史时只允许零仓位基线；现有外部仓位即使净额为零也拒绝接管。
- Unknown 只查询，不盲目重发；不自动补腿或清仓。模拟部分成交出现已证实净差后，仅允许人工确认的系统仓位 reduce-only，仍须逐笔证据与快照匹配。
- 手动与自动确认共用持久化提案、执行器及风险检查。重复 key/同提案别名不会创建新操作。HTTP 调用取消不取消已排队执行。模拟自动开平仓由后台计时驱动，关闭浏览器不影响 worker。
- 风险检查包含恢复闭锁、身份、双账户/盘口新鲜度、深度、审批价格、现金可用额、最小名义、仓位与单笔上限、持久化发送预算及 reduce-only 数量上限。自动模式另检查实际手续费、持久化当日计数/名义、RTH 和资金费观测时间/周期一致性。
- 保留现有网页、CSV、图表与分析工具。默认 API 不创建 ProbeSession 或每次确认新建 Engine；GET 只取状态投影/历史。旧 session/executor/controller 保留为回归测试与迁移参考，默认入口不调用。
- pause 停采样/提案，保留 WS 和对账；resume 恢复；stop 结束采集且不清仓，API 服务仍可查看/对账。应用退出排空命令，再关闭采集与数据库。采集取消时清理子任务。
- 无私钥/签名器接入。生产只有公开账户读取；**真实 submit 仍不存在于新服务路径**。SIMULATION 只支持 Python 测试显式注入 fake 对象，无 HTTP/CLI 启用参数。

## 安全启动

在本仓库目录内运行；不要同时启动 CLI collector 与 web owner。

```powershell
python -m pip install -r requirements.txt -r requirements-web.txt
python -m web
```

访问 `http://127.0.0.1:8765`，新建只记录任务后点击启动。不提供账户标识也能采集公开盘口；账户显示未知，对账保持闭锁，这不是启动失败。

可选只读公开标识（不要填写私钥）：

```powershell
python -m web --hl-account 0xPUBLIC_MASTER_ADDRESS --lighter-account-index PUBLIC_INDEX
```

上面为占位符，必须替换为有效公开地址与整数账户索引。不从 `.env` 或继承环境读取密钥。真实 Lighter 完整订单历史/结算协议尚未验证，因此提供标识也不会开启真实交易就绪。

CLI 单独采集：

```powershell
python main.py --symbol SNDK --hedge lighter --record-only --no-dashboard
```

CLI 控制已运行的 web 服务（不启动第二个 owner）：

```powershell
python main.py --symbol SNDK --hedge lighter --command status
python main.py --symbol SNDK --hedge lighter --command pause
python main.py --symbol SNDK --hedge lighter --command resume
python main.py --symbol SNDK --hedge lighter --command reconcile
python main.py --symbol SNDK --hedge lighter --command stop
```

`/health/live` 表示进程心跳，`/health/ready` 表示可执行模拟交易；生产 RECORD_ONLY 后者返回 503 是预期行为。`/api/status` 包含 recovery_reason、unknown_legs、oldest_unknown_age_sec、对账任务与查询次数。`/api/operations/{id}` 查询持久化双腿状态。

## 迁移与故障处理

1. 人工确认停止旧程序，备份整个 `.web` 和 CSV。若存在 `.web/recorder.pid`，新服务拒绝自动接管；核实旧进程确实停止后人工归档元数据，禁止仅因 PID 不存在而清除交易风险。
2. v1 数据库自动结构升级到 v2；既有 P2 operation 没有账户基线时仍闭锁，不补造基线。未知/损坏/不完整 schema 拒绝覆盖。schema 升级不可直接降级；回退使用离线备份，不能同时运行旧发送器。
3. 旧 JSON 不自动导入真实仓位、成交或 live_armed；任务需重新设置。旧杠杆/保证金、库存阶梯、时段自动中枢选项尚未接入新执行路径，网页已注明；原代码仍保留。
4. 遇到未知订单、外部仓位、缺成交、结算水位缺失时，保留数据库并点击对账；不得用重启、删除 DB 或更换订单 ID 绕过闭锁。没有自动平掉真实账户仓位的流程。
5. 停止应用后备份 SQLite（连同尚未 checkpoint 的 WAL/SHM，或使用 SQLite backup）。不要只复制运行中的主数据库文件。长时间运行的快照保留/归档策略仍需 P5 验证。

## 验证边界

**Requires Runtime Validation**

- 真实 HL/Lighter 公共响应、账户模型、订单/成交分页覆盖、client ID 编码/去重、最终性与结算水位。HL fixture 按官方 info API 结构测试，未访问用户账户：[官方 info endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)。Lighter 的不完整公开响应明确标记 incomplete，不把它当作可交易证据。
- 真实交易精度、最小数量、margin/leverage、手续费等级及资金费周期。模拟策略采用 .0001 数量步长和 .9/0 bps 费用假设，不据此开放实盘；资金费周期未验证即不允许自动模式。
- Windows 真实睡眠/唤醒、机器断电、真实网络断连、浏览器人工视觉验收与 24h soak。本轮 OS 锁和故障测试是临时进程/fake，不能替代上述验收。
- RTH 当前为工作日时间窗口，尚无美股节假日/半日交易日历。

**Suspected Risk**

- 长期累计全量成交/快照的查询成本与磁盘增长需要 soak/监控数据定量评估。
- 真实接口非原子快照、最终性滞后与分页截断可能长期闭锁；不能为了减少闭锁而推断零仓位或假定订单失败。

本轮完成的是 P3/P4 的安全服务骨架、默认生产只读接入及模拟端到端执行；真实可恢复交易和迁移验收仍属待验证工作，不能宣称系统已可实盘。

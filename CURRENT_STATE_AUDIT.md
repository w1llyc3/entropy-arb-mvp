# Current State Audit — Phase 1

审计日期：2026-10-11（Asia/Shanghai）。仅审计与设计，未实施修复。

目标仓库：https://github.com/w1llyc3/entropy-arb-mvp

基线 commit：`45d1d1f6e2ce78bde314d1b3b50a30d66bc3aa7c`。
独立本地分支：`docs/phase1-reliability-audit`。
上游参考：`your-quantguy/entropy-arb`，本地参考 commit `aa0391471f6bf72f78c45801fb8117b7bf7e8c89`。
本轮没有读取真实 .env、私钥或账户，没有安装实盘 SDK，没有发送订单或自动清仓。
此版本比之前下载的 ZIP 新：已加入自动确认、减仓、手动清仓、最小下单量与部分 Windows 补丁。旧结论不能直接当作本次证据。

## 证据分级

- **Confirmed by Code**：可由本基线源码控制流证实。离线复现另行注明，不等于交易所实盘验证。
- **Suspected Risk**：存在危险窗口或契约缺口，后果尚未通过受控故障注入证实。
- **Requires Runtime Validation**：依赖真实协议、SDK、操作系统或账户模式；本轮未验证。

## 当前架构与状态归属

CLI：`main.py → Engine.run/_run_inner`。一个进程长期持有两腿 venue、盘口、position、cash、发送预算、锁、nonce；live 启动先读仓位，启动行情、策略、对账、余额任务。除 `--record-only` 外仍是 live 默认；不是本轮目标的默认 RECORD_ONLY。

Web：`web.panel` 创建 `ProbeSession` 与 `RecorderControl`。采集器是独立 record-only 子进程；网页根据分钟 CSV 和缓存账户读数生成提议。task/queue/risk/entry/daily/flatten 位于 `.web/*.json`，HTTP 处理代码负责读写这些交易状态。

`GET /api/status → overlay → pending_payload → _auto_fire → _route_admitted → execute_admitted → asyncio.run(_route)` 可以触发自动订单。没有独立长期交易调度器；浏览器不轮询时这条自动路径不会运行。Web 后端而非浏览器 JS 保存交易状态，但状态与页面请求耦合。

每个 admitted 请求的设计路径新建 Engine、venue、连接与账户 WS，执行一笔后在 finally 关闭。**当前路由先在未定义变量处失败**，所以不能声称当前默认 executor 已成功发单；以下交易后果是该阻断被修复后仍存在的结构性问题。CLI、Web 自动、Web 手动与 flatten 虽复用部分函数，锁、仓位、预算和 nonce 并不全局共享。

## 已证实问题

### A01 — Web 实盘路由阻断（高）

**Confirmed by Code + 离线复现。** `web/live_exec.py:62–76` 的 `_route` 使用 `order`、`position`，它们只在 `execute_admitted` 的局部作用域定义，未作为参数传入。模拟 missing_live_env 与 load_config 后调用 `_route`，在第 73 行得到 `NameError: name 'order' is not defined`，发生于 Engine/HTTP session 创建前。现有测试多数注入 executor，漏掉这条默认路由。

### A02 — 没有发送前持久化订单意图（严重）

**Confirmed by Code。** `web/session.py:1090–1147` 生成 confirm_id、写日志、清空 pending，之后执行；结果返回才更新 risk/history/entry。没有事务性的操作/两腿/客户端 ID 记录。`engine.py:_execute` 发送后才更新本地 fill 和 CSV。崩溃于清卡后、发送前：提议丢失且不知道是否发送；崩溃于请求提交后、结果保存前：可能有真实订单，却没有可恢复订单账本。

`venue_hl.py:194–203` 与 `venue_lighter.py:302–311` 在适配器内部生成基于毫秒时间的内存 ID，未持久化、未向调用方完整返回。重启不能稳定追查原请求。时间相同的多实例 ID/nonce 冲突后果另列 R02。

### A03 — Unknown 分类与处理不安全（严重）

**Confirmed by Code。** Lighter 的 create_order 异常被映射为零成交、`unresolved=False`（`venue_lighter.py:324–334`）；响应接受后 WS 超时才是 `unresolved=True`（354–361）。不能用网络异常证明交易所未接受。

Engine gather 异常同样被映射为已知 send-failed（`engine.py:529–537`）。HL 对 timeout/5xx 有 cloid 轮询兜底，值得复用；但异常/畸形响应分类不完整，查询兜底 `avg_px=None`，需要补成交查询。

Web `execute_confirmed` 的 halted 只按本次 `buy_fill-sell_fill` 判断（`engine.py:718–741`）；两腿均未知且填零时可以 `ok=False, halted=False, sent=True`。`session._apply_result:1187` 不保存 unresolved，`_route_admitted:1148` 根据 sent 且未 HALT 保存 entry/清除 close entry，并非按查询确认完成。Unknown 不能安全恢复。

### A04 — 局部成交差不是账户净敞口（严重）

**Confirmed by Code。** 两 venue 初始化 position=0。`web/live_exec._route` 不调用 CLI 的启动 `_reconcile_positions`，也不启动长期 reconcile loop。`execute_confirmed` headroom 使用这些零持仓；结果 net_base 是本次 fill 差，不含历史仓位。Web 会读缓存账户并做部分资金/净敞口闸门，但未将其原子同步到 Engine。缺失账户读数时 `_account_net` 返回 None，手动闸门并非统一 fail-closed。净零的已有双腿仓位不等于仍有加仓空间。

### A05 — 对账按钮没有交易所恢复（高）

**Confirmed by Code。** `web/session.py:551–572` 只写 reconcile.json 并返回 routed=False。CLI 重启确实读取真实仓位；但没有恢复订单意图、未终结订单、成交明细和幂等请求。CLI 后续自动 reduce-only 对冲与 Web confirmed 的不自动 hedge 语义不同。不能把 CLI 仓位读取等同完整 crash recovery。

### A06 — 缺少跨请求原子性与单一执行者（严重）

**Confirmed by Code（缺少保护）；重复真实订单后果属于 Suspected Risk。** FastAPI sync handlers 可以并行执行。ProbeSession 的 JSON read-modify-write、固定 `.tmp` 文件与 confirm 卡消费没有交易锁/事务/唯一键。每次 confirm 生成新 UUID，不能用它识别同一请求的重试。先清 pending 只覆盖顺序重复点击，不覆盖两请求同时读到 pending。RecorderControl 的文件锁仅保护采集器，不保护交易。flatten 的 `_flatten_busy` 和 auto 的内存冷却不是跨进程保护。

### A07 — Windows 生命周期仍不可靠（严重/高）

**Confirmed by Code。** `recorder_ctl.py:171–182` 用 `os.kill(pid,0)` 检测存活；Python 3.12 Windows 将非 CTRL_C/CTRL_BREAK 的 sig 传给 TerminateProcess，零信号也不是 POSIX 存活探测。状态查询有终止进程风险。没有对真实进程执行此危险探测。

官方依据：[Python 3.12 os.kill](https://docs.python.org/3.12/library/os.html#os.kill)。

`pause/resume:392–415` 仍访问 Windows 没有的 SIGSTOP/SIGCONT。`_matches` 在无 /proc 时只检测 PID，不能排除 PID 复用。已有 `_signal`、taskkill、spawn flags、CLI signal fallback 补丁是部分改善，不是完整 Windows 验收。

**本机测试确认**：`recorder.py:317–321` 在仍打开 fh0 时 os.replace，`test_old_header_rotates` 实际失败 WinError 32。同类 trades CSV 轮转也须检查，不能宣称已复现。

### A08 — 行情/资金费数据新鲜度与规模约束不完整（高）

**Confirmed by Code。** book.is_fresh 仅检查 alive_ts；feeds 任意帧刷新 alive_ts，心跳不能证明盘口更新。FundingPoller 失败保留上次值，没有成功时间/有效期，不能把分钟行时间当 funding 更新时间。

`engine._size_legs:455–494` 会 ceil qty、用放宽后的 limit 计算 notional，未重新校验新 qty 对应的深度/edge/headroom；plan_arb 的 cap 用买一估算。是否实际超上限/亏损需要故障与边界测试。`recent_fill_fee_bps` 按 symbol 子串找近期成交，没有关联该 operation 的订单 ID，可能取旧订单或别的 dex 成交。

## 十个问题的回答

1. 每次确认重新建 Engine？设计控制流是；当前先被 A01 阻断。
2. 请求后崩溃留下什么？可能只有 route 日志/已清卡 JSON；没有完整可追查意图与两腿 IDs。
3. 超时/部分成交/未知？HL 短期查询，Lighter WS 等待；known fill 会更新本地量。CLI 净差对冲；Web pair 净差 HALT、不自动 hedge；不对称 flatten 的短填会 HALT。双腿等量部分平仓也可能被 Web 当作已结束，不能证明账户 flat。
4. 重复按钮能产生两笔？无全局幂等保护已证实；并发真实下单尚未验证，且 A01 当前阻断默认路由。
5. 重启恢复真实仓位？CLI 启动可查询；Web 展示账户但不恢复 execution ledger，JSON 缺失/损坏退回空或默认状态。
6. 自动与手动共享正确状态？Web 共享 ProbeSession JSON/缓存，不共享持久化执行状态与长期 Engine；CLI 另有状态。GET 触发自动交易。
7. Windows 可靠？否，有代码明确缺口和轮转测试失败。
8. 测试覆盖？见下节，mock 行为覆盖不等于真实路由/故障恢复覆盖。
9. 重复订单/错误净敞口？A02/A03/A04/A06 是根因；具体账户后果待受控验证。
10. 可无修改保留？展示/分析与若干纯函数可以先保留；整个交易核心不能无修改作为可靠执行者。

## 原有测试：实际运行与覆盖

环境：Windows，Python 3.12.10，隔离 `.venv-audit`；只安装 requirements-web.txt 与 pytest，没有实盘 SDK。依赖使用现有未固定的下限，不等于作者历史环境。

命令：

```powershell
.\.venv-audit\Scripts\python.exe -m pytest tests --ignore=tests/test_watchdog.py --basetemp=.venv-audit/test-tmp -q -k "not start_stop_cleans_up_the_subprocess and not http_start_normalizes_inputs_and_stop_cleans_up and not pause_resume_and_reconcile_do_not_route and not signal_permission_error_falls_back_to_os_kill and not signal_attribute_error_falls_back_to_os_kill"
```

结果：**129 passed, 3 failed, 1 skipped, 5 deselected**，23.08 秒。
失败：recorder old-header rotation 是代码 Windows 文件句柄问题；两个 analyze Web 测试是 os.symlink 缺少 Windows 权限（WinError 1314），属于测试可移植性问题，不应直接归为策略失败。
跳过：POSIX flock。排除：3 个实际进程控制用例（危险 os.kill(pid,0)/Unix pause），2 个 POSIX mock 不适用于此宿主；watchdog 文件本轮仅源码审阅，未纳入最终测试命令。最初沙箱运行因临时目录权限报错并中止，不用于产品通过率判断。

- test_book/config/engine：盘口、fee-aware signal、库存梯度、扫描持仓 cap、最小金额、mock 双腿单腿失败、reduce-only、flatten。没有默认 live_exec 的正向集成测试。
- test_probe/automation：确认字段、模式、RTH/强制确认、模拟费用/账户、自动闸门、时段切换、减仓、flatten。executor 大多替换成返回固定结果的函数；没有并发 barrier 重复请求测试。
- test_recorder/funding/analyze：分钟聚合、薄深度、schema、费用/返佣分离、资金费解析、时段与覆盖率、半衰期；不能证明真实成交与资金费周期已统一。
- test_web/main/basis：HTTP 与 loopback、CSV/status/chart、CLI signal fallback、Windows stop/spawn mock；未覆盖 Windows pause/resume 的完整实际生命周期。
- test_watchdog：锁、端口/HTTP 与子进程判定；只保障面板启动层，不能保障唯一交易发送者。

缺口：网络接受后丢响应、重复/乱序成交回报、send crash 每个窗口、SQLite durable intent、重启订单查询、账户未知 fail-closed、自动/手动并发、默认 executor、全账户净敞口、浏览器关闭后运行、磁盘满/DB 锁与损坏。

## 可保留能力与修改边界

先原样保留：`web/index.html` 的布局/图表；`web/basis.py` 的历史展示；`tools/analyze.py` 的离线研究入口；`dashboard.py` 展示；book 中 floor_step/truncate_size/crossable_base 等纯函数；config 既有校验和已工作的 recorder 聚合。保留并不证明盈利或已通过生产验收，调用边界与测试仍需补充。

局部修改：book freshness/规模终检、feeds 序列校验/观测、funding 时间与周期、recorder 轮转/单写者、config RECORD_ONLY 默认；venue 保留签名/市场/HTTP 功能，补外部稳定 ID、规范结果、订单和成交查询。

重点重构：web/live_exec 临时 Engine；web/session 的执行与 JSON 真值；engine 的发送/恢复职责；recorder_ctl 的 Windows 生命周期。不删除确认、自动开平、flatten、RTH、分析或界面功能；迁移期间 live 默认关闭。

## 待验证清单

- R01 **Suspected Risk**：并发 confirm/auto/flatten 会重复发送、覆盖 risk/history 或冲突 `.tmp`，需 mock barrier 和多进程测试。
- R02 **Suspected Risk**：多个短命 venue 的时间 ID/nonce 冲突、发送预算重置；需模拟时钟及 SDK 契约测试。
- R03 **Suspected Risk**：ceil size 突破 cap/edge、残留 partial close 被清 entry；需极端小深度和既有仓位测试。
- V01 **Requires Runtime Validation**：HL orderStatus origSz/sz 对 filled/canceled 的语义，填单唯一 ID/费用字段；不凭字段差猜成交。
- V02 **Requires Runtime Validation**：Lighter client_order_index 查询能力、唯一范围、最终结算、重复提交语义、REST 滞后与 nonce；未知时不允许重发。
- V03 **Requires Runtime Validation**：空仓账户 payload、unified USDC 的可用保证金含义、isolated 模式；position=None 不可当 0。
- V04 **Requires Runtime Validation**：真实 SNDK funding 周期与新鲜度、stock session/holiday 行为；不扩大策略范围。
- V05 **Requires Runtime Validation**：Windows 隐藏启动、PID 身份、合作式停止、断电恢复；仅 disposable fake venue 环境验证。

结论：保留现有能力，围绕唯一长期执行者、发送前持久化与恢复闭锁进行渐进重构。当前未修复任何问题，等待五份设计文档批准后才能实施。

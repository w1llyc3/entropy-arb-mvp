# 多资产 record-only 研究阶段

2026-10-14；分支 `codex/multi-asset-record-only`，基于 P3/P4 Draft PR #24。用户将原 Phase 1 单标的范围扩大到**固定清单的行情记录与研究图表**；交易服务仍只识别 SNDK Entropy/Lighter，真实发送仍关闭。P5 合并与实盘测试须等待采集和数据检查，不自动执行。

## 已接入的资产

- Sandisk：io:SNDK ↔ Lighter SNDK，沿用 `logs/minutes.csv`。
- Anthropic：io:ANTH ↔ Lighter ANTHROPIC。
- DRAM：io:DRAM ↔ Lighter DRAM。
- OpenAI：io:OAI ↔ Lighter OPENAI。
- Nebius：io:NBIS ↔ Lighter NBIS。
- EWY：io:EWY ↔ Lighter EWY。
- Xiaomi：io:XIAOMI ↔ Lighter XIAOMI。
- Tencent：io:TCNT ↔ Lighter TENCENT。
- IONQ、GoPro：io:IONQ、io:GPRO。本轮公开目录中 Lighter 和 trade.xyz 没有对应市场；记录 Entropy 单边，不计算 bp。

代码不硬编码 market ID。启动时通过 Entropy `meta(dex=io)` 和 Lighter `orderBooks`，核对唯一、有效、perp 市场及明确的符号映射。模糊/重复/失效/spot 不配对。目录保存在 `logs/record-only/catalog.json`，只包含公开元数据。目录在启动或采集重试时重查，不是全市场 Scanner；新增上市需要重启采集核查。

匹配代码仅证明公开 ticker 对应，**不证明合约经济条款一致**。特别是 OAI/ANTH 的上市前产品、指数构成、估值单位与结算条件仍需逐项核对；较大 bp 不是已证实的套利收益。

## 运行和数据位置

```powershell
python -m web --port 8766
```

访问 `http://127.0.0.1:8766`，启动只记录任务后同时采集全部十个资产。只选择研究资产会切换图表，不改变交易任务或采集清单。CLI 原 SNDK 只记录入口也会附带启动这份研究清单。

- 新增配对资产：`logs/record-only/<Entropy代码>/minutes.csv`。复用原 MinuteRecorder 的 1 Hz 采样、双边新鲜度、1 分钟 OHLC bp、双向盘口边际、$50/$100/$250 深度边际与滑点列。
- 无对冲资产：`logs/record-only/IONQ/entropy-only.csv`、`logs/record-only/GPRO/entropy-only.csv`，仅有 Entropy bid/ask/mid 和样本数。未来有对应市场时配对数据写另一个文件，不覆盖单边历史。
- 新增研究流只导入公开 WS feeds 和 recorder，不导入交易 venue/signing SDK。新流不查询账户、订单或成交，也不获取密钥。新增资产资金费当前留空，不沿用 SNDK 的费率/周期；费前 bp 研究不假设这些数值。
- pause 对全部采样生效，行情连接和原服务对账保持运行；resume 恢复；stop 刷新部分分钟并停止全部采集，不清仓。关闭网页不停止采集。服务进程停止、电脑关机或断网则不能保证继续记录；WS 重连期间不补造数据。
- 本轮已在 Windows 本机真实接收公开盘口并验证 CSV/图表；运行日志为 `.web/research-server.stdout.log` 与 `.web/research-server.stderr.log`（后台启动时）。这些行情与日志均忽略提交，不推送 GitHub。

## 图表和指标

页面保留原任务与风控 UI，新增资产/时间选择和十张同时可见的图。双价格线左轴 USD，bp 右轴，相对零值绿正红负，鼠标主图可看分钟价格/bp。使用真实 UTC 时间；超过 120 秒断档不连线，不插补休市/网络空档。无对冲市场只画一条价格线，hedge/bp 为 null。

`basis_bps = (Entropy mid / Lighter mid - 1) × 10000`，不是两腿往返净收益。报价按 USD/USDC 近似等值，未校正脱锚。两个执行方向分别采用 bid/ask，不用 mid 差冒充可成交价差：

- 卖 Entropy / 买 Lighter：`(Entropy bid / Lighter ask - 1) × 10000`。
- 买 Entropy / 卖 Lighter：`(Lighter bid / Entropy ask - 1) × 10000`。

研究摘要显示分钟收盘 bp min/max、P05/P50/P95、双向分钟平均盘口边际 P50/P95，以及 $100 深度边际中位/有效分钟数。分位数按分钟等权、线性插值，不表示秒级分布。缺深度保持空白。

“观测段采样覆盖”以第一条至最后一条分钟的应有秒数为分母，含中间缺口；“所选窗口覆盖”以完整 24h/72h/7d 为分母。当前分钟可能未写入，停止会保存部分分钟。相同分钟重复写入取最后一条，不跨资产复用成交标记或 SNDK 的任务阈值。数据少时 min/max 和分位数仅描述这段短样本。

只读 API：`GET /api/research/assets`、`GET /api/basis?symbol=ANTH&hours=24`。symbol 只能取固定清单，不能传入文件路径。读取 API 不启动采集或推进策略，`POST /api/start` 仍拒绝新增交易 symbol。

## P5 前的数据检查

本轮几分钟公开行情验证仅证明接线、落盘和显示，**不满足长期验收**。建议先积累至少 24–72 小时，覆盖美股常规时段与非 RTH，再审查：

1. 每个资产的观测跨度、所选窗口覆盖率、断档与新鲜度，区分稀疏盘口和程序异常。
2. bp 区间是否稳定；mid 差、bid/ask 边际与 $100 深度边际是否一致，是否由薄盘口制造。
3. 每个产品的合约条款、单位与结算口径，尤其上市前资产；真实费率和资金费周期仍需另验。
4. 原 P1–P4 测试、真实 Windows pause/resume/stop、重启恢复和长时运行证据；新增研究数据不等于交易可靠性验证。

通过检查后再进入 P5 迁移/合并审查；实盘测试另行明确范围和授权，不因为样本 bp 为正而自动启用。

官方接口说明：[HL info](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)、[Lighter orderBooks](https://apidocs.lighter.xyz/reference/orderbooks)。本地目录快照才是本轮具体上市状态的直接证据。

## 本轮验证记录

- Windows / Python 3.12.10：完整 pytest **248 passed、1 skipped、1 warning，20.54 秒**；新增 25 项多资产测试。skip 为 POSIX flock，warning 为既有 Starlette/httpx 弃用提示。
- 命令：`.\.venv-audit\Scripts\python.exe -m pytest tests -q --basetemp=.venv-audit/multi-final --tb=short`。覆盖别名、重复/停牌/spot 拒绝配对、十资产空图、跨资产隔离、无成交 JSON 标记、坏价/未来时间/零样本过滤、单边无虚假 bp、真实 recorder schema、API 保持单标的执行及采集取消清理。
- 浏览器实测：十张图、NBIS 主图选择、实际公开价格/bp 与覆盖率显示，控制台无 warning/error；页面脚本另经 `node --check` 语法检查。无配对图不显示 bp 轴或零值基差。
- 本机实际 pause → resume → stop → 服务重启 → start 全部返回成功；pause marker 存在时研究流保持运行。无订单发送，账户未配置。
- 首轮约十分钟后：9 个资产各写 10 根分钟；XIAOMI 为 9 根、有效样本较少，页面显示缺口。绝大部分资产约 529 个有效秒样本，XIAOMI 233 个。后台已恢复继续记录；这份短样本不能通过 P5 长时验收。

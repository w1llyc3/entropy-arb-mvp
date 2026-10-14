# 图表时区与美股参考盘段

2026-10-14；分支 `codex/chart-timezones-sessions`，基于多资产研究 PR #25。

- 图表时区可选北京时间（Asia/Shanghai，UTC+8）或美股时间（America/New_York，ET）。默认北京时间；浏览器记住选择，存储受限时仍可正常切换。主图和十张概览同步更新，无需重新采集。
- 横轴为 24 小时制 `月/日 时:分`，副标题标明时区。悬浮显示完整日期、北京和美东两个时间、EDT/EST、对应美股参考盘段。夏令时由浏览器 Intl 和服务端 IANA zoneinfo 处理，不固定减 12/13 小时。
- 图表顶部色条标记盘前、常规交易、盘后、隔夜、周末和节假日。即使选择北京时间，盘段仍按**美东交易日**定义，避免北京跨日导致错判。
- 标准美东参考：04:00–09:30 盘前、09:30–16:00 常规、16:00–20:00 盘后。2026–2028 计划节假日/提前收盘使用已核对的 NYSE 公告；提前收盘日常规在 13:00 结束，盘后按 NYSE Arca 的 17:00 结束口径标注。
- 超出 2026–2028 的数据标“日历未验证”。不覆盖临时休市、个股停牌、其他地区现金市场或上市前合约的专属时段。标签明确为“美股参考（NYSE/Arca）”，不代表 Entropy/Lighter 永续合约开闭市。
- UTC 时间戳、CSV、价格/bp、策略 RTH 判断与执行权限均不改变。新日历仅供展示，不授权自动/真实交易。默认仍 RECORD_ONLY。

来源：[Nasdaq 标准交易时段](https://www.nasdaq.com/market-activity/stock-market-holiday-schedule)、[NYSE/ICE 2026–2028 节假日及提前收盘公告](https://ir.theice.com/press/news-details/2025/NYSE-Group-Announces-2026-2027-and-2028-Holiday-and-Early-Closings-Calendar/default.aspx)。日历是截至本次核对的计划安排，后续变更需更新。

验证：Windows / Python 3.12.10，完整 pytest **273 passed、1 skipped、1 既有 warning（20.06 秒）**；新增 25 项 Python 盘段/API 测试。Node 标准测试 **7 passed**，覆盖截图 06:25 UTC、冬夏开盘、春季跳时、秋季重复小时、跨日与宿主时区独立性。

```powershell
.\.venv-audit\Scripts\python.exe -m pytest tests -q --basetemp=.venv-audit/timezones-reviewed --tb=short
node --test tests/chart_time.test.cjs
```

浏览器人工验收：北京时间/美东切换、十图副标题同步、刷新后保留时区、双时区 tooltip、实际隔夜参考色条，控制台无错误。本机 8766 服务已更新并恢复公开行情采集；未合并 main/P5，未读取私钥或发送真实订单。

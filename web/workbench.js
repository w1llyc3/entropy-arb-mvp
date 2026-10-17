"use strict";
const el = id => document.getElementById(id);
const states = {DRAFT:"规则已保存",OBSERVING:"运行中",PAUSED:"已暂停新开仓",DRAINING:"模拟收尾中",RECOVERY_REQUIRED:"待核验后继续",ENDED:"已结束"};
const labels = {RULES_SAVED:"规则已保存",OBSERVATION_START:"开始观察",OBSERVATION_PAUSE:"暂停观察",OBSERVATION_RESUME:"继续观察",OBSERVATION_END:"结束观察并生成报告",PROCESS_RESTART_OBSERVATION_INTERRUPTED:"进程重启，观察已中断，等待确认",DURATION_REACHED_OBSERVATION_PAUSED:"达到时长，观察已暂停"};
let active = null, busy = false, market = null, report = null, assets = [], marketRequest = 0, reportRequest = 0;
// A failed response keeps its key so retrying an uncertain create is idempotent.
let pending = null;
const zone = () => el("zone").value;
const selectedSymbol = () => el("symbol").value;
const sessionSymbol = session => session.rules.symbol || "SNDK";
const fmt = ts => ts == null ? "—" : ChartTime.format(ts, zone(), true);
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {cache:"no-store"} : {
    method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "请求失败");
  return data;
}
function controls() {
  const state = active?.state;
  const allowed = {start:["DRAFT"],pause:["OBSERVING"],resume:["PAUSED","RECOVERY_REQUIRED"],end:["DRAFT","OBSERVING","PAUSED","RECOVERY_REQUIRED","DRAINING"],"reconcile-paper":["OBSERVING","PAUSED","RECOVERY_REQUIRED","DRAINING"]};
  document.querySelectorAll("[data-action]").forEach(b => b.disabled = busy || (active && sessionSymbol(active) !== selectedSymbol()) || (b.dataset.action === "reconcile-paper" && active?.mode !== "PAPER_SIMULATION") || !allowed[b.dataset.action].includes(state));
  el("create").disabled = busy || !!active || !assets.length;
}
async function loadReport(id) {
  const request=++reportRequest,symbol=selectedSymbol();
  try {
    const result=await api(`/api/sessions/${id}/report`);
    if(request !== reportRequest || symbol !== selectedSymbol() || sessionSymbol(result.session) !== symbol) return;
    report=result;
    el("report-note").textContent = result.status === "PAPER_SIMULATION_REPORT" ? "模拟报告 · 费用和盈亏仅属于虚拟账本，未计真实资金费；不代表真实账户收益。" : "观察报告 · 仓位、成交、费用及结算尚未核验；不代表一次真实交易 Session 已完成。";
    el("report").textContent = JSON.stringify(report,null,2);
    el("download").disabled = false;
  } catch (error) { if(request === reportRequest && symbol === selectedSymbol()) el("error").textContent = error.message; }
}
async function refresh() {
  const data = await api("/api/status"); const previous=active;active = data.active;
  el("state").textContent = active ? states[active.state] : "未运行";
  el("session-id").textContent = active ? `${active.id.slice(0,10)} · ${active.mode === "PAPER_SIMULATION" ? "盘口模拟" : "仅观察"} · v${active.rules_version}` : "可创建下一次 Session";
  el("session-summary").textContent = active ? `${sessionSymbol(active)} · ${active.rules.direction === "BUY_ENTROPY" ? "买 Entropy / 卖 Lighter" : "卖 Entropy / 买 Lighter"} · 开仓 ${active.rules.entry_bps} bps / 平仓 ${active.rules.exit_bps} bps · 单次 $${active.rules.order_usd} · 库存上限 $${active.rules.max_inventory_usd} / 腿 · ${active.rules.duration_minutes} 分钟（含暂停）${sessionSymbol(active) !== selectedSymbol() ? "。当前浏览其他资产；请切回该 Session 标的后操作。" : ""}` : "先保存规则，再开始观察。";
  el("events").replaceChildren();
  for (const event of data.events.slice().reverse()) {
    const row = document.createElement("div");row.className="event";
    const time = document.createElement("time");time.textContent=fmt(event.observed);
    const text = document.createElement("span");text.textContent=labels[event.kind] || event.kind;
    row.append(time,text);el("events").append(row);
  }
  if (!data.events.length) el("events").textContent="暂无活动 Session 事件；可在历史报告中查看。";
  el("history").replaceChildren();
  for (const session of data.sessions.filter(s => sessionSymbol(s) === selectedSymbol())) {
    const button = document.createElement("button");
    button.textContent=`${sessionSymbol(session)} · ${fmt(session.created)} · ${states[session.state]}`;
    button.addEventListener("click",() => loadReport(session.id));el("history").append(button);
  }
  renderPaper(data);
  if(!active && previous?.mode === "PAPER_SIMULATION" && sessionSymbol(previous) === selectedSymbol() && !(report?.session.id === previous.id && report.session.state === "ENDED")) loadReport(previous.id);
  controls();
}
function renderPaper(data) {
  const paper=active && sessionSymbol(active)===selectedSymbol() && active.mode === "PAPER_SIMULATION" ? data.paper : null;
  el("paper-state").textContent=paper ? `模拟状态：${paper.status} · ${paper.reason}` : "所选资产尚无活动盘口模拟";
  el("paper-inventory").textContent=paper ? `虚拟库存 · Entropy ${paper.positions.entropy ?? "未知"} / Lighter ${paper.positions.lighter ?? "未知"} · 持有 ${Math.round(paper.inventory_held_sec)} 秒 · 待确认：${paper.recovery_required ? "是，先查询模拟订单" : "否"}` : "模拟仓位：—";
  const signal=paper?.signal;
  el("paper-signal").textContent=signal ? `开仓净价差 ${Number(signal.entry_net_bps).toFixed(2)} bps · 平仓剩余价差 ${Number(signal.exit_residual_bps).toFixed(2)} bps · 信号时间 ${fmt(signal.observed_at)}（可能已过期，执行前重新检查）` : "盘口信号：待新鲜双边盘口";
  el("paper-pnl").textContent=paper ? `模拟手续费 $${Number(paper.fees_usd).toFixed(4)} · 已平仓净收益（未计资金费）${paper.realized_pnl_before_funding_usd == null ? "持仓或未核验，暂不结算" : "$"+Number(paper.realized_pnl_before_funding_usd).toFixed(4)}` : "模拟费用 / 盈亏：—";
  el("paper-orders").textContent=paper ? paper.orders.slice(-12).map(o=>`${o.venue} · ${o.reduce_only ? "减仓" : "开仓"} · ${o.side ? "买" : "卖"} ${o.qty} · ${o.state} · ${o.id.slice(0,8)}`).join("\n") : "";
  el("paper-distribution").textContent=paper?.fill_edge_distribution.length ? paper.fill_edge_distribution.map(b=>`${b.intent === "reduce" ? "减仓" : "开仓"} ${b.lower_bps} ~ ${b.upper_bps} bps：${b.fill_count} 笔虚拟成交 / $${Number(b.notional_usd).toFixed(2)}`).join("；") : "暂无模拟成交。真实成交尚未接入，绝不混用。";
  const evidence=active && sessionSymbol(active)===selectedSymbol() ? data.real_account_reconciliation : null;
  el("attribution").textContent=evidence ? `${evidence.status} · 归属尚未完成；${evidence.account_position_delta ? "账户数量变化 "+JSON.stringify(evidence.account_position_delta)+"，不自动认领" : "等待本 Session 开始后的双边新鲜基线"}` : "尚无所选资产的 Session 基线";
}
async function command(path, payload) {
  if (busy) return;
  busy = true;controls();el("error").textContent="";
  const signature = JSON.stringify({path,payload});
  if (!pending || pending.signature !== signature) pending={signature,key:crypto.randomUUID()};
  try {
    const result = await api(path,{...payload,key:pending.key});pending=null;
    if (result.state === "ENDED") await loadReport(result.id);
    await refresh();
  } catch (error) { el("error").textContent=error.message; await refresh().catch(() => {}); }
  finally { busy=false;controls(); }
}
el("rules-form").addEventListener("submit",event => {
  event.preventDefault();const rules={symbol:selectedSymbol(),run_mode:el("run_mode").value,strategy:el("strategy").value,direction:el("direction").value};
  for (const name of ["entry_bps","exit_bps","order_usd","max_inventory_usd","max_unhedged_usd","duration_minutes","fee_entropy_bps","fee_lighter_bps","slippage_bps","funding_buffer_bps","max_loss_usd"]) rules[name]=Number(el(name).value);
  command("/api/sessions",{rules});
});
document.querySelectorAll("[data-action]").forEach(button => button.addEventListener("click",() => {
  if (active) command(`/api/sessions/${active.id}/${button.dataset.action}`,{version:active.version});
}));
el("download").addEventListener("click",() => {
  if (!report) return;const url=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:"application/json"}));
  const a=document.createElement("a");a.href=url;a.download=`session-${sessionSymbol(report.session)}-${report.session.id}.json`;a.click();setTimeout(() => URL.revokeObjectURL(url),1000);
});
function draw() {
  const canvas=el("basis"),ctx=canvas.getContext("2d"),width=canvas.clientWidth,height=240,dpr=window.devicePixelRatio || 1;
  canvas.width=width*dpr;canvas.height=height*dpr;ctx.scale(dpr,dpr);ctx.clearRect(0,0,width,height);
  const paired=market?.paired !== false;
  const points=(market?.points || []).filter(p => Number.isFinite(p.entropy_usd) && (!paired || (Number.isFinite(p.hedge_usd) && Number.isFinite(p.basis_bps))));
  ctx.font="11px system-ui";
  if (points.length<2) {ctx.fillStyle="#91a3af";ctx.fillText("尚无足够的有效分钟数据",20,100);return;}
  const prices=points.flatMap(p => paired ? [p.entropy_usd,p.hedge_usd] : [p.entropy_usd]);
  const low=Math.min(...prices),high=Math.max(...prices),pad=Math.max((high-low)*.08,high*.0001);
  const first=points[0].minute_ts,last=points.at(-1).minute_ts,left=58,right=width-52,top=22,bottom=196;
  const x=ts => left+(ts-first)/(last-first || 1)*(right-left);
  const priceY=v => top+(high+pad-v)/(high-low+2*pad)*(bottom-top);
  const bps=points.map(p => p.basis_bps),bLow=paired ? Math.min(0,...bps)-1 : 0,bHigh=paired ? Math.max(0,...bps)+1 : 1;
  const basisY=v => top+(bHigh-v)/(bHigh-bLow)*(bottom-top);
  ctx.fillStyle="#91a3af";ctx.fillText("USD",4,12);
  if(paired){ctx.textAlign="right";ctx.fillText("bps",width-4,12);ctx.textAlign="left";}
  const segments=[];let segment=[];
  for(const point of points){if(segment.length && point.minute_ts-segment.at(-1).minute_ts>120){segments.push(segment);segment=[];}segment.push(point);}if(segment.length)segments.push(segment);
  if(paired){
    // Clip the same polygons at zero so sign changes never borrow the wrong color.
    for(const positive of [true,false]){
      ctx.save();const zero=basisY(0);ctx.beginPath();ctx.rect(left,positive ? top : zero,right-left,positive ? zero-top : bottom-zero);ctx.clip();ctx.fillStyle=positive ? "rgba(99,223,189,.20)" : "rgba(242,128,125,.22)";
      for(const part of segments){ctx.beginPath();ctx.moveTo(x(part[0].minute_ts),zero);for(const p of part)ctx.lineTo(x(p.minute_ts),basisY(p.basis_bps));ctx.lineTo(x(part.at(-1).minute_ts),zero);ctx.closePath();ctx.fill();}ctx.restore();
    }
    ctx.strokeStyle="#526269";ctx.setLineDash([3,4]);ctx.beginPath();ctx.moveTo(left,basisY(0));ctx.lineTo(right,basisY(0));ctx.stroke();ctx.setLineDash([]);
  }
  ctx.strokeStyle="#26333d";ctx.fillStyle="#91a3af";
  for(let i=0;i<=4;i++){const value=low-pad+(high-low+2*pad)*i/4,yy=priceY(value);ctx.beginPath();ctx.moveTo(left,yy);ctx.lineTo(right,yy);ctx.stroke();ctx.fillText(value.toFixed(high<10 ? 3 : 2),2,yy+4);if(paired){ctx.textAlign="right";ctx.fillText((bHigh-(yy-top)/(bottom-top)*(bHigh-bLow)).toFixed(1),width-2,yy+4);ctx.textAlign="left";}}
  for(const [field,color] of (paired ? [["entropy_usd","#6da8fa"],["hedge_usd","#63dfbd"]] : [["entropy_usd","#6da8fa"]])){
    ctx.strokeStyle=color;ctx.lineWidth=1.5;ctx.beginPath();for(const part of segments){ctx.moveTo(x(part[0].minute_ts),priceY(part[0][field]));for(const p of part.slice(1))ctx.lineTo(x(p.minute_ts),priceY(p[field]));}ctx.stroke();
  }
  ctx.fillStyle="#91a3af";ctx.fillText(ChartTime.format(first,zone()),left,225);ctx.textAlign="right";ctx.fillText(ChartTime.format(last,zone()),right,225);ctx.textAlign="left";
}
async function loadMarket() {
  const symbol=selectedSymbol(),request=++marketRequest;
  try {const result=await api(`/api/basis?symbol=${encodeURIComponent(symbol)}`);
    if(request !== marketRequest || symbol !== selectedSymbol()) return;
    market=result;const s=market.summary;
    el("chart-title").textContent=`${symbol} · 最近 24 小时 · ${market.paired ? "价格与原始价差" : "仅 Entropy 价格（USD）"}`;
    el("chart-legend").textContent=market.paired ? "蓝线 Entropy · 绿线 Lighter · 左轴 USD / 右轴 bps · 绿 / 红面积：正 / 负价差" : "蓝线 Entropy · 左轴 USD · 无配对市场，不生成价差";
    el("chart-note").textContent=market.paired ? "（Entropy 中间价 / Lighter 中间价 − 1）× 10000；未扣成本，不能代表可成交净收益。超过两分钟不连线。" : "未取得配对数据，只显示 Entropy 价格；不生成价差或套利收益。超过两分钟不连线。";
    el("basis-summary").textContent=`${symbol} · ${market.paired ? s.basis_count+" 根配对分钟 · P05 / 中位 / P95："+[s.p05_bps,s.p50_bps,s.p95_bps].map(v => v == null ? "—" : v.toFixed(2)).join(" / ")+" bps" : s.minutes+" 根单边分钟"} · 最新 ${fmt(s.last_minute_ts)} · 数据距今 ${s.data_age_sec == null ? "未知" : Math.round(s.data_age_sec)+" 秒"}`;draw();
  } catch(error){if(request !== marketRequest) return;market=null;draw();el("basis-summary").textContent="行情历史读取失败；不会作为执行依据。";}
}
async function loadAssets() {
  try {
    const result=await api("/api/assets");assets=result.assets;
    el("symbol").replaceChildren();
    for(const asset of assets){const option=document.createElement("option");option.value=asset.symbol;option.textContent=`${asset.label} · ${asset.symbol}`;el("symbol").append(option);}
    el("symbol").disabled=false;updateAsset();await Promise.all([loadMarket(),loadInventory()]);controls();
  } catch(error){el("asset-status").textContent="资产目录读取失败；无法创建 Session。";}
}
function updateAsset() {
  const asset=assets.find(a => a.symbol === selectedSymbol());
  el("asset-heading").textContent=`ENTROPY / LIGHTER · ${selectedSymbol()}`;
  const names={PAIRED:"已发现双边市场",ENTROPY_ONLY:"仅 Entropy 市场",UNAVAILABLE:"市场不可用",NOT_CHECKED:"市场待核验"};
  el("asset-status").textContent=`${asset ? names[asset.state] || "市场待核验" : "目录未加载"} · 仅观察；交易权限关闭，合约等价性未核验。`;
}
async function loadInventory() {
  const symbol=selectedSymbol();
  try {
    const data=await api(`/api/inventory?symbol=${encodeURIComponent(symbol)}`);
    if(symbol !== selectedSymbol()) return;
    for(const venue of ["entropy","lighter"]){
      const row=data.legs[venue],name=venue === "entropy" ? "Entropy" : "Lighter";
      el("inventory-"+venue).textContent=`${name} · ${symbol}：${row.status === "FRESH" ? "仓位数量 "+row.position_qty+" · 挂单 "+(row.open_orders_verified ? row.open_order_count : "未核验")+" · 查询于 "+fmt(row.observed_at) : row.configured ? "数据不可用或过期，仓位未知" : "未配置公开账户"}`;
    }
  } catch(error){if(symbol === selectedSymbol()) for(const venue of ["entropy","lighter"]) el("inventory-"+venue).textContent="账户查询状态不可用，仓位未知";}
}
el("symbol").addEventListener("change",() => {
  market=null;report=null;marketRequest++;reportRequest++;draw();updateAsset();controls();
  el("report").textContent="";el("download").disabled=true;
  el("report-note").textContent="请选择本资产的历史 Session 查看报告。切换资产不会改变已保存 Session 的标的。";
  el("basis-summary").textContent="正在读取所选资产…";el("chart-title").textContent=selectedSymbol()+" · 加载中";
  for(const venue of ["entropy","lighter"]) el("inventory-"+venue).textContent="正在查询所选资产，仓位待核验…";
  refresh().catch(() => {});loadMarket();loadInventory();
});
el("zone").addEventListener("change",() => {draw();loadMarket();loadInventory();refresh().catch(() => {});});
window.addEventListener("resize",draw);
async function poll() {try{await refresh();}catch(error){el("error").textContent="工作台连接中断；库存与执行状态未核验。";document.querySelectorAll("[data-action],#create").forEach(b=>b.disabled=true);}finally{setTimeout(poll,2000);}}
controls();poll();loadAssets();setInterval(loadMarket,45000);setInterval(loadInventory,5000);

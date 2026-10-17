"use strict";
const el = id => document.getElementById(id);
const states = {DRAFT:"规则已保存",OBSERVING:"观察中",PAUSED:"已暂停",RECOVERY_REQUIRED:"重启待确认",ENDED:"已结束"};
const labels = {RULES_SAVED:"规则已保存",OBSERVATION_START:"开始观察",OBSERVATION_PAUSE:"暂停观察",OBSERVATION_RESUME:"继续观察",OBSERVATION_END:"结束观察并生成报告",PROCESS_RESTART_OBSERVATION_INTERRUPTED:"进程重启，观察已中断，等待确认",DURATION_REACHED_OBSERVATION_PAUSED:"达到时长，观察已暂停"};
let active = null, busy = false, market = null, report = null;
// A failed response keeps its key so retrying an uncertain create is idempotent.
let pending = null;
const zone = () => el("zone").value;
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
  const allowed = {start:["DRAFT"],pause:["OBSERVING"],resume:["PAUSED","RECOVERY_REQUIRED"],end:["DRAFT","OBSERVING","PAUSED","RECOVERY_REQUIRED"]};
  document.querySelectorAll("[data-action]").forEach(b => b.disabled = busy || !allowed[b.dataset.action].includes(state));
  el("create").disabled = busy || !!active;
}
async function loadReport(id) {
  try {
    report = await api(`/api/sessions/${id}/report`);
    el("report-note").textContent = "观察报告 · 仓位、成交、费用及结算尚未核验；不代表一次真实交易 Session 已完成。";
    el("report").textContent = JSON.stringify(report,null,2);
    el("download").disabled = false;
  } catch (error) { el("error").textContent = error.message; }
}
async function refresh() {
  const data = await api("/api/status"); active = data.active;
  el("state").textContent = active ? states[active.state] : "未运行";
  el("session-id").textContent = active ? `${active.id.slice(0,10)} · v${active.rules_version}` : "可创建下一次 Session";
  el("session-summary").textContent = active ? `${active.rules.direction === "BUY_ENTROPY" ? "买 Entropy / 卖 Lighter" : "卖 Entropy / 买 Lighter"} · 开仓 ${active.rules.entry_bps} bps / 平仓 ${active.rules.exit_bps} bps · 单次 $${active.rules.order_usd} · 库存上限 $${active.rules.max_inventory_usd} / 腿 · ${active.rules.duration_minutes} 分钟（含暂停）` : "先保存规则，再开始观察。";
  el("events").replaceChildren();
  for (const event of data.events.slice().reverse()) {
    const row = document.createElement("div");row.className="event";
    const time = document.createElement("time");time.textContent=fmt(event.observed);
    const text = document.createElement("span");text.textContent=labels[event.kind] || event.kind;
    row.append(time,text);el("events").append(row);
  }
  if (!data.events.length) el("events").textContent="暂无活动 Session 事件；可在历史报告中查看。";
  el("history").replaceChildren();
  for (const session of data.sessions) {
    const button = document.createElement("button");
    button.textContent=`${fmt(session.created)} · ${states[session.state]}`;
    button.addEventListener("click",() => loadReport(session.id));el("history").append(button);
  }
  controls();
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
  event.preventDefault();const rules={strategy:el("strategy").value,direction:el("direction").value};
  for (const name of ["entry_bps","exit_bps","order_usd","max_inventory_usd","max_unhedged_usd","duration_minutes"]) rules[name]=Number(el(name).value);
  command("/api/sessions",{rules});
});
document.querySelectorAll("[data-action]").forEach(button => button.addEventListener("click",() => {
  if (active) command(`/api/sessions/${active.id}/${button.dataset.action}`,{version:active.version});
}));
el("download").addEventListener("click",() => {
  if (!report) return;const url=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:"application/json"}));
  const a=document.createElement("a");a.href=url;a.download=`session-${report.session.id}.json`;a.click();setTimeout(() => URL.revokeObjectURL(url),1000);
});
function draw() {
  const canvas=el("basis"),ctx=canvas.getContext("2d"),width=canvas.clientWidth,height=240,dpr=window.devicePixelRatio || 1;
  canvas.width=width*dpr;canvas.height=height*dpr;ctx.scale(dpr,dpr);ctx.clearRect(0,0,width,height);
  const points=(market?.points || []).filter(p => Number.isFinite(p.basis_bps));ctx.font="12px system-ui";
  if (points.length<2) {ctx.fillStyle="#91a3af";ctx.fillText("尚无足够的有效配对分钟数据",20,100);return;}
  const low=Math.min(0,...points.map(p => p.basis_bps)),high=Math.max(0,...points.map(p => p.basis_bps));
  const min=low-1,max=high+1,first=points[0].minute_ts,last=points.at(-1).minute_ts;
  const x=ts => 52+(ts-first)/(last-first || 1)*(width-76),y=v => 20+(max-v)/(max-min)*172;
  ctx.strokeStyle="#26333d";ctx.fillStyle="#91a3af";
  for(let i=0;i<=4;i++){const v=min+(max-min)*i/4;ctx.beginPath();ctx.moveTo(50,y(v));ctx.lineTo(width-20,y(v));ctx.stroke();ctx.fillText(v.toFixed(1),4,y(v)+4);}
  ctx.strokeStyle="#63dfbd";ctx.lineWidth=1.5;ctx.beginPath();let previous=null;
  for(const point of points){if(!previous || point.minute_ts-previous.minute_ts>120)ctx.moveTo(x(point.minute_ts),y(point.basis_bps));else ctx.lineTo(x(point.minute_ts),y(point.basis_bps));previous=point;}ctx.stroke();
  ctx.fillStyle="#91a3af";ctx.fillText(ChartTime.format(first,zone()),52,225);ctx.textAlign="right";ctx.fillText(ChartTime.format(last,zone()),width-20,225);ctx.textAlign="left";
}
async function loadMarket() {
  try {market=await api("/api/basis");const s=market.summary;
    el("basis-summary").textContent=`${s.basis_count} 根配对分钟 · P05 / 中位 / P95：${[s.p05_bps,s.p50_bps,s.p95_bps].map(v => v == null ? "—" : v.toFixed(2)).join(" / ")} bps · 最新 ${fmt(s.last_minute_ts)} · 数据距今 ${s.data_age_sec == null ? "未知" : Math.round(s.data_age_sec)+" 秒"}`;draw();
  } catch(error){el("basis-summary").textContent="行情历史读取失败；不会作为执行依据。";}
}
el("zone").addEventListener("change",() => {draw();refresh().catch(() => {});});
window.addEventListener("resize",draw);
async function poll() {try{await refresh();}catch(error){el("error").textContent="工作台连接中断；库存与执行状态未核验。";document.querySelectorAll("[data-action],#create").forEach(b=>b.disabled=true);}finally{setTimeout(poll,2000);}}
controls();poll();loadMarket();setInterval(loadMarket,45000);

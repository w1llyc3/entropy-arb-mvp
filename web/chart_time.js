/* Pure display formatting. UTC epochs and market classification never change. */
(function (root) {
  "use strict";
  const ZONES = ["Asia/Shanghai", "America/New_York"];
  const cache = new Map();
  function parts(ts, zone) {
    if (!ZONES.includes(zone) || !Number.isFinite(ts)) throw new RangeError("invalid chart time");
    if (!cache.has(zone)) cache.set(zone, new Intl.DateTimeFormat("zh-CN", {
      timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit",
      weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    }));
    return Object.fromEntries(cache.get(zone).formatToParts(new Date(ts * 1000)).map(p => [p.type,p.value]));
  }
  function abbreviation(ts) {
    return new Intl.DateTimeFormat("en-US", {timeZone:"America/New_York",timeZoneName:"short"})
      .formatToParts(new Date(ts * 1000)).find(p => p.type === "timeZoneName").value;
  }
  function zoneLabel(zone, ts) {
    if (zone === "Asia/Shanghai") return "北京时间 UTC+8";
    if (zone !== "America/New_York") throw new RangeError("invalid chart timezone");
    return "美东时间 " + (ts == null ? "ET（自动夏令时）" : abbreviation(ts));
  }
  function format(ts, zone, full = false) {
    const p = parts(ts,zone);
    const short = `${p.month}/${p.day} ${p.hour}:${p.minute}`;
    return full ? `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute} ${p.weekday} · ${zoneLabel(zone,ts)}` : short;
  }
  const api = {format,zoneLabel,abbreviation,ZONES};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ChartTime = api;
})(typeof globalThis === "undefined" ? this : globalThis);

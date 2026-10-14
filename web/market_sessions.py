"""Display-only US cash-equity reference calendar. Never an execution gate.

Scheduled NYSE 2026–2028 dates, checked 2026-10-14:
https://ir.theice.com/press/news-details/2025/NYSE-Group-Announces-2026-2027-and-2028-Holiday-and-Early-Closings-Calendar/default.aspx
Early-close late-session cutoff follows NYSE Arca's published 17:00 ET.
Does not model emergency closures, individual halts or perp trading hours.
"""
from datetime import date,datetime,time,timedelta,timezone
from zoneinfo import ZoneInfo

NEW_YORK=ZoneInfo("America/New_York")
YEARS=(2026,2027,2028)
HOLIDAYS=set("""
2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25
2026-06-19 2026-07-03 2026-09-07 2026-11-26 2026-12-25
2027-01-01 2027-01-18 2027-02-15 2027-03-26 2027-05-31
2027-06-18 2027-07-05 2027-09-06 2027-11-25 2027-12-24
2028-01-17 2028-02-21 2028-04-14 2028-05-29 2028-06-19
2028-07-04 2028-09-04 2028-11-23 2028-12-25
""".split())
EARLY_CLOSE=set("2026-11-27 2026-12-24 2027-11-26 2028-07-03 2028-11-24".split())
LABELS=dict(pre="盘前",regular="常规交易",post="盘后",overnight="隔夜 / 常规闭市",
            weekend="周末",holiday="美股节假日",unknown="日历未验证")


def day_schedule(day):
    if day.year not in YEARS: return [(0,1440,"unknown",LABELS["unknown"])]
    if day.weekday()>=5: return [(0,1440,"weekend",LABELS["weekend"])]
    if day.isoformat() in HOLIDAYS: return [(0,1440,"holiday",LABELS["holiday"])]
    early=day.isoformat() in EARLY_CLOSE
    close,late=(780,1020) if early else (960,1200)
    return [(0,240,"overnight",LABELS["overnight"]),(240,570,"pre",LABELS["pre"]),
            (570,close,"regular","常规交易（提前收盘）" if early else LABELS["regular"]),
            (close,late,"post","盘后（提前结束）" if early else LABELS["post"]),
            (late,1440,"overnight",LABELS["overnight"])]


def classify(ts):
    local=datetime.fromtimestamp(ts,timezone.utc).astimezone(NEW_YORK)
    minute=local.hour*60+local.minute
    for start,end,key,label in day_schedule(local.date()):
        if start<=minute<end:
            return dict(key=key,label=label,et_date=local.date().isoformat(),
                        et_abbreviation=local.tzname(),calendar_verified=local.year in YEARS)


def segments(start,end):
    if end<=start or end-start>366*86400: return []
    day=datetime.fromtimestamp(start,timezone.utc).astimezone(NEW_YORK).date()
    last=datetime.fromtimestamp(end,timezone.utc).astimezone(NEW_YORK).date()
    out=[]
    while day<=last:
        for lo,hi,key,label in day_schedule(day):
            midnight=datetime.combine(day,time(),NEW_YORK)
            # Construct local wall times before converting: DST days are
            # actually 23 or 25 hours, never blindly add 86400 UTC seconds.
            a=(midnight+timedelta(minutes=lo)).timestamp()
            b=(midnight+timedelta(minutes=hi)).timestamp()
            a,b=max(a,start),min(b,end)
            if b>a: out.append(dict(start=a,end=b,key=key,label=label))
        day+=timedelta(days=1)
    return out


def annotate(payload):
    points=payload["points"]
    for point in points: point["session"]=classify(point["minute_ts"])
    payload["session_segments"]=segments(points[0]["minute_ts"],points[-1]["minute_ts"]+60) if points else []
    payload["session_reference"]=dict(timezone="America/New_York",calendar_years=list(YEARS),
        label="美股参考时段（NYSE/Arca），不代表永续合约开闭市；不含临时停牌/临时休市",
        normal="美东 04:00–09:30 盘前 · 09:30–16:00 常规 · 16:00–20:00 盘后",
        early="提前收盘日：13:00 常规收盘、17:00 盘后结束（NYSE Arca 口径）")
    return payload

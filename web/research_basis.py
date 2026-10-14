"""Recorded research data only; never consults accounts or execution JSON."""
import csv
import json
import math
from pathlib import Path
import time

from entropy_arb.watchlist import ASSETS,asset_for
from web.basis import build_basis,MAX_POINTS
from web.market_sessions import annotate


def finite(value):
    try:
        number=float(value)
        return number if math.isfinite(number) else None
    except (ValueError,TypeError): return None


def catalog(root):
    path=Path(root)/"logs"/"record-only"/"catalog.json"
    saved={}; observed=None
    try:
        data=json.loads(path.read_text(encoding="utf-8"))
        saved={r["symbol"]:r for r in data["assets"]}; observed=data["observed_at"]
    except (OSError,ValueError,KeyError,TypeError): pass
    rows=[]
    for asset in ASSETS:
        row=saved.get(asset.symbol,{})
        rows.append(dict(symbol=asset.symbol,label=asset.label,entropy_coin=asset.coin,
            lighter_symbol=asset.lighter_symbol,state=row.get("state","NOT_CHECKED"),
            market_id=row.get("market_id"),csv_path=asset.csv_rel,
            execution_enabled=False,contract_terms_verified=False))
    return dict(assets=rows,observed_at=observed,record_only=True)


def quantile(values,q):
    values=sorted(values)
    if not values: return None
    index=(len(values)-1)*q; low=int(index); high=min(low+1,len(values)-1)
    return values[low]+(values[high]-values[low])*(index-low)


def usable(row,paired,now):
    ts=finite(row.get("minute_ts")); samples=finite(row.get("samples"))
    if ts is None or ts<0 or ts>now or samples is None or not 0<samples<=60: return False
    for prefix in (("entropy","hedge") if paired else ("entropy",)):
        bid,ask=finite(row.get(prefix+"_bid")),finite(row.get(prefix+"_ask"))
        if bid is None or ask is None or bid<=0 or ask<bid: return False
    return True


def summary(points,rows,now):
    values=[p["basis_bps"] for p in points if p.get("basis_bps") is not None]
    out=dict(minutes=len(points),basis_count=len(values),min_bps=min(values) if values else None,
             max_bps=max(values) if values else None,p05_bps=quantile(values,.05),
             p50_bps=quantile(values,.5),p95_bps=quantile(values,.95))
    wanted={p["minute_ts"] for p in points}
    valid=[r for r in rows if finite(r.get("minute_ts")) in wanted]
    samples=[finite(r.get("samples")) for r in valid]
    # Duplicate/restarted minutes are last-row-wins, matching the price series.
    out["fresh_samples"]=sum(max(0,min(s or 0,60)) for s in samples)
    expected=(int((points[-1]["minute_ts"]-points[0]["minute_ts"])/60)+1)*60 if points else 0
    out["observed_span_sec"]=expected
    out["coverage_fraction"]=out["fresh_samples"]/expected if expected else None
    out["last_minute_ts"]=points[-1]["minute_ts"] if points else None
    out["data_age_sec"]=max(0,now-points[-1]["minute_ts"]-60) if points else None
    for side in ("sell","buy"):
        series=[finite(r.get(side+"_edge_mean_bps")) for r in valid]
        series=[v for v in series if v is not None]
        out[side+"_edge_p50_bps"]=quantile(series,.5)
        out[side+"_edge_p95_bps"]=quantile(series,.95)
        series=[finite(r.get("fill_"+side+"_edge_100_bps")) for r in valid]
        series=[v for v in series if v is not None]
        out["fill_"+side+"_100_p50_bps"]=quantile(series,.5)
        out["fill_"+side+"_100_minutes"]=len(series)
    return out


def build_research_basis(root,symbol="SNDK",hours=0,now=None):
    asset=asset_for(symbol); root=Path(root); now=time.time() if now is None else now
    if not math.isfinite(hours) or not 0<=hours<=24*366: hours=0
    market=next(r for r in catalog(root)["assets"] if r["symbol"]==symbol)
    paired_path=root/asset.csv_rel
    single_rel=asset.csv_rel.replace("minutes.csv","entropy-only.csv")
    single_path=root/single_rel
    single=(market["state"]=="ENTROPY_ONLY" or (not paired_path.exists() and single_path.exists()))
    rel=single_rel if single else asset.csv_rel
    path=(root/rel).resolve()
    path.relative_to(root.resolve())
    rows={}
    try:
        with path.open(encoding="utf-8-sig",newline="") as source:
            for row in csv.DictReader(source):
                ts=finite(row.get("minute_ts"))
                if ts is not None and usable(row,not single,now): rows[ts]=row
    except (OSError,csv.Error): pass
    if single:
        points=[]
        for ts,row in sorted(rows.items()):
            bid,ask=finite(row.get("entropy_bid")),finite(row.get("entropy_ask"))
            if bid is None or ask is None or bid<=0 or ask<bid: continue
            points.append(dict(minute_ts=ts,entropy_usd=(bid+ask)/2,hedge_usd=None,basis_bps=None))
        if hours: points=[p for p in points if p["minute_ts"]>=now-hours*3600]
        payload=dict(points=points[-MAX_POINTS:],csv_path=rel,csv_exists=path.exists(),markers=[],bands={},error=None)
    else:
        payload=build_basis(root,hours=hours,now=now,csv_rel=rel,include_markers=False)
        payload["points"]=[p for p in payload["points"] if p["minute_ts"] in rows]
        payload["markers"]=[]; payload["bands"]={}
    stats=summary(payload["points"],list(rows.values()),now)
    stats["requested_window_coverage_fraction"]=min(1,stats["fresh_samples"]/(hours*3600)) if hours else None
    payload.update(symbol=symbol,label=asset.label,market=market,paired=not single,
        entropy_label=asset.coin+" @ Entropy",hedge_label=asset.lighter_symbol+" @ Lighter",
        summary=stats,
        formula="(Entropy mid / hedge mid - 1) × 10000",
        research_warning="价格差不是净收益：未扣手续费、资金费与滑点；合约标的/结算条款尚未核验。")
    return annotate(payload)

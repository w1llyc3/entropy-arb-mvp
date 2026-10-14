import asyncio
import csv
import json
from pathlib import Path
import time

from fastapi.testclient import TestClient
import pytest

from entropy_arb.watchlist import ASSETS,resolve_catalog,asset_for
from entropy_arb.research_recorder import SingleVenueRecorder,ResearchCollector
from entropy_arb.recorder import MinuteRecorder
from web.research_basis import build_research_basis
from web.panel import create_app
from test_service import setup,book


def metadata():
    hl=dict(universe=[dict(name=a.coin) for a in ASSETS])
    lighter=dict(order_books=[dict(symbol=a.lighter_symbol,market_id=i,status="active",market_type="perp")
                             for i,a in enumerate(ASSETS) if a.symbol not in ("IONQ","GPRO")])
    return hl,lighter


def save_catalog(root,rows):
    path=root/"logs"/"record-only"/"catalog.json"
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(dict(observed_at=123,assets=rows)),encoding="utf-8")


def write_rows(root,symbol,rows,single=False):
    rel=asset_for(symbol).csv_rel
    if single: rel=rel.replace("minutes.csv","entropy-only.csv")
    path=root/rel; path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("w",encoding="utf-8",newline="") as out:
        writer=csv.DictWriter(out,fieldnames=sorted({k for row in rows for k in row}))
        writer.writeheader(); writer.writerows(rows)


def row(ts=60,premium=10):
    return dict(minute_ts=ts,entropy_bid=100,entropy_ask=102,hedge_bid=99,hedge_ask=101,
        premium_close_bps=premium,samples=60,sell_edge_mean_bps=5,buy_edge_mean_bps=-6,
        fill_sell_edge_100_bps=2)


def test_catalog_uses_explicit_aliases_and_never_guesses_missing_markets():
    rows={r["symbol"]:r for r in resolve_catalog(*metadata())}
    assert len(rows)==10
    assert rows["ANTH"]["lighter_symbol"]=="ANTHROPIC"
    assert rows["OAI"]["lighter_symbol"]=="OPENAI"
    assert rows["TCNT"]["lighter_symbol"]=="TENCENT"
    assert sum(r["state"]=="PAIRED" for r in rows.values())==8
    assert rows["IONQ"]["state"]==rows["GPRO"]["state"]=="ENTROPY_ONLY"
    assert all(not r["execution_enabled"] for r in rows.values())


@pytest.mark.parametrize("change",["inactive","spot","duplicate","delisted"])
def test_unsafe_catalogs_do_not_produce_pairs(change):
    hl,lighter=metadata()
    if change=="inactive": lighter["order_books"][0]["status"]="inactive"
    if change=="spot": lighter["order_books"][0]["market_type"]="spot"
    if change=="duplicate": lighter["order_books"].append(dict(lighter["order_books"][0]))
    if change=="delisted": hl["universe"][0]["isDelisted"]=True
    assert resolve_catalog(hl,lighter)[0]["state"]!="PAIRED"


def test_asset_data_stats_and_markers_are_isolated(tmp_path,monkeypatch):
    import web.basis as basis
    def forbidden(*a): pytest.fail("research must not read legacy trade evidence")
    monkeypatch.setattr(basis,"load_markers",forbidden)
    write_rows(tmp_path,"SNDK",[row(premium=999)])
    write_rows(tmp_path,"ANTH",[row(60,-10),row(120,0),row(240,10),row(240,20)])
    payload=build_research_basis(tmp_path,"ANTH",now=400)
    assert [p["basis_bps"] for p in payload["points"]]==[-10,0,20]
    assert payload["summary"]["p50_bps"]==0
    assert payload["summary"]["p05_bps"]==pytest.approx(-9)
    assert payload["summary"]["p95_bps"]==pytest.approx(18)
    assert payload["summary"]["coverage_fraction"]==.75
    assert payload["summary"]["fill_sell_100_minutes"]==3
    assert payload["markers"]==[] and payload["bands"]=={}
    assert payload["entropy_label"].startswith("io:ANTH")


@pytest.mark.parametrize("symbol",[a.symbol for a in ASSETS])
def test_all_assets_have_honest_empty_chart(tmp_path,symbol):
    payload=build_research_basis(tmp_path,symbol,now=400)
    assert payload["points"]==[]
    assert payload["summary"]["p50_bps"] is None
    assert payload["summary"]["coverage_fraction"] is None


def test_single_venue_records_pause_and_never_creates_fake_basis(tmp_path):
    save_catalog(tmp_path,resolve_catalog(*metadata()))
    asset=asset_for("IONQ"); flag=tmp_path/"paused"
    path=tmp_path/asset.csv_rel.replace("minutes.csv","entropy-only.csv")
    recorder=SingleVenueRecorder(path,book(50,51),flag)
    recorder.sample(60); flag.touch(); recorder.sample(61)
    flag.unlink(); recorder.sample(62); recorder.flush()
    payload=build_research_basis(tmp_path,"IONQ",now=400)
    assert not payload["paired"]
    assert payload["points"][0]["entropy_usd"]==50.5
    assert payload["points"][0]["hedge_usd"] is None
    assert payload["points"][0]["basis_bps"] is None
    assert payload["summary"]["fresh_samples"]==2


@pytest.mark.parametrize("field,value",[("entropy_ask",99),("hedge_bid",0),("hedge_ask","NaN"),
                                         ("samples",0),("minute_ts",9999)])
def test_bad_or_future_quotes_cannot_become_research_opportunities(tmp_path,field,value):
    bad=row(); bad[field]=value
    write_rows(tmp_path,"OAI",[bad])
    assert build_research_basis(tmp_path,"OAI",now=400)["points"]==[]


def test_real_recorder_schema_roundtrips_to_asset_chart(tmp_path):
    recorder=MinuteRecorder(str(tmp_path/asset_for("NBIS").csv_rel),book(201,201.1),book(200,200.1),10)
    recorder.sample(60); recorder.sample(61); recorder.close()
    payload=build_research_basis(tmp_path,"NBIS",now=400)
    assert len(payload["points"])==1
    assert payload["summary"]["fresh_samples"]==2
    assert payload["summary"]["fill_sell_100_minutes"]==1


def test_http_research_never_expands_execution_scope(tmp_path):
    svc,venues,_=setup(tmp_path)
    with TestClient(create_app(tmp_path,service=svc)) as client:
        assets=client.get("/api/research/assets").json()["assets"]
        assert len(assets)==10
        for asset in assets:
            response=client.get("/api/basis",params=dict(symbol=asset["symbol"]))
            assert response.status_code==200
            assert response.json()["points"]==[]
        assert client.get("/api/basis",params=dict(symbol="../../secret")).status_code==400
        assert client.post("/api/start",json=dict(symbol="NBIS")).status_code==409
        assert all(v.submit_calls==0 for v in venues.values())


def test_cancelled_research_streams_flush_and_release_all_workers(tmp_path,monkeypatch):
    import entropy_arb.research_recorder as module
    async def feed(self,stop): await asyncio.Event().wait()
    monkeypatch.setattr(module.HLBookFeed,"run",feed)
    monkeypatch.setattr(module.LighterBookFeed,"run",feed)
    async def run():
        collector=ResearchCollector(tmp_path,tmp_path/"pause")
        async def catalog(_): return resolve_catalog(*metadata())
        collector.catalog=catalog
        collector.start()
        for _ in range(100):
            if collector.workers: break
            await asyncio.sleep(.001)
        workers=list(collector.workers)
        assert workers
        await collector.close()
        assert all(task.done() for task in workers)
        assert not collector.workers and not collector.status()["running"]
    asyncio.run(run())

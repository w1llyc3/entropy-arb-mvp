from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from web.market_sessions import classify,segments,annotate
from web.panel import create_app
from test_service import setup


def stamp(value): return datetime.fromisoformat(value.replace("Z","+00:00")).timestamp()


@pytest.mark.parametrize("utc,key",[
    ("2026-10-14T07:59:59Z","overnight"),
    ("2026-10-14T08:00:00Z","pre"),
    ("2026-10-14T13:29:59Z","pre"),
    ("2026-10-14T13:30:00Z","regular"),
    ("2026-10-14T19:59:59Z","regular"),
    ("2026-10-14T20:00:00Z","post"),
    ("2026-10-15T00:00:00Z","overnight"),
    ("2026-01-09T14:29:59Z","pre"),
    ("2026-01-09T14:30:00Z","regular"),
    ("2026-01-09T21:00:00Z","post"),
    ("2026-07-03T14:00:00Z","holiday"),
    ("2026-11-26T15:00:00Z","holiday"),
    ("2026-11-27T17:59:59Z","regular"),
    ("2026-11-27T18:00:00Z","post"),
    ("2026-11-27T22:00:00Z","overnight"),
    ("2026-12-24T18:00:00Z","post"),
    ("2026-10-17T15:00:00Z","weekend"),
    ("2029-10-15T15:00:00Z","unknown"),
    # NYSE explicitly does not observe Saturday New Year 2028 on Friday.
    ("2027-12-31T15:00:00Z","regular"),
])
def test_reference_session_boundaries(utc,key):
    assert classify(stamp(utc))["key"]==key


@pytest.mark.parametrize("start,end,hours",[
    ("2026-03-08T05:00:00Z","2026-03-09T04:00:00Z",23),
    ("2026-11-01T04:00:00Z","2026-11-02T05:00:00Z",25),
])
def test_dst_weekend_is_contiguous_and_has_real_duration(start,end,hours):
    a,b=stamp(start),stamp(end)
    parts=segments(a,b)
    assert parts[0]["start"]==a and parts[-1]["end"]==b
    assert sum(p["end"]-p["start"] for p in parts)==hours*3600
    assert all(p["key"]=="weekend" for p in parts)


def test_beijing_next_day_uses_previous_us_trading_date():
    result=classify(stamp("2026-10-15T00:30:00Z"))
    assert result["et_date"]=="2026-10-14"
    assert result["et_abbreviation"]=="EDT"


def test_segments_cover_window_with_exact_transition_instants():
    parts=segments(stamp("2026-10-14T07:00:00Z"),stamp("2026-10-15T01:00:00Z"))
    assert [p["key"] for p in parts]==["overnight","pre","regular","post","overnight"]
    assert all(a["end"]==b["start"] for a,b in zip(parts,parts[1:]))
    assert parts[2]["start"]==stamp("2026-10-14T13:30:00Z")


def test_calendar_annotation_does_not_change_utc_or_prices():
    ts=stamp("2026-10-14T06:25:00Z")
    payload=annotate(dict(points=[dict(minute_ts=ts,entropy_usd=100,hedge_usd=99,basis_bps=101)]))
    point=payload["points"][0]
    assert point["minute_ts"]==ts and point["basis_bps"]==101
    assert point["session"]["key"]=="overnight"
    assert payload["session_reference"]["calendar_years"]==[2026,2027,2028]


def test_time_script_and_session_api_are_read_only(tmp_path):
    svc,venues,_=setup(tmp_path)
    with TestClient(create_app(tmp_path,service=svc)) as client:
        script=client.get("/assets/chart-time.js")
        assert script.status_code==200 and "javascript" in script.headers["content-type"]
        assert client.get("/api/basis").json()["session_segments"]==[]
        assert all(v.submit_calls==0 for v in venues.values())

"""Basis chart series: minutes.csv parse, empty files, fill markers.

Run:  python3 -m pytest tests/test_basis.py
"""
import csv
import json
import os
import struct
import subprocess
import sys
import zlib

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from web.basis import (  # noqa: E402
    REQUIRED_COLUMNS,
    build_basis,
    classify_fills,
    load_markers,
    parse_minutes,
)
from tools.plot_basis import render_png  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
PLOT = os.path.join(ROOT, "tools", "plot_basis.py")

HEADER = [
    "minute_ts", "time_utc", "entropy_bid", "entropy_ask",
    "hedge_bid", "hedge_ask", "premium_close_bps",
    "sell_edge_mean_bps", "buy_edge_mean_bps", "samples",
]


def _write(path, rows, header=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header or HEADER, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _minute(ts, prem="10", e_bid="100", e_ask="102", h_bid="99", h_ask="101"):
    return {
        "minute_ts": ts,
        "time_utc": "2026-01-01T00:00:00Z",
        "entropy_bid": e_bid,
        "entropy_ask": e_ask,
        "hedge_bid": h_bid,
        "hedge_ask": h_ask,
        "premium_close_bps": prem,
        "samples": "60",
    }


def _minutes(tmp_path, rows):
    path = tmp_path / "logs" / "minutes.csv"
    _write(path, rows)
    return path


def _png_raw(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos = 8
    w = h = None
    idat = b""
    while pos + 8 <= len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + ln]
        if tag == b"IHDR":
            w, h = struct.unpack(">II", chunk[:8])
        elif tag == b"IDAT":
            idat += chunk
        elif tag == b"IEND":
            break
        pos += 12 + ln
    raw = zlib.decompress(idat)
    return w, h, raw


def _count_if(raw, w, h, pred):
    stride = w * 3
    n = 0
    for y in range(h):
        row = raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)]
        for i in range(0, len(row), 3):
            if pred(row[i], row[i + 1], row[i + 2]):
                n += 1
    return n


def test_parse_uses_recorded_premium_and_mids(tmp_path):
    path = _minutes(tmp_path, [
        _minute(1_700_000_000, prem="12.5"),
        _minute(1_700_000_060, prem=""),
    ])
    parsed = parse_minutes(path)
    assert parsed["missing_columns"] == []
    assert parsed["skipped_rows"] == 0
    assert len(parsed["points"]) == 2
    first = parsed["points"][0]
    assert first["entropy_usd"] == pytest.approx(101.0)
    assert first["hedge_usd"] == pytest.approx(100.0)
    assert first["basis_bps"] == pytest.approx(12.5)
    assert first["basis_source"] == "premium_close_bps"
    # Blank premium_close_bps uses the same mids. 101/100 - 1 = 100 bps.
    second = parsed["points"][1]
    assert second["basis_source"] == "mids"
    assert second["basis_bps"] == pytest.approx(100.0)
    assert second["time_utc"] == "2026-01-01T00:00:00Z"


def test_missing_and_empty_and_header_only_do_not_invent_rows(tmp_path):
    missing = build_basis(tmp_path)
    assert missing["csv_exists"] is False
    assert missing["points"] == []
    assert missing["markers"] == []
    assert missing["error"] is None
    assert missing["missing_columns"] == list(REQUIRED_COLUMNS)
    assert "not on disk" in missing["note"]

    path = tmp_path / "logs" / "minutes.csv"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    empty = build_basis(tmp_path)
    assert empty["csv_exists"] is True
    assert empty["header_only"] is False
    assert empty["points"] == []
    assert empty["error"] is None
    assert "empty" in empty["note"]
    for col in REQUIRED_COLUMNS:
        assert col in empty["note"]

    path.write_text(
        "minute_ts,entropy_bid,entropy_ask,hedge_bid,hedge_ask,premium_close_bps\n",
        encoding="utf-8")
    header = build_basis(tmp_path)
    assert header["header_only"] is True
    assert header["points"] == []
    assert header["error"] is None
    assert "no data rows" in header["note"]

    # A blank trades file is not a fill, and a zero-fill row is not either.
    trades = tmp_path / "logs" / "trades.csv"
    trades.write_bytes(b"")
    assert load_markers(tmp_path) == ([], None)
    _write(trades, [{
        "ts": "1700000000", "direction": "buy_entropy",
        "buy_fill": "0", "sell_fill": "0",
    }], header=["ts", "direction", "buy_venue", "sell_venue", "qty",
                "buy_fill", "sell_fill"])
    assert load_markers(tmp_path) == ([], None)
    _write(trades, [{
        "ts": "", "direction": "buy_entropy",
        "buy_fill": "1", "sell_fill": "1",
    }], header=["ts", "direction", "buy_fill", "sell_fill"])
    assert load_markers(tmp_path) == ([], None)


def test_bad_rows_are_skipped_and_prices_are_not_invented(tmp_path):
    _minutes(tmp_path, [
        _minute("not-a-time", prem="5"),
        _minute(1_700_000_000, prem="4", e_bid="0", e_ask="-1"),
        _minute(1_700_000_060, prem="4", h_bid="", h_ask=""),
        _minute(1_700_000_120, prem="3.5", e_bid="50", e_ask=""),
    ])
    payload = build_basis(tmp_path)
    assert payload["error"] is None
    assert payload["skipped_rows"] == 3
    assert len(payload["points"]) == 1
    assert payload["points"][0]["entropy_usd"] == pytest.approx(50.0)
    assert payload["points"][0]["hedge_usd"] == pytest.approx(100.0)
    assert payload["points"][0]["basis_bps"] == pytest.approx(3.5)
    # The skipped row must not show up as a zero or placeholder price.
    assert all(p["entropy_usd"] > 0 and p["hedge_usd"] > 0 for p in payload["points"])


def test_open_close_from_trade_fills_and_empty_when_absent(tmp_path):
    _minutes(tmp_path, [
        _minute(1_000),
        _minute(2_000),
        _minute(3_000),
    ])
    trades = tmp_path / "logs" / "trades.csv"
    _write(trades, [
        {"ts": "1000", "direction": "buy_entropy", "buy_fill": "2", "sell_fill": "2"},
        {"ts": "2000", "direction": "buy_entropy", "buy_fill": "1", "sell_fill": "1"},
        {"ts": "3000", "direction": "sell_entropy", "buy_fill": "3", "sell_fill": "3"},
    ], header=["ts", "direction", "buy_fill", "sell_fill"])
    payload = build_basis(tmp_path)
    assert payload["marker_source"] == "logs/trades.csv"
    assert [m["kind"] for m in payload["markers"]] == ["open", "open", "close"]
    assert payload["markers"][0]["direction"] == "buy_entropy"
    assert payload["markers"][2]["direction"] == "sell_entropy"

    # Classification uses the whole file, then the visible window drops the
    # earlier open. The remaining fill stays a close.
    window = build_basis(tmp_path, hours=1, now=2_500 + 3_600)
    assert [m["kind"] for m in window["markers"]] == ["close"]


def test_classify_starts_flat_and_ignores_unknown_direction():
    marks = classify_fills([
        {"ts": 1, "direction": "sell_entropy", "qty": 1},
        {"ts": 2, "direction": "buy_entropy", "qty": 1},
        {"ts": 3, "direction": "flip", "qty": 1},
        {"ts": 4, "direction": "buy_entropy", "qty": 0},
    ])
    assert [(m["ts"], m["kind"]) for m in marks] == [(1, "open"), (2, "close")]


def test_panel_last_order_used_only_when_trades_have_no_fills(tmp_path):
    web = tmp_path / ".web"
    web.mkdir()
    (web / "risk.json").write_text(json.dumps({
        "last_order": {
            "confirm_id": "abc",
            "ts": 50,
            "buy_fill": 1.5,
            "sell_fill": 1.5,
        },
    }), encoding="utf-8")
    (web / "probe.log").write_text(
        "2026-01-01T00:00:00Z route confirm_id=abc direction=sell_entropy "
        "force_confirm_outside_rth=false\n"
        "2026-01-01T00:00:01Z result confirm_id=abc routed=true sent=true "
        "ok=true halt= fee_bps=None error=\n",
        encoding="utf-8")
    markers, source = load_markers(tmp_path)
    assert source == ".web/risk.json"
    assert markers == [{
        "ts": 50.0, "kind": "open", "direction": "sell_entropy", "qty": 1.5,
    }]

    # A real trades.csv fill wins. The panel's last order is not added on top.
    _write(tmp_path / "logs" / "trades.csv", [
        {"ts": "10", "direction": "buy_entropy", "buy_fill": "1", "sell_fill": "1"},
    ], header=["ts", "direction", "buy_fill", "sell_fill"])
    markers, source = load_markers(tmp_path)
    assert source == "logs/trades.csv"
    assert len(markers) == 1
    assert markers[0]["direction"] == "buy_entropy"

    # No direction in the log means no open/close marker.
    (tmp_path / "logs" / "trades.csv").unlink()
    (web / "probe.log").write_text("result only, no route\n", encoding="utf-8")
    assert load_markers(tmp_path) == ([], None)


def test_paths_outside_the_repo_are_ignored(tmp_path):
    outside = tmp_path.parent / "outside-minutes.csv"
    outside.write_text(
        "minute_ts,entropy_bid,entropy_ask,hedge_bid,hedge_ask,premium_close_bps\n"
        "10,1,1,1,1,5\n",
        encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        "recorder:\n  csv: /tmp/not-ours.csv\n"
        "logging:\n  trades_csv: ../trades.csv\n",
        encoding="utf-8")
    payload = build_basis(tmp_path)
    assert payload["csv_path"] == "logs/minutes.csv"
    assert payload["points"] == []
    refused = build_basis(tmp_path, csv_rel="../minutes.csv")
    assert refused["points"] == []
    assert str(refused["error"]).startswith("refusing")


def test_bands_default_to_decision_card_and_follow_the_task(tmp_path):
    default = build_basis(tmp_path)["bands"]
    assert default["source"] == "default"
    assert default["midline_bps"] == pytest.approx(-1.7)
    assert default["upper_bps"] == pytest.approx(1.0)
    assert default["lower_bps"] == pytest.approx(1.0)
    assert default["upper_line_bps"] == pytest.approx(-0.7)
    assert default["lower_line_bps"] == pytest.approx(-2.7)

    web = tmp_path / ".web"
    web.mkdir()
    (web / "task.json").write_text(json.dumps({
        "midline_bps": 2.0,
        "upper_bps": 1.5,
        "lower_bps": 0.5,
    }), encoding="utf-8")
    bands = build_basis(tmp_path)["bands"]
    assert bands["source"] == "task"
    assert bands["midline_bps"] == pytest.approx(2.0)
    assert bands["upper_line_bps"] == pytest.approx(3.5)
    assert bands["lower_line_bps"] == pytest.approx(1.5)


def test_png_empty_and_shaded_basis(tmp_path):
    empty = render_png(build_basis(tmp_path))
    w, h, raw = _png_raw(empty)
    assert (w, h) == (1200, 640)
    green = lambda r, g, b: g > r + 15 and g > b + 10
    red = lambda r, g, b: r > g + 15 and r > b + 10
    assert _count_if(raw, w, h, green) < 50

    _minutes(tmp_path, [
        _minute(1_700_000_000, prem="40", e_bid="100", e_ask="100",
                h_bid="100", h_ask="100"),
        _minute(1_700_003_600, prem="50", e_bid="110", e_ask="110",
                h_bid="100", h_ask="100"),
        _minute(1_700_007_200, prem="-20", e_bid="90", e_ask="90",
                h_bid="100", h_ask="100"),
    ])
    png = render_png(build_basis(tmp_path))
    w, h, raw = _png_raw(png)
    assert _count_if(raw, w, h, green) > 500
    assert _count_if(raw, w, h, red) > 100
    out = tmp_path / "basis.png"
    out.write_bytes(png)
    assert out.stat().st_size > 1000


def test_plot_cli_writes_png_and_refuses_outside_csv(tmp_path):
    proc = subprocess.run(
        [sys.executable, PLOT, "--root", str(tmp_path), "--out", "shot.png"],
        cwd=str(tmp_path), capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    shot = tmp_path / "shot.png"
    assert shot.read_bytes().startswith(b"\x89PNG")
    assert "0 minutes" in proc.stdout

    refused = subprocess.run(
        [sys.executable, PLOT, "--root", str(tmp_path),
         "--csv", "../minutes.csv", "--out", "nope.png"],
        cwd=str(tmp_path), capture_output=True, text=True, check=False)
    assert refused.returncode == 2
    assert "refusing" in refused.stderr
    assert not (tmp_path / "nope.png").exists()


def test_panel_serves_the_chart(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from web.panel import create_app

    _minutes(tmp_path, [_minute(1_700_000_000, prem="-1.2")])
    app = create_app(tmp_path)
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        html = page.text
        assert "Price (USD)" in html
        assert "premium_close_bps" in html
        assert "BASIS_POLL_MS = 45000" in html
        assert "/api/basis" in html
        assert "minute_ts" in html
        body = client.get("/api/basis")
        assert body.headers["cache-control"] == "no-store"
        payload = body.json()
        assert payload["points"][0]["basis_bps"] == pytest.approx(-1.2)
        assert payload["bands"]["midline_bps"] == pytest.approx(-1.7)
        assert payload["bands"]["upper_line_bps"] == pytest.approx(-0.7)
        assert payload["bands"]["lower_line_bps"] == pytest.approx(-2.7)

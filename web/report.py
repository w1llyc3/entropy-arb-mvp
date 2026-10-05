"""Status from logs/minutes.csv and a parse of tools/analyze.py output.

Numbers shown as G1–G4 come from the analyzer's own text so the panel and
the CLI cannot drift apart:

  G1  SELL entropy GATE net p50 (pre-fee median − fees, rebate forced to 0)
  G2  BUY entropy GATE net p50  (same rule)
  G3  slip@$100 p90
  G4  mean depth_ok_frac
"""
from __future__ import annotations

import csv
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import yaml

# The analyze button always runs this. Hours, fees, and the sample floor are
# fixed so the panel cannot be pointed at a different gate definition.
ANALYZE_ARGV = [
    sys.executable,
    "tools/analyze.py",
    "--hours", "24",
    "--fees-bps", "0.9",
    "--min-samples", "48",
]
ANALYZE_COMMAND = (
    "python3 tools/analyze.py --hours 24 --fees-bps 0.9 --min-samples 48"
)
DEFAULT_CSV = "logs/minutes.csv"

# A completed minute is written when the clock rolls over, so a healthy
# recorder's newest row is often 60–120s old. Past this, samples have stopped.
STALE_MINUTE_SEC = 180
LOW_SAMPLES = 15
GAP_SEC = 120


def recorder_csv_rel(root: Path) -> str:
    """Relative CSV path from config.yaml, else logs/minutes.csv.

    Absolute paths and ``..`` are ignored. The panel never takes a CSV path
    from the request.
    """
    cfg = Path(root) / "config.yaml"
    if not cfg.is_file():
        return DEFAULT_CSV
    try:
        raw = yaml.safe_load(cfg.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return DEFAULT_CSV
    rec = raw.get("recorder") if isinstance(raw, dict) else None
    path = DEFAULT_CSV
    if isinstance(rec, dict) and rec.get("csv"):
        path = str(rec["csv"]).strip() or DEFAULT_CSV
    parts = Path(path).parts
    if os.path.isabs(path) or ".." in parts or not path:
        return DEFAULT_CSV
    return path


def _blank(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value) -> Optional[int]:
    text = _blank(value)
    if text is None:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _float(value) -> Optional[float]:
    text = _blank(value)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def read_minutes(path: Path) -> dict:
    """Summarize a recorder CSV. Missing file is an empty dataset, not an error."""
    empty = {
        "csv_exists": False,
        "minutes_collected": 0,
        "rows": [],
        "error": None,
    }
    if not path.is_file():
        return empty
    rows = []
    try:
        with open(path, newline="") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                empty["csv_exists"] = True
                return empty
            for raw in reader:
                ts = _float(raw.get("minute_ts"))
                samples = _int(raw.get("samples"))
                if ts is None:
                    continue
                rows.append({
                    "minute_ts": ts,
                    "time_utc": _blank(raw.get("time_utc")),
                    "samples": samples,
                    "tob": {
                        "entropy_bid": _blank(raw.get("entropy_bid")),
                        "entropy_ask": _blank(raw.get("entropy_ask")),
                        "hedge_bid": _blank(raw.get("hedge_bid")),
                        "hedge_ask": _blank(raw.get("hedge_ask")),
                        "premium_close_bps": _blank(raw.get("premium_close_bps")),
                        "sell_edge_mean_bps": _blank(raw.get("sell_edge_mean_bps")),
                        "buy_edge_mean_bps": _blank(raw.get("buy_edge_mean_bps")),
                    },
                    "fillable_100": {
                        "sell_bps": _blank(raw.get("fill_sell_edge_100_bps")),
                        "buy_bps": _blank(raw.get("fill_buy_edge_100_bps")),
                    },
                })
    except OSError as exc:
        empty["csv_exists"] = True
        empty["error"] = f"could not read {path.name}: {exc}"
        return empty
    empty["csv_exists"] = True
    empty["minutes_collected"] = len(rows)
    empty["rows"] = rows
    return empty


def coverage_from(rows: list) -> dict:
    """samples/60 for the latest minute, the whole file, and the last five."""
    def frac(samples: Optional[int]) -> Optional[float]:
        if samples is None:
            return None
        return samples / 60.0

    if not rows:
        return {
            "latest_samples": None,
            "latest_frac": None,
            "mean_frac": None,
            "recent_frac": None,
        }
    latest = rows[-1]["samples"]
    known = [r["samples"] for r in rows if r["samples"] is not None]
    mean = (sum(known) / len(known) / 60.0) if known else None
    recent = [r["samples"] for r in rows[-5:] if r["samples"] is not None]
    recent_frac = (sum(recent) / len(recent) / 60.0) if recent else None
    return {
        "latest_samples": latest,
        "latest_frac": frac(latest),
        "mean_frac": mean,
        "recent_frac": recent_frac,
    }


def sample_warnings(rows: list, *, running: bool,
                    uptime_sec: Optional[float], now: float,
                    csv_label: str = DEFAULT_CSV) -> list:
    """Warn when the newest minutes show a stall or a thin sample."""
    warnings = []
    if not rows:
        if running and uptime_sec is not None and uptime_sec > STALE_MINUTE_SEC:
            warnings.append(
                "recent samples dropped: recorder has been up for "
                f"{int(uptime_sec)}s but {csv_label} has no minute rows")
        return warnings
    last = rows[-1]
    samples = last["samples"]
    if samples is not None and samples < LOW_SAMPLES:
        warnings.append(
            "recent samples dropped: latest minute has "
            f"{samples}/60 fresh samples")
    if len(rows) >= 2:
        gap = float(last["minute_ts"]) - float(rows[-2]["minute_ts"])
        if gap > GAP_SEC:
            warnings.append(
                "recent samples dropped: "
                f"{int(gap)}s gap before the latest minute")
    if running and uptime_sec is not None and uptime_sec > STALE_MINUTE_SEC:
        age = now - float(last["minute_ts"])
        if age > STALE_MINUTE_SEC:
            warnings.append(
                f"recent samples dropped: latest minute is {int(age)}s old")
    return warnings


def assemble_status(root: Path, proc: dict, now: Optional[float] = None) -> dict:
    """Merge the pid-file snapshot with the latest minute bar."""
    now = time.time() if now is None else now
    rel = recorder_csv_rel(root)
    data = read_minutes(Path(root) / rel)
    warnings = list(proc.get("warnings") or [])
    if data["error"]:
        warnings.append(data["error"])
    warnings.extend(sample_warnings(
        data["rows"], running=bool(proc.get("running")),
        uptime_sec=proc.get("uptime_sec"), now=now, csv_label=rel))
    latest = data["rows"][-1] if data["rows"] else None
    latest_out = None
    if latest is not None:
        latest_out = {
            "time_utc": latest["time_utc"],
            "minute_ts": latest["minute_ts"],
            "samples": latest["samples"],
            "tob": latest["tob"],
            "fillable_100": latest["fillable_100"],
        }
    return {
        "running": bool(proc.get("running")),
        "pid": proc.get("pid"),
        "uptime_sec": proc.get("uptime_sec"),
        "symbol": proc.get("symbol"),
        "hedge": proc.get("hedge"),
        "minutes_collected": data["minutes_collected"],
        "samples_coverage": coverage_from(data["rows"]),
        "latest": latest_out,
        "warnings": warnings,
        "log_tail": proc.get("log_tail") or [],
        "csv_path": rel,
        "csv_exists": data["csv_exists"],
        "csv_hint": (
            "Minute bars are written to logs/minutes.csv "
            "(one row per completed minute)."
        ),
    }


def _num(token: str) -> Optional[float]:
    token = token.strip().split()[0] if token else ""
    if not token or token == "n/a":
        return None
    try:
        value = float(token)
    except ValueError:
        return None
    return value


def _text_num(token: str) -> Optional[str]:
    token = token.strip().split()[0] if token else ""
    if not token or token == "n/a":
        return None
    if _num(token) is None:
        return None
    return token


def parse_analyze(stdout: str) -> dict:
    """Pull G1–G4 out of ``tools/analyze.py`` stdout."""
    g1 = g2 = None
    g1_text = g2_text = None
    slip = {"sell": None, "buy": None, "sell_text": None, "buy_text": None,
            "sell_n": None, "buy_n": None}
    depth = None
    depth_text = None
    basis = None
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("fillable edge @"):
            basis = stripped
        if "|" in line:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3:
                if parts[0].startswith("SELL entropy"):
                    g1_text = _text_num(parts[2])
                    g1 = _num(parts[2])
                elif parts[0].startswith("BUY entropy"):
                    g2_text = _text_num(parts[2])
                    g2 = _num(parts[2])
        if stripped.startswith("G3"):
            match = re.search(
                r"SELL\s+(\S+)\s+\(n=(\d+)\).*BUY\s+(\S+)\s+\(n=(\d+)\)",
                stripped)
            if match:
                slip = {
                    "sell_text": None if match.group(1) == "n/a" else match.group(1),
                    "sell": _num(match.group(1)),
                    "sell_n": int(match.group(2)),
                    "buy_text": None if match.group(3) == "n/a" else match.group(3),
                    "buy": _num(match.group(3)),
                    "buy_n": int(match.group(4)),
                }
        if stripped.startswith("G4"):
            match = re.search(
                r"depth_ok_frac.*?:\s*(n/a|[+-]?\d+(?:\.\d+)?)", stripped)
            if match and match.group(1) != "n/a":
                depth_text = match.group(1)
                depth = _num(match.group(1))
    return {
        "gates": {
            "G1": {
                "id": "G1",
                "label": "SELL entropy GATE net p50 (rebate 0)",
                "value": g1,
                "text": g1_text,
                "unit": "bps",
            },
            "G2": {
                "id": "G2",
                "label": "BUY entropy GATE net p50 (rebate 0)",
                "value": g2,
                "text": g2_text,
                "unit": "bps",
            },
            "G3": {
                "id": "G3",
                "label": "slip@$100 p90",
                "sell": slip["sell"],
                "buy": slip["buy"],
                "sell_text": slip["sell_text"],
                "buy_text": slip["buy_text"],
                "sell_n": slip["sell_n"],
                "buy_n": slip["buy_n"],
                "unit": "bps",
            },
            "G4": {
                "id": "G4",
                "label": "mean depth_ok_frac",
                "value": depth,
                "text": depth_text,
                "unit": "fraction",
            },
        },
        "slip_p90": {
            "sell": slip["sell"],
            "buy": slip["buy"],
            "sell_text": slip["sell_text"],
            "buy_text": slip["buy_text"],
        },
        "depth_ok_frac": depth,
        "depth_ok_frac_text": depth_text,
        "basis": basis,
    }


def run_analyze(root: Path) -> dict:
    """Run the fixed analyze command and attach parsed gate numbers."""
    try:
        proc = subprocess.run(
            ANALYZE_ARGV,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "exit_code": None,
            "command": ANALYZE_COMMAND,
            "argv": ANALYZE_ARGV,
            "stdout": "",
            "stderr": "analyze timed out after 120s",
            "csv_path": DEFAULT_CSV,
            "gates": None,
            "slip_p90": None,
            "depth_ok_frac": None,
            "depth_ok_frac_text": None,
            "basis": None,
        }
    parsed = parse_analyze(proc.stdout or "")
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "command": ANALYZE_COMMAND,
        "argv": ANALYZE_ARGV,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "csv_path": DEFAULT_CSV,
        **parsed,
    }

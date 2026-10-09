"""Read-only equity / available / position. Secrets stay on the server.

The panel parses ``.env`` only to decide whether a public account query is
possible. Private keys are never returned, logged, or sent to the browser.
Position and margin mode are filled when the public payload names SNDK;
otherwise those cells stay null and the UI shows a gap.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

_ENV_KEYS = (
    "HL_PRIVATE_KEY",
    "HL_ACCOUNT_ADDRESS",
    "LIGHTER_ACCOUNT_INDEX",
    "LIGHTER_API_KEY_INDEX",
    "LIGHTER_API_PRIVATE_KEY",
)
_SECRET = re.compile(
    r"(0x[0-9a-fA-F]{16,}|PRIVATE|api_private|secret|BEGIN )",
    re.IGNORECASE,
)
HL_INFO = "https://api.hyperliquid.xyz/info"
LIGHTER_ACCOUNT = "https://mainnet.zklighter.elliot.ai/api/v1/account"


def parse_env_file(path: Path) -> dict:
    found = {key: "" for key in _ENV_KEYS}
    if not path.is_file():
        return found
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return found
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key in found:
            found[key] = value.strip().strip('"').strip("'")
    return found


def creds_ready(vals: dict) -> dict:
    entropy = bool(vals.get("HL_PRIVATE_KEY") and vals.get("HL_ACCOUNT_ADDRESS"))
    lighter = bool(vals.get("LIGHTER_ACCOUNT_INDEX")
                   and vals.get("LIGHTER_API_KEY_INDEX")
                   and vals.get("LIGHTER_API_PRIVATE_KEY"))
    return {"entropy": entropy, "lighter": lighter}


def _blank_leg() -> dict:
    return {"equity": None, "available": None, "position": None, "isolated": None}


def _public_snapshot(creds: dict, note: Optional[str] = None) -> dict:
    return {
        "creds": {"entropy": bool(creds.get("entropy")),
                  "lighter": bool(creds.get("lighter"))},
        "entropy": _blank_leg(),
        "lighter": _blank_leg(),
        "note": note,
    }


def scrub(value):
    """Drop anything that looks like key material before it leaves the process."""
    if isinstance(value, str):
        if _SECRET.search(value):
            return "[redacted]"
        return value
    if isinstance(value, dict):
        return {str(k): scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, tuple):
        return [scrub(v) for v in value]
    return value


def _opener_json(opener, url: str, payload: Optional[dict], timeout: float):
    data = None
    headers = {"Accept": "application/json", "User-Agent": "entropy-arb-panel"}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    open_fn = opener.open if hasattr(opener, "open") else opener
    with open_fn(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8", errors="replace"))


def _apply_hl(out: dict, payload: dict) -> None:
    margin = payload.get("marginSummary") or {}
    if margin.get("accountValue") is not None:
        out["entropy"]["equity"] = float(margin["accountValue"])
    if payload.get("withdrawable") is not None:
        out["entropy"]["available"] = float(payload["withdrawable"])
    for item in payload.get("assetPositions") or []:
        pos = (item or {}).get("position") or {}
        coin = str(pos.get("coin") or "")
        if "SNDK" not in coin.upper():
            continue
        out["entropy"]["position"] = float(pos.get("szi") or 0.0)
        lev = pos.get("leverage") or {}
        if isinstance(lev, dict) and lev.get("type"):
            out["entropy"]["isolated"] = str(lev["type"]).lower() == "isolated"
        break


def _apply_lighter(out: dict, payload: dict) -> None:
    accounts = payload.get("accounts") or []
    acct = accounts[0] if accounts else None
    if not isinstance(acct, dict):
        return
    if acct.get("total_asset_value") is not None:
        out["lighter"]["equity"] = float(acct["total_asset_value"])
    if acct.get("available_balance") is not None:
        out["lighter"]["available"] = float(acct["available_balance"])
    for pos in acct.get("positions") or []:
        if not isinstance(pos, dict):
            continue
        symbol = str(pos.get("symbol") or pos.get("market_symbol") or "")
        if symbol.upper() != "SNDK":
            continue
        sign = float(pos.get("sign") or 1.0)
        out["lighter"]["position"] = sign * float(pos.get("position") or 0.0)
        mode = pos.get("margin_mode") or pos.get("isolated")
        if isinstance(mode, bool):
            out["lighter"]["isolated"] = mode
        elif isinstance(mode, str) and mode:
            out["lighter"]["isolated"] = mode.lower() == "isolated"
        break


def read_accounts(root: Path, opener=None, timeout: float = 2.5) -> dict:
    """Public balances when identifiers exist. Failures stay empty, not fatal."""
    vals = parse_env_file(Path(root) / ".env")
    flags = creds_ready(vals)
    if not flags["entropy"] and not flags["lighter"]:
        return _public_snapshot(flags, "未配置密钥")
    out = _public_snapshot(flags, None)
    fetch = opener or urllib.request.urlopen
    errors = False
    if flags["entropy"] and vals.get("HL_ACCOUNT_ADDRESS"):
        try:
            payload = _opener_json(
                fetch, HL_INFO,
                {"type": "clearinghouseState",
                 "user": vals["HL_ACCOUNT_ADDRESS"],
                 "dex": "io"},
                timeout)
            if isinstance(payload, dict):
                _apply_hl(out, payload)
        except (OSError, urllib.error.URLError, ValueError, TypeError, KeyError):
            errors = True
    if flags["lighter"] and vals.get("LIGHTER_ACCOUNT_INDEX"):
        try:
            url = (LIGHTER_ACCOUNT + "?by=index&value="
                   + str(vals["LIGHTER_ACCOUNT_INDEX"]))
            payload = _opener_json(fetch, url, None, timeout)
            if isinstance(payload, dict):
                _apply_lighter(out, payload)
        except (OSError, urllib.error.URLError, ValueError, TypeError, KeyError):
            errors = True
    if errors and out["entropy"]["equity"] is None and out["lighter"]["equity"] is None:
        out["note"] = "已检测到密钥，余额尚未读到"
    elif (flags["entropy"] or flags["lighter"]) and out["note"] is None:
        if out["entropy"]["position"] is None and out["lighter"]["position"] is None:
            out["note"] = "权益已读；持仓或逐仓标记可能为空"
    return scrub(out)

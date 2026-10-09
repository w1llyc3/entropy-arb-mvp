"""Read-only equity / available / position. Secrets stay on the server.

The panel parses ``.env`` only to decide whether a public account query is
possible. Private keys are never returned, logged, or sent to the browser.
Position and margin mode are filled when the public payload names SNDK;
otherwise those cells stay null and the UI shows a gap.

Hyperliquid Unified accounts hold USDC in the spot clearinghouse. The
``dex=io`` perp clearinghouse still names an isolated SNDK position, but
its account value and withdrawable are 0 and are not the balance. Equity
and available then come from spot USDC. Available prefers
``tokenToAvailableAfterMaintenance``; otherwise the USDC coin's
``available`` and ``total`` fields. That available number is the confirm
funding gate and must cover the order notional.
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


def missing_live_env(root: Path) -> list:
    """Names of the five live keys that are missing or blank. Values stay here."""
    vals = parse_env_file(Path(root) / ".env")
    return [key for key in _ENV_KEYS if not vals.get(key)]


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


def _optional_float(value):
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _hl_margin_blank(payload: dict) -> bool:
    """True when perp equity and withdrawable are both missing or zero.

    That is the Unified shape: the dex clearinghouse does not hold the USDC.
    """
    margin = payload.get("marginSummary") or {}
    equity = _optional_float(margin.get("accountValue"))
    available = _optional_float(payload.get("withdrawable"))
    equity_blank = equity is None or equity == 0.0
    available_blank = available is None or available == 0.0
    return equity_blank and available_blank


def _apply_hl(out: dict, payload: dict) -> None:
    """SNDK position from the dex clearinghouse.

    Non-zero perp margin is kept. A 0/null margin is left unset so the
    caller can fill equity and available from spot USDC.
    """
    if not _hl_margin_blank(payload):
        margin = payload.get("marginSummary") or {}
        equity = _optional_float(margin.get("accountValue"))
        available = _optional_float(payload.get("withdrawable"))
        if equity is not None:
            out["entropy"]["equity"] = equity
        if available is not None:
            out["entropy"]["available"] = available
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


def _usdc_entry(balances) -> Optional[dict]:
    for item in balances or []:
        if not isinstance(item, dict):
            continue
        coin = str(item.get("coin") or "").upper()
        token = item.get("token")
        if coin == "USDC" or token == 0 or str(token) == "0":
            return item
    return None


def _same_token(left, right) -> bool:
    if left is None or right is None:
        return False
    if isinstance(left, str) and left.strip().upper() == "USDC":
        return isinstance(right, str) and right.strip().upper() == "USDC"
    try:
        return float(left) == float(right)
    except (TypeError, ValueError):
        return str(left) == str(right)


def _maintenance_pairs(raw):
    if isinstance(raw, dict):
        return list(raw.items())
    if not isinstance(raw, list):
        return []
    pairs = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            pairs.append((item[0], item[1]))
        elif isinstance(item, dict):
            token = item.get("token", item.get("coin"))
            amount = item.get("amount", item.get("available"))
            pairs.append((token, amount))
    return pairs


def _maintenance_usdc(payload: dict, token_id) -> Optional[float]:
    raw = payload.get("tokenToAvailableAfterMaintenance")
    if raw is None:
        return None
    named = None
    indexed = None
    for token, amount in _maintenance_pairs(raw):
        if isinstance(token, str) and token.strip().upper() == "USDC":
            named = _optional_float(amount)
            break
        if token_id is not None and _same_token(token, token_id):
            indexed = _optional_float(amount)
            break
    if named is not None:
        return named
    return indexed


def _apply_spot_usdc(out: dict, payload: dict) -> None:
    """Equity and available from the USDC spot balance.

    ``tokenToAvailableAfterMaintenance`` wins for available when it names
    USDC. Otherwise the coin entry's ``available``, then ``total``. Equity
    uses ``total`` when that field is present, and the available figure
    when it is not.
    """
    if not isinstance(payload, dict):
        return
    entry = _usdc_entry(payload.get("balances"))
    token_id = 0
    if entry is not None and entry.get("token") is not None:
        token_id = entry.get("token")
    maintained = _maintenance_usdc(payload, token_id)
    coin_available = None
    coin_total = None
    if entry is not None:
        coin_available = _optional_float(entry.get("available"))
        coin_total = _optional_float(entry.get("total"))
    if maintained is not None:
        available = maintained
    elif coin_available is not None:
        available = coin_available
    else:
        available = coin_total
    if coin_total is not None:
        equity = coin_total
    else:
        equity = available
    if equity is not None:
        out["entropy"]["equity"] = equity
    if available is not None:
        out["entropy"]["available"] = available


def funding_block(accounts: Optional[dict], order_notional) -> Optional[str]:
    """Confirm gate: Unified spot USDC available must cover the order.

    ``entropy.available`` is that number. A missing reading does not invent
    a shortfall. The message names no address.
    """
    order = _optional_float(order_notional)
    if order is None:
        return None
    entropy = (accounts or {}).get("entropy") or {}
    available = _optional_float(entropy.get("available"))
    if available is None:
        return None
    if available + 1e-9 < order:
        return "资金不足：Unified 可用 USDC 低于订单名义"
    return None


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
        # Address stays in the public query body. It is not logged or returned.
        user = vals["HL_ACCOUNT_ADDRESS"]
        try:
            payload = _opener_json(
                fetch, HL_INFO,
                {"type": "clearinghouseState", "user": user, "dex": "io"},
                timeout)
            if isinstance(payload, dict):
                _apply_hl(out, payload)
                if _hl_margin_blank(payload):
                    spot = _opener_json(
                        fetch, HL_INFO,
                        {"type": "spotClearinghouseState", "user": user},
                        timeout)
                    if isinstance(spot, dict):
                        _apply_spot_usdc(out, spot)
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

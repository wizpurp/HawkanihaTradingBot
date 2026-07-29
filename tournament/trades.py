from __future__ import annotations

import copy
import json
import os
import time
from dataclasses import asdict, fields, is_dataclass
from datetime import datetime

from .models import TournamentTrade
from .profiles import PROFILE_DEFINITIONS, PROFILE_ORDER
from .state import load_json_with_backup, market_timestamp, write_json_atomic


TOURNAMENT_TRADES_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "tournament_trades.json",
)
TOURNAMENT_TRADES_BACKUP_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "tournament_trades.backup.json",
)
LAST_TRADES_LOAD_SOURCE = "EMPTY"
ALLOWED_TRADE_STATUSES = {"OPEN", "CLOSED", "CANCELLED"}
ALLOWED_DIRECTIONS = {"CALL", "PUT"}


def profile_display_name(profile_id: str) -> str:
    return PROFILE_DEFINITIONS[profile_id]["display_name"]


def _coerce_trade(value) -> TournamentTrade | None:
    if is_dataclass(value):
        value = asdict(value)
    if not isinstance(value, dict):
        return None
    allowed = {field.name for field in fields(TournamentTrade)}
    payload = {key: value.get(key) for key in allowed if key in value}
    try:
        trade = TournamentTrade(**payload)
        validate_tournament_trade(trade)
        return trade
    except Exception:
        return None


def _trade_sort_key(trade: TournamentTrade):
    if trade.entry_epoch is not None:
        return float(trade.entry_epoch)
    try:
        return datetime.fromisoformat(trade.entry_time).timestamp()
    except Exception:
        return 0.0


def validate_tournament_trade(trade: TournamentTrade) -> None:
    if trade.profile_id not in PROFILE_ORDER:
        raise ValueError("invalid profile_id")
    if trade.profile_display_name != profile_display_name(trade.profile_id):
        raise ValueError("profile_display_name does not match profile_id")
    if trade.direction not in ALLOWED_DIRECTIONS:
        raise ValueError("direction must be CALL or PUT")
    if trade.status not in ALLOWED_TRADE_STATUSES:
        raise ValueError("invalid status")
    if int(trade.contracts) < 1:
        raise ValueError("contracts must be at least 1")
    if float(trade.entry_price) <= 0:
        raise ValueError("entry_price must be greater than 0")
    expected_cost = float(trade.entry_price) * 100 * int(trade.contracts)
    if abs(float(trade.entry_cost) - expected_cost) > 0.000001:
        raise ValueError("entry_cost must equal entry_price * 100 * contracts")
    if trade.status == "CLOSED":
        required_exit_values = [
            trade.exit_time,
            trade.exit_epoch,
            trade.exit_price,
            trade.exit_price_source,
            trade.exit_value,
            trade.pnl_dollars,
            trade.pnl_percent,
        ]
        if any(value is None or value == "" for value in required_exit_values):
            raise ValueError("CLOSED trades must contain exit fields and P/L")


def generate_trade_id(profile_id: str, option_symbol: str, timestamp: str | None = None) -> str:
    stamp = timestamp or market_timestamp()
    try:
        safe_stamp = datetime.fromisoformat(stamp).strftime("%Y%m%d%H%M%S%f")
    except Exception:
        safe_stamp = "".join(char for char in stamp if char.isdigit())[:20]
    return f"{profile_id}-{safe_stamp}-{option_symbol}"


def load_tournament_trades(path: str = TOURNAMENT_TRADES_FILE) -> list[TournamentTrade]:
    global LAST_TRADES_LOAD_SOURCE
    raw, source = load_json_with_backup(path, [])
    LAST_TRADES_LOAD_SOURCE = source
    if not isinstance(raw, list):
        LAST_TRADES_LOAD_SOURCE = "EMPTY"
        return []

    trades = []
    seen_ids = set()
    for row in raw:
        trade = _coerce_trade(row)
        if not trade or trade.trade_id in seen_ids:
            continue
        trades.append(trade)
        seen_ids.add(trade.trade_id)
    return sorted(trades, key=_trade_sort_key, reverse=True)


def save_tournament_trades(trades: list[TournamentTrade | dict], path: str = TOURNAMENT_TRADES_FILE) -> list[TournamentTrade]:
    normalized = []
    seen_ids = set()
    for row in trades:
        trade = _coerce_trade(row)
        if trade is None:
            raise ValueError("invalid tournament trade")
        if trade.trade_id in seen_ids:
            raise ValueError("duplicate trade_id")
        seen_ids.add(trade.trade_id)
        normalized.append(trade)

    write_json_atomic(path, [asdict(trade) for trade in normalized])
    return copy.deepcopy(normalized)


def append_tournament_trade(trade: TournamentTrade, path: str = TOURNAMENT_TRADES_FILE) -> TournamentTrade:
    trades = load_tournament_trades(path)
    if any(existing.trade_id == trade.trade_id for existing in trades):
        raise ValueError("duplicate trade_id")
    validate_tournament_trade(trade)
    trades.append(trade)
    save_tournament_trades(trades, path)
    return copy.deepcopy(trade)


def get_tournament_trade(trade_id: str, path: str = TOURNAMENT_TRADES_FILE) -> TournamentTrade | None:
    for trade in load_tournament_trades(path):
        if trade.trade_id == trade_id:
            return copy.deepcopy(trade)
    return None


def update_tournament_trade(trade_id: str, updates: dict, path: str = TOURNAMENT_TRADES_FILE) -> TournamentTrade:
    trades = load_tournament_trades(path)
    updated_trade = None
    for index, trade in enumerate(trades):
        if trade.trade_id != trade_id:
            continue
        payload = asdict(trade)
        payload.update(updates or {})
        payload["trade_id"] = trade.trade_id
        payload["updated_at"] = payload.get("updated_at") or market_timestamp()
        updated_trade = TournamentTrade(**payload)
        validate_tournament_trade(updated_trade)
        trades[index] = updated_trade
        break
    if updated_trade is None:
        raise KeyError("trade_id not found")
    save_tournament_trades(trades, path)
    return copy.deepcopy(updated_trade)


def list_tournament_trades(
    profile_id: str | None = None,
    status: str | None = None,
    limit: int | None = None,
    path: str = TOURNAMENT_TRADES_FILE,
) -> list[TournamentTrade]:
    trades = load_tournament_trades(path)
    if profile_id:
        trades = [trade for trade in trades if trade.profile_id == profile_id]
    if status:
        trades = [trade for trade in trades if trade.status == status]
    if limit is not None:
        trades = trades[: max(0, int(limit))]
    return copy.deepcopy(trades)


def create_synthetic_tournament_trade(
    profile_id: str,
    direction: str,
    entry_price: float = 1.0,
    option_symbol: str | None = None,
    symbol: str = "SPY",
) -> TournamentTrade:
    if profile_id not in PROFILE_ORDER:
        raise ValueError("invalid profile_id")
    direction = str(direction or "").upper()
    if direction not in ALLOWED_DIRECTIONS:
        raise ValueError("direction must be CALL or PUT")
    entry_price = float(entry_price)
    option_symbol = option_symbol or f"{symbol}TEST{direction}"
    timestamp = market_timestamp()
    contracts = 1
    return TournamentTrade(
        trade_id=generate_trade_id(profile_id, option_symbol, timestamp),
        profile_id=profile_id,
        profile_display_name=profile_display_name(profile_id),
        symbol=symbol,
        option_symbol=option_symbol,
        direction=direction,
        contracts=contracts,
        status="OPEN",
        entry_time=timestamp,
        entry_epoch=time.time(),
        entry_price=entry_price,
        entry_price_source="SYNTHETIC",
        entry_cost=entry_price * 100 * contracts,
        signal="TEST_RECORD",
        confidence=0,
        dominance_percent=0.0,
        bullish_score=0,
        bearish_score=0,
        market_state="TEST",
        momentum_required=False,
        or_confirmation_required=False,
        created_at=timestamp,
        updated_at=timestamp,
    )

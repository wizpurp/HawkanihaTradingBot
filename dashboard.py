import re
from flask import Flask, request, redirect, jsonify
import requests
import json
import csv
import os
import threading
import time
import html as html_lib
from datetime import datetime, timedelta, time as datetime_time
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from logs.trade_logger import *

load_dotenv()


app = Flask(__name__)
MARKET_TZ = ZoneInfo("America/New_York")


def market_now():
    return datetime.now(MARKET_TZ)


def parse_market_datetime(value):
    if not value:
        return None

    text = str(value).strip()
    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"]:
        try:
            return datetime.strptime(text[:19], fmt).replace(tzinfo=MARKET_TZ)
        except:
            pass

    return None

TOKEN = os.getenv("TRADIER_TOKEN", "")
ACCOUNT = os.getenv("TRADIER_ACCOUNT", "")
BASE_URL = os.getenv("TRADIER_BASE_URL", "https://sandbox.tradier.com/v1")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_AUDIT_FILE = os.path.join(APP_DIR, "bot_audit_log.csv")
BOT_AUDIT_COLUMNS = [
    "timestamp", "action", "decision", "symbol", "option_symbol",
    "market_state", "bullish_score", "bearish_score", "confidence",
    "current_signal", "bot_enabled", "reason_log", "entry_grade",
    "live_grade", "exit_grade", "pnl", "pnl_percent", "current_price",
    "entry_price", "current_value", "cost_basis", "bot_budget",
    "spent_today", "trades_today", "skip_reason", "exit_reason",
    "order_status", "order_id", "human_or_bot", "max_unrealized_profit",
    "max_drawdown", "hold_time", "spy_price_at_entry", "spy_price_at_exit",
    "market_state_at_entry", "market_state_at_exit", "ema_state",
    "vwap_state", "green_tick_percent", "red_tick_percent",
    "highest_option_price_since_entry", "trailing_stop_percent",
    "calculated_stop_price", "stop_armed", "no_sell_reason",
    "sell_trigger_reason", "trailing_drawdown_percent",
    "entry_price_source", "estimated_entry_price"
]
BOT_LOCK = threading.Lock()
BOT_STATE = {
    "samples": [],
    "bullish_score": 0,
    "bearish_score": 0,
    "confidence": 0,
    "dominance_percent": 0,
    "bullish_percent": 0,
    "bearish_percent": 0,
    "current_signal": "NONE",
    "last_action": "Idle",
    "reason_log": [],
    "trades_today": 0,
    "spent_today": 0,
    "budget_remaining": 0,
    "next_call_cost": None,
    "next_put_cost": None,
    "market_state": "NEUTRAL",
    "market_context": {},
    "level_distances": {},
    "thread_alive": False,
    "last_tick": "",
    "samples_length": 0,
    "last_trade_timestamp": "",
    "cooldown_remaining_seconds": 0,
    "historical_levels": {},
    "historical_levels_day": None,
    "historical_levels_symbol": None,
    "running": False,
    "day": None,
    "position_peaks": {},
    "position_max_profit": {},
    "position_max_drawdown": {},
    "last_trade_review": {},
    "last_quote_time": "",
    "last_quote_epoch": None,
    "last_quote_latency_ms": None,
    "last_quote_status": "UNKNOWN",
    "last_position_time": "",
    "last_position_epoch": None,
    "last_position_latency_ms": None,
    "last_position_status": "UNKNOWN",
    "last_tick_epoch": None,
    "last_tick_duration_ms": None,
    "last_error": "None",
    "quote_request_count": 0,
    "quote_latency_total_ms": 0,
    "quote_latency_slowest_ms": 0,
    "quote_failed_count": 0,
    "quote_rate_limited_count": 0,
    "last_order_submit_ms": None,
    "last_broker_confirm_ms": None,
    "last_market_scan_ms": None,
    "last_indicators_ms": None,
    "last_signal_ms": None,
    "pending_entry": {
        "active": False,
        "direction": "",
        "decision": "",
        "option_symbol": "",
        "starting_option_price": None,
        "required_confirmation_percent": 0,
        "confirmation_price": None,
        "current_option_price": None,
        "time_remaining_seconds": 0,
        "status": "NONE",
        "reason": "",
        "started_epoch": None,
        "expires_epoch": None,
        "contracts": 0,
        "contract": {},
        "market_context": {}
    }
}


def clamp_int(value, minimum, maximum, default):
    try:
        number = int(value)
    except:
        number = default
    return max(minimum, min(maximum, number))


def load_config():
    with open("config.json", "r") as f:
        config = json.load(f)

    config.setdefault("entry_rules", {
        "ema_alignment": True,
        "macd_confirmation": True,
        "vwap_confirmation": True,
        "volume_confirmation": False,
        "minimum_signals": 3,
        "allow_calls": True,
        "allow_puts": True,
        "cooldown_minutes": 5,
        "max_trades_per_day": 10
    })

    config.setdefault("bot_enabled", False)
    config.setdefault("strategy_mode", "SURFER")
    config.setdefault("scanner", {"interval_seconds": 60})
    config.setdefault("strategy", {})
    config.setdefault("decision_time", "09:35")
    config.setdefault("bot_budget", 100)
    config.setdefault("human_daily_trading_budget", 500)
    config.setdefault("bot_starting_account_balance", 500)
    config.setdefault("human_starting_account_balance", 500)
    config.setdefault("max_contract_price", 1.00)
    config["max_open_contracts"] = clamp_int(config.get("max_open_contracts", 1), 1, 5, 1)
    config.setdefault("contract_selection_mode", "strict_atm")
    config["option_momentum_confirmation_enabled"] = bool(config.get("option_momentum_confirmation_enabled", True))
    config["option_momentum_percent"] = max(0.1, min(20.0, safe_float(config.get("option_momentum_percent", 2.0), 2.0)))
    config["confirmation_timeout_seconds"] = clamp_int(config.get("confirmation_timeout_seconds", 10), 1, 300, 10)
    config.setdefault("minimum_confidence", 2)
    config["minimum_confidence"] = clamp_int(config.get("minimum_confidence", 2), 1, 10, 2)
    config["minimum_dominance_percent"] = clamp_int(config.get("minimum_dominance_percent", 60), 50, 100, 60)
    config["strategy"].setdefault("ema_fast", 1)
    config["strategy"].setdefault("ema_medium", 5)
    config["strategy"].setdefault("ema_slow", 10)
    config["strategy"].setdefault("ma_fast", 5)
    config["strategy"].setdefault("ma_medium", 10)
    config["strategy"].setdefault("ma_slow", 20)
    config["strategy"].setdefault("use_macd", False)
    config["strategy"].setdefault("use_vwap", True)
    config["strategy"].setdefault("use_volume", False)
    config["strategy"].setdefault("hard_stop_percent", 20)
    config["strategy"].setdefault("trailing_stop_percent", 15)
    config["strategy"]["exit_poll_interval_ms"] = clamp_int(
        config["strategy"].get("exit_poll_interval_ms", 1000),
        100,
        5000,
        1000
    )
    config["strategy"].setdefault("direction_threshold_percent", 60)

    return config


def save_config(config):
    with open("config.json", "w") as f:
        json.dump(config, f, indent=4)


def grade_from_score(score):
    score = max(0, min(100, int(round(score))))
    if score >= 95:
        return "A+"
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "F"


def grade_value(grade):
    return {"A+": 97, "A": 92, "B": 85, "C": 75, "D": 65, "F": 45}.get(grade, 0)


def current_market_context_snapshot():
    with BOT_LOCK:
        context = dict(BOT_STATE.get("market_context") or {})
    return context or empty_market_context(None)


def grade_entry_setup(market_context):
    confidence = int(market_context.get("confidence") or 0)
    bullish_score = int(market_context.get("bullish_score") or 0)
    bearish_score = int(market_context.get("bearish_score") or 0)
    market_state = market_context.get("market_state", "NEUTRAL")
    reasons = list(market_context.get("reasons", []))
    score = 50 + (confidence * 15)
    grade_reasons = []

    if market_state in ["BULLISH", "BEARISH"]:
        score += 10
        grade_reasons.append(f"Market state {market_state}")
    if market_state == "CHOPPY":
        score -= 25
        grade_reasons.append("CHOPPY market penalty")
    if market_state == "STAGNANT":
        score -= 25
        grade_reasons.append("STAGNANT market penalty")
    if confidence <= 1:
        score -= 15
        grade_reasons.append("Weak confidence")

    score_diff = abs(bullish_score - bearish_score)
    if score_diff >= 4:
        score += 8
        grade_reasons.append("Strong score separation")
    elif score_diff <= 1:
        score -= 10
        grade_reasons.append("Conflicting signals")

    score += min(12, len(reasons) * 2)

    for phrase in ["EMA aligned", "MA aligned", "Price above VWAP", "Price below VWAP"]:
        if any(phrase in reason for reason in reasons):
            score += 3

    if any("Volume confirmation" in reason for reason in reasons):
        score += 4
        grade_reasons.append("Volume confirmation")
    if any("ticks" in reason.lower() for reason in reasons):
        score += 4
        grade_reasons.append("Opening direction confirmation")
    if any("Broke previous" in reason or "Broke last hour" in reason for reason in reasons):
        score += 5
        grade_reasons.append("Market structure confirmation")

    grade_reasons.extend(reasons[:6])
    return grade_from_score(score), max(0, min(100, int(round(score)))), "; ".join(grade_reasons[:10])


def grade_live_trade(pl, market_context, position_symbol=""):
    confidence = int(market_context.get("confidence") or 0)
    market_state = market_context.get("market_state", "NEUTRAL")
    current_signal = market_context.get("current_signal", "NONE")
    distance_to_stop = market_context.get("distance_to_trailing_stop")
    pnl = float(pl.get("pnl") or 0)
    pnl_percent = float(pl.get("pnl_percent") or 0)
    score = 55 + (confidence * 8)
    reasons = []
    is_call = "C" in position_symbol[-9:]
    thesis_holds = (is_call and market_state == "BULLISH") or ((not is_call) and market_state == "BEARISH")

    if thesis_holds:
        score += 15
        reasons.append("Original thesis still holds")
    elif market_state in ["CHOPPY", "STAGNANT"]:
        score -= 15
        reasons.append(f"Market is {market_state}")
    elif current_signal != "NONE":
        score -= 20
        reasons.append("Direction flip risk")

    if pnl > 0:
        score += min(12, pnl_percent)
        reasons.append("P/L supports holding")
    elif pnl < 0:
        score += max(-15, pnl_percent)
        reasons.append("Trade is under pressure")

    if distance_to_stop is not None:
        if distance_to_stop > 0:
            score += 5
            reasons.append("Above trailing stop")
        else:
            score -= 15
            reasons.append("Near or below trailing stop")

    reasons.extend((market_context.get("decision_reasons") or market_context.get("reasons") or [])[:5])
    return grade_from_score(score), max(0, min(100, int(round(score)))), "; ".join(reasons[:10])


def position_hold_time(symbol):
    last_buy = find_last_buy(symbol)
    if not last_buy:
        return "N/A"

    try:
        entry_time = parse_market_datetime(last_buy.get("Time", ""))
        return str(market_now() - entry_time).split(".")[0] if entry_time else "N/A"
    except:
        return "N/A"


def stop_debug_values(symbol, entry_price, current_price, config=None):
    config = config or load_config()
    hard_stop_percent = float(config.get("strategy", {}).get("hard_stop_percent", 20))
    trailing_stop_percent = float(config.get("strategy", {}).get("trailing_stop_percent", 15))

    with BOT_LOCK:
        peak_price = BOT_STATE["position_peaks"].get(symbol, entry_price)

    if current_price is not None:
        peak_price = max(peak_price or entry_price, current_price)

    hard_stop_price = entry_price * (1 - hard_stop_percent / 100) if entry_price is not None else None
    trailing_stop_price = peak_price * (1 - trailing_stop_percent / 100) if peak_price is not None else None
    stop_armed = trailing_stop_price >= entry_price if trailing_stop_price is not None and entry_price is not None else False
    drawdown_from_peak_percent = ((peak_price - current_price) / peak_price) * 100 if peak_price and current_price is not None else 0

    return {
        "hard_stop_percent": hard_stop_percent,
        "hard_stop_price": hard_stop_price,
        "peak_price": peak_price,
        "trailing_stop_percent": trailing_stop_percent,
        "trailing_stop_price": trailing_stop_price,
        "stop_armed": stop_armed,
        "drawdown_from_peak_percent": drawdown_from_peak_percent
    }


def current_exit_display(symbol, stop_values):
    context = current_market_context_snapshot()
    decision = context.get("decision") or "HOLD"
    reasons = context.get("decision_reasons") or []
    drawdown = stop_values.get("drawdown_from_peak_percent", 0)
    trailing_percent = stop_values.get("trailing_stop_percent", 0)
    stop_armed = stop_values.get("stop_armed", False)

    if not stop_armed:
        trailing_reasons = ["Trailing stop inactive.", "Trailing stop price has not reached entry."]
    else:
        trailing_reasons = (
            ["Trailing stop hit.", f"Drawdown {drawdown:.2f}% >= {trailing_percent:.2f}%."]
            if drawdown >= trailing_percent
            else ["Trailing stop not hit.", f"Drawdown {drawdown:.2f}% < {trailing_percent:.2f}%."]
        )
    reasons = trailing_reasons + [str(reason) for reason in reasons]

    return decision, "\n".join(str(reason) for reason in reasons)


def grade_exit_trade(symbol, qty, sell_price, pnl, market_context):
    last_buy = find_last_buy(symbol)
    hold_time = ""
    pnl_percent = 0

    if last_buy:
        try:
            entry_price = float(last_buy.get("Price") or 0)
            entry_time = parse_market_datetime(last_buy.get("Time", ""))
            hold_time = str(market_now() - entry_time).split(".")[0] if entry_time else ""
            entry_cost = entry_price * 100 * float(qty)
            pnl_percent = (float(pnl or 0) / entry_cost) * 100 if entry_cost else 0
        except:
            pass

    score = 70
    reasons = []

    if pnl_percent > 10:
        score += 20
        reasons.append("Good profit")
    elif pnl_percent > 0:
        score += 10
        reasons.append("Small profit")
    elif abs(pnl_percent) < 1:
        reasons.append("Break even")
    elif pnl_percent > -10:
        score -= 15
        reasons.append("Small loss")
    else:
        score -= 30
        reasons.append("Large loss")

    decision_reasons = market_context.get("decision_reasons") or []
    if any("Trailing stop" in reason for reason in decision_reasons):
        score += 8
        reasons.append("Trailing stop respected")
    if any("Hard stop" in reason for reason in decision_reasons):
        score += 5
        reasons.append("Stop loss respected")
    if market_context.get("decision") == "SELL":
        score += 8
        reasons.append("Exit followed bot signal")

    reasons.extend(decision_reasons[:5])
    return grade_from_score(score), max(0, min(100, int(round(score)))), "; ".join(reasons[:10]), hold_time


def parse_order_response(status, text, label="ORDER"):
    print(f"{label} HTTP STATUS:", status)
    if status != 200:
        return False, None

    try:
        data = json.loads(text)
        order = data.get("order", data)
        order_status = order.get("status")
        print(f"{label} ORDER STATUS:", order_status)

        if order_status == "rejected":
            reason = (
                order.get("reason_description")
                or order.get("reason")
                or order.get("message")
                or order.get("error")
                or data.get("reason_description")
                or data.get("reason")
                or data.get("message")
                or data.get("error")
                or text
            )
            print(f"{label} REJECTED:", reason)
            return False, order_status

        return order_status in ["ok", "accepted", "filled"], order_status
    except:
        normalized = text.replace(" ", "")
        if '"status":"rejected"' in normalized:
            print(f"{label} REJECTED:", text)
            return False, "rejected"
        if '"status":"ok"' in normalized:
            print(f"{label} ORDER STATUS:", "ok")
            return True, "ok"
        return False, None


def extract_order_id(text):
    try:
        data = json.loads(text)
        order = data.get("order", data)
        return order.get("id") or order.get("order_id") or data.get("id") or data.get("order_id") or ""
    except:
        return ""


def ensure_audit_file():
    if os.path.exists(BOT_AUDIT_FILE) and os.path.getsize(BOT_AUDIT_FILE) > 0:
        with open(BOT_AUDIT_FILE, "r", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            fieldnames = reader.fieldnames or []

        if fieldnames == BOT_AUDIT_COLUMNS:
            return

        with open(BOT_AUDIT_FILE, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=BOT_AUDIT_COLUMNS)
            writer.writeheader()
            for row in rows:
                writer.writerow({column: row.get(column, "") for column in BOT_AUDIT_COLUMNS})
        return

    with open(BOT_AUDIT_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=BOT_AUDIT_COLUMNS)
        writer.writeheader()


def log_bot_audit(action, decision, symbol, market_context, config, **extra):
    ensure_audit_file()
    reason_log = extra.get("reason_log") or market_context.get("decision_reasons") or market_context.get("reasons") or []
    tick_statistics = market_context.get("tick_statistics", {})
    row = {
        "timestamp": market_now().strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "decision": decision,
        "symbol": symbol,
        "option_symbol": extra.get("option_symbol", ""),
        "market_state": market_context.get("market_state", ""),
        "bullish_score": market_context.get("bullish_score", ""),
        "bearish_score": market_context.get("bearish_score", ""),
        "confidence": market_context.get("confidence", ""),
        "current_signal": market_context.get("current_signal", ""),
        "bot_enabled": config.get("bot_enabled"),
        "reason_log": " | ".join(str(reason) for reason in reason_log),
        "entry_grade": extra.get("entry_grade", ""),
        "live_grade": extra.get("live_grade", ""),
        "exit_grade": extra.get("exit_grade", ""),
        "pnl": extra.get("pnl", ""),
        "pnl_percent": extra.get("pnl_percent", ""),
        "current_price": extra.get("current_price", market_context.get("price", "")),
        "entry_price": extra.get("entry_price", ""),
        "current_value": extra.get("current_value", ""),
        "cost_basis": extra.get("cost_basis", ""),
        "bot_budget": config.get("bot_budget", ""),
        "spent_today": BOT_STATE.get("spent_today", ""),
        "trades_today": BOT_STATE.get("trades_today", ""),
        "skip_reason": extra.get("skip_reason", ""),
        "exit_reason": extra.get("exit_reason", ""),
        "order_status": extra.get("order_status", ""),
        "order_id": extra.get("order_id", ""),
        "human_or_bot": extra.get("human_or_bot", "BOT"),
        "max_unrealized_profit": extra.get("max_unrealized_profit", ""),
        "max_drawdown": extra.get("max_drawdown", ""),
        "hold_time": extra.get("hold_time", ""),
        "spy_price_at_entry": extra.get("spy_price_at_entry", market_context.get("price", "") if action in ["BUY CALL", "BUY PUT"] else ""),
        "spy_price_at_exit": extra.get("spy_price_at_exit", market_context.get("price", "") if action == "SELL" else ""),
        "market_state_at_entry": extra.get("market_state_at_entry", market_context.get("market_state", "") if action in ["BUY CALL", "BUY PUT"] else ""),
        "market_state_at_exit": extra.get("market_state_at_exit", market_context.get("market_state", "") if action == "SELL" else ""),
        "ema_state": extra.get("ema_state", market_context.get("ema_state", "")),
        "vwap_state": extra.get("vwap_state", market_context.get("vwap_state", "")),
        "green_tick_percent": extra.get("green_tick_percent", tick_statistics.get("green_percent", "")),
        "red_tick_percent": extra.get("red_tick_percent", tick_statistics.get("red_percent", "")),
        "highest_option_price_since_entry": extra.get("highest_option_price_since_entry", ""),
        "trailing_stop_percent": extra.get("trailing_stop_percent", ""),
        "calculated_stop_price": extra.get("calculated_stop_price", ""),
        "stop_armed": extra.get("stop_armed", ""),
        "no_sell_reason": extra.get("no_sell_reason", ""),
        "sell_trigger_reason": extra.get("sell_trigger_reason", ""),
        "trailing_drawdown_percent": extra.get("trailing_drawdown_percent", ""),
        "entry_price_source": extra.get("entry_price_source", ""),
        "estimated_entry_price": extra.get("estimated_entry_price", "")
    }

    with open(BOT_AUDIT_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=BOT_AUDIT_COLUMNS)
        writer.writerow({column: row.get(column, "") for column in BOT_AUDIT_COLUMNS})


def update_last_trade_review(review):
    with BOT_LOCK:
        BOT_STATE["last_trade_review"] = review


def option_market_is_open():
    now = market_now().time()
    return datetime_time(9, 30) <= now <= datetime_time(16, 0)


def submit_and_parse_option_order(option_symbol, qty, action, label):
    if action == "buy_to_open":
        allowed, reason, current_total, max_open_contracts = validate_buy_position_cap(qty)
        if not allowed:
            text = f"{reason}; Current: {current_total}; Maximum: {max_open_contracts}"
            print(f"{label} ORDER BLOCKED:", reason)
            print("current_total_option_contracts:", current_total)
            print("max_open_contracts:", max_open_contracts)
            add_bot_reason(f"{label} BUY skipped: {reason}; Current: {current_total}; Maximum: {max_open_contracts}")
            return False, reason, 0, text

    status, text = submit_option_order(option_symbol, qty, action)
    print(f"{label} ORDER STATUS:", status)
    print(f"{label} ORDER RESPONSE:", text)
    ok, order_status = parse_order_response(status, text, label)
    return ok, order_status, status, text


def headers():
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json"
    }


def fmt_money(value):
    if value is None:
        return "N/A"
    try:
        return f"${float(value):,.2f}"
    except:
        return "N/A"


def fmt_int(value):
    if value is None:
        return "N/A"
    try:
        return f"{int(float(value)):,}"
    except:
        return "N/A"


def option_entry_price(cost_basis, qty):
    if not qty:
        return 0
    return cost_basis / (qty * 100)


def option_pnl(sell_price, cost_basis, qty):
    entry_price = option_entry_price(cost_basis, qty)
    return (sell_price - entry_price) * 100 * qty


def option_current_value(option_price, qty):
    return option_price * 100 * qty


def is_option_position(pos):
    return len(str(pos.get("symbol", ""))) > 6


def total_option_contracts(positions):
    if not positions:
        return 0

    total = 0
    for pos in positions:
        if not is_option_position(pos):
            continue
        try:
            total += abs(int(float(pos.get("quantity", 0) or 0)))
        except:
            pass
    return total


def get_position_cap_status(positions, config):
    current_total = total_option_contracts(positions)
    max_open_contracts = clamp_int(config.get("max_open_contracts", 1), 1, 5, 1)
    return {
        "current_total_option_contracts": current_total,
        "max_open_contracts": max_open_contracts,
        "position_cap_status": "ACTIVE" if current_total >= max_open_contracts else "OK"
    }


def validate_buy_position_cap(qty):
    config = load_config()
    max_open_contracts = clamp_int(config.get("max_open_contracts", 1), 1, 5, 1)
    live_positions = get_position()
    current_total = total_option_contracts(live_positions)

    if current_total + int(qty) > max_open_contracts:
        return False, "Max Open Contracts reached", current_total, max_open_contracts

    return True, "OK", current_total, max_open_contracts


def record_api_diagnostic(kind, started_at, status):
    latency_ms = int((time.perf_counter() - started_at) * 1000)
    now = market_now()
    timestamp = now.strftime("%H:%M:%S.%f")[:-3]
    epoch = time.time()
    with BOT_LOCK:
        BOT_STATE[f"last_{kind}_time"] = timestamp
        BOT_STATE[f"last_{kind}_epoch"] = epoch
        BOT_STATE[f"last_{kind}_latency_ms"] = latency_ms
        BOT_STATE[f"last_{kind}_status"] = status
        if kind == "quote":
            BOT_STATE["quote_request_count"] += 1
            BOT_STATE["quote_latency_total_ms"] += latency_ms
            BOT_STATE["quote_latency_slowest_ms"] = max(BOT_STATE["quote_latency_slowest_ms"], latency_ms)
            if status == "HTTP 429":
                BOT_STATE["quote_rate_limited_count"] += 1
            if not str(status).startswith("OK"):
                BOT_STATE["quote_failed_count"] += 1


def record_tick_finished(started_at):
    with BOT_LOCK:
        BOT_STATE["last_tick"] = market_now().strftime("%H:%M:%S.%f")[:-3]
        BOT_STATE["last_tick_epoch"] = time.time()
        BOT_STATE["last_tick_duration_ms"] = int((time.perf_counter() - started_at) * 1000)


def set_last_error(error):
    with BOT_LOCK:
        BOT_STATE["last_error"] = str(error) if error else "None"


def age_ms(epoch):
    if not epoch:
        return None
    return int((time.time() - epoch) * 1000)


def find_broker_position(symbol):
    positions = get_position()
    if not positions:
        return None

    for pos in positions:
        if pos.get("symbol") == symbol:
            return pos

    return None


def resolve_actual_entry_price(option_symbol, expected_qty, estimated_entry_price, retries=6, delay_seconds=0.5):
    for attempt in range(retries):
        pos = find_broker_position(option_symbol)
        if pos:
            try:
                qty = float(pos.get("quantity", expected_qty) or expected_qty)
                cost_basis = float(pos.get("cost_basis", 0) or 0)
                if qty and cost_basis:
                    actual_entry_price = cost_basis / qty / 100
                    print("BUY ENTRY RESOLVED FROM BROKER")
                    print("symbol:", option_symbol)
                    print("qty:", qty)
                    print("cost_basis:", cost_basis)
                    print("actual_entry_price:", actual_entry_price)
                    return actual_entry_price, "BROKER_COST_BASIS", estimated_entry_price
            except Exception as exc:
                print("BUY ENTRY RESOLVE ERROR:", exc)

        if attempt < retries - 1:
            time.sleep(delay_seconds)

    print("BUY ENTRY ESTIMATED")
    print("symbol:", option_symbol)
    print("estimated_entry_price:", estimated_entry_price)
    print("reason:", "broker position unavailable after retry")
    return estimated_entry_price, "ESTIMATED_ASK", estimated_entry_price


def get_market_quote(symbol):
    started_at = time.perf_counter()
    try:
        r = requests.get(
            f"{BASE_URL}/markets/quotes",
            params={"symbols": symbol},
            headers=headers()
        )
        if r.status_code != 200:
            record_api_diagnostic("quote", started_at, f"HTTP {r.status_code}")
            return None

        data = r.json()["quotes"]
        if "quote" not in data:
            record_api_diagnostic("quote", started_at, "NO_QUOTE")
            return None

        quote = data["quote"]
        record_api_diagnostic("quote", started_at, "OK")
        if isinstance(quote, list):
            return quote[0]
        return quote
    except Exception as exc:
        record_api_diagnostic("quote", started_at, f"ERROR {exc}")
        return None


def get_position():
    started_at = time.perf_counter()
    try:
        r = requests.get(
            f"{BASE_URL}/accounts/{ACCOUNT}/positions",
            headers=headers()
        )
        if r.status_code != 200:
            record_api_diagnostic("position", started_at, f"HTTP {r.status_code}")
            return None

        data = r.json()
        print("POSITIONS DATA:", data)

        if data.get("positions") == "null":
            record_api_diagnostic("position", started_at, "OK_NO_POSITION")
            return None

        pos = data["positions"]["position"]
        record_api_diagnostic("position", started_at, "OK_POSITION")
        return pos if isinstance(pos, list) else [pos]
    except Exception as exc:
        record_api_diagnostic("position", started_at, f"ERROR {exc}")
        return None


def get_expirations(symbol):
    try:
        r = requests.get(
            f"{BASE_URL}/markets/options/expirations",
            params={"symbol": symbol, "includeAllRoots": "true"},
            headers=headers()
        )
        if r.status_code != 200:
            return []

        dates = r.json()["expirations"]["date"]
        return dates if isinstance(dates, list) else [dates]
    except:
        return []


def get_option_chain(symbol, expiration):
    try:
        r = requests.get(
            f"{BASE_URL}/markets/options/chains",
            params={"symbol": symbol, "expiration": expiration},
            headers=headers()
        )
        if r.status_code != 200:
            return []

        options = r.json()["options"]["option"]
        return options if isinstance(options, list) else [options]
    except:
        return []


def select_atm_contract(symbol, side):
    quote = get_market_quote(symbol)
    if not quote or quote.get("last") is None:
        return None

    price = float(quote["last"])
    expirations = get_expirations(symbol)
    if not expirations:
        return None

    expiration = expirations[0]
    chain = get_option_chain(symbol, expiration)

    option_type = "call" if side == "CALL" else "put"
    matching = [o for o in chain if o.get("option_type") == option_type]

    if not matching:
        return None

    selected = min(matching, key=lambda o: abs(float(o["strike"]) - price))
    selected["expiration"] = expiration
    selected["underlying_price"] = price
    return selected


def select_closest_contract_within_budget(symbol, side, max_contract_price):
    quote = get_market_quote(symbol)
    if not quote or quote.get("last") is None:
        return None

    price = float(quote["last"])
    expirations = get_expirations(symbol)
    if not expirations:
        return None

    expiration = expirations[0]
    chain = get_option_chain(symbol, expiration)
    option_type = "call" if side == "CALL" else "put"
    matching = [o for o in chain if o.get("option_type") == option_type]
    matching.sort(key=lambda o: abs(float(o["strike"]) - price))

    for contract in matching:
        ask = get_option_trade_price(contract)
        if ask is not None and ask <= max_contract_price:
            contract["expiration"] = expiration
            contract["underlying_price"] = price
            return contract

    return None


def select_entry_contract(config, decision, strict_call, strict_put):
    side = "CALL" if decision == "BUY CALL" else "PUT" if decision == "BUY PUT" else "NONE"
    if side == "NONE":
        return None

    strict_contract = strict_call if side == "CALL" else strict_put
    if config.get("contract_selection_mode", "strict_atm") != "closest_within_budget":
        return strict_contract

    max_contract_price = float(config.get("max_contract_price", 1))
    strict_ask = get_option_trade_price(strict_contract)
    if strict_contract and strict_ask is not None and strict_ask <= max_contract_price:
        return strict_contract

    return select_closest_contract_within_budget(config.get("symbol", "SPY"), side, max_contract_price)


def submit_option_order(option_symbol, qty, action="buy_to_open"):
    config = load_config()
    underlying = config["symbol"]

    data = {
        "class": "option",
        "symbol": underlying,
        "option_symbol": option_symbol,
        "side": action,
        "quantity": str(qty),
        "type": "market",
        "duration": "day"
    }

    started_at = time.perf_counter()
    r = requests.post(
        f"{BASE_URL}/accounts/{ACCOUNT}/orders",
        headers=headers(),
        data=data
    )
    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
    with BOT_LOCK:
        BOT_STATE["last_order_submit_ms"] = elapsed_ms
        BOT_STATE["last_broker_confirm_ms"] = elapsed_ms

    return r.status_code, r.text


def add_bot_reason(message):
    timestamp = market_now().strftime("%H:%M:%S")
    line = f"{timestamp} {message}"

    with BOT_LOCK:
        BOT_STATE["last_action"] = message
        BOT_STATE["reason_log"].append(line)
        BOT_STATE["reason_log"] = BOT_STATE["reason_log"][-80:]

    print(message)


def get_quote_price(quote):
    if not quote:
        return None

    for key in ["last", "bid", "ask"]:
        if quote.get(key) is not None:
            try:
                return float(quote.get(key))
            except:
                pass

    return None


def get_option_trade_price(contract):
    if not contract:
        return None

    for key in ["ask", "last", "bid"]:
        if contract.get(key) is not None:
            try:
                return float(contract.get(key))
            except:
                pass

    return None


def default_pending_entry():
    return {
        "active": False,
        "direction": "",
        "decision": "",
        "option_symbol": "",
        "starting_option_price": None,
        "required_confirmation_percent": 0,
        "confirmation_price": None,
        "current_option_price": None,
        "time_remaining_seconds": 0,
        "status": "NONE",
        "reason": "",
        "started_epoch": None,
        "expires_epoch": None,
        "contracts": 0,
        "contract": {},
        "market_context": {}
    }


def get_pending_entry():
    with BOT_LOCK:
        return dict(BOT_STATE.get("pending_entry") or default_pending_entry())


def set_pending_entry(pending):
    with BOT_LOCK:
        BOT_STATE["pending_entry"] = dict(pending)


def clear_pending_entry(status="NONE", reason=""):
    pending = default_pending_entry()
    pending["status"] = status
    pending["reason"] = reason
    set_pending_entry(pending)


def refresh_pending_time_remaining(pending):
    expires_epoch = pending.get("expires_epoch")
    pending["time_remaining_seconds"] = max(0, int(round(expires_epoch - time.time()))) if expires_epoch else 0
    return pending


def create_pending_entry(config, decision, side, contract, contracts, start_price, market_context):
    momentum_percent = float(config.get("option_momentum_percent", 2.0))
    timeout_seconds = int(config.get("confirmation_timeout_seconds", 10))
    started_epoch = time.time()
    confirmation_price = start_price * (1 + momentum_percent / 100)
    pending = {
        "active": True,
        "direction": side,
        "decision": decision,
        "option_symbol": contract.get("symbol", ""),
        "starting_option_price": start_price,
        "required_confirmation_percent": momentum_percent,
        "confirmation_price": confirmation_price,
        "current_option_price": start_price,
        "time_remaining_seconds": timeout_seconds,
        "status": "WAITING FOR MOMENTUM",
        "reason": "",
        "started_epoch": started_epoch,
        "expires_epoch": started_epoch + timeout_seconds,
        "contracts": contracts,
        "contract": dict(contract),
        "market_context": dict(market_context)
    }
    set_pending_entry(pending)
    add_bot_reason(
        f"PENDING BUY {side}: {contract.get('symbol', '')} start {start_price:.2f}, "
        f"confirm {confirmation_price:.2f}, timeout {timeout_seconds}s"
    )
    log_bot_audit(
        "PENDING ENTRY",
        decision,
        config.get("symbol", ""),
        market_context,
        config,
        option_symbol=contract.get("symbol", ""),
        current_price=start_price,
        skip_reason="waiting for option momentum confirmation"
    )


def pending_entry_snapshot():
    pending = get_pending_entry()
    return refresh_pending_time_remaining(pending)


def parse_clock(value, default_hour=9, default_minute=35):
    try:
        hour, minute = value.split(":")
        return datetime_time(int(hour), int(minute))
    except:
        return datetime_time(default_hour, default_minute)


def is_after_decision_time(config):
    decision = parse_clock(config.get("decision_time", "09:35"))
    return market_now().time() >= decision


def buy_cost(row):
    return safe_float(row.get("Price")) * safe_float(row.get("Qty"), 0) * 100


def daily_buy_totals(rows, source=None, day=None):
    day = day or market_now().strftime("%Y-%m-%d")
    trades_today = 0
    spent_today = 0.0

    for row in rows:
        if row.get("Action") != "BUY":
            continue
        if source and trade_source(row) != source:
            continue
        if not str(row.get("Time", "")).startswith(day):
            continue
        trades_today += 1
        spent_today += buy_cost(row)

    return trades_today, spent_today


def sync_trade_limits_from_file(config):
    today = market_now().strftime("%Y-%m-%d")
    rows = []
    last_sell_time = None

    try:
        rows = load_visible_trade_rows()
        for row in rows:
            if trade_source(row) != "BOT":
                continue
            if row.get("Action") == "SELL":
                try:
                    sell_time = parse_market_datetime(row.get("Time", ""))
                    if sell_time is None:
                        continue
                    if last_sell_time is None or sell_time > last_sell_time:
                        last_sell_time = sell_time
                except:
                    pass
    except:
        pass

    trades_today, spent_today = daily_buy_totals(rows, "BOT", today)

    cooldown_minutes = int(config.get("entry_rules", {}).get("cooldown_minutes", 5) or 0)
    cooldown_remaining_seconds = 0
    if last_sell_time and cooldown_minutes > 0:
        elapsed = (market_now() - last_sell_time).total_seconds()
        cooldown_remaining_seconds = max(0, int((cooldown_minutes * 60) - elapsed))

    with BOT_LOCK:
        if BOT_STATE["day"] != today:
            BOT_STATE["samples"] = []
            BOT_STATE["position_peaks"] = {}
            BOT_STATE["day"] = today
        BOT_STATE["trades_today"] = trades_today
        BOT_STATE["spent_today"] = spent_today
        BOT_STATE["budget_remaining"] = max(0, safe_float(config.get("bot_budget"), 100) - spent_today)
        BOT_STATE["last_trade_timestamp"] = last_sell_time.strftime("%Y-%m-%d %H:%M:%S") if last_sell_time else ""
        BOT_STATE["cooldown_remaining_seconds"] = cooldown_remaining_seconds

    return trades_today, spent_today


configure_trade_logger(
    current_market_context_snapshot=current_market_context_snapshot,
    grade_entry_setup=grade_entry_setup,
    grade_exit_trade=grade_exit_trade,
    stop_debug_values=stop_debug_values,
    load_config=load_config,
    sync_trade_limits_from_file=sync_trade_limits_from_file
)


def get_cooldown_state(config):
    sync_trade_limits_from_file(config)
    with BOT_LOCK:
        return BOT_STATE["last_trade_timestamp"], BOT_STATE["cooldown_remaining_seconds"]


def moving_average(values, period):
    if not values:
        return None

    period = max(1, int(period))
    window = values[-period:]
    return sum(window) / len(window)


def exponential_average(values, period):
    if not values:
        return None

    period = max(1, int(period))
    alpha = 2 / (period + 1)
    ema = values[0]

    for value in values[1:]:
        ema = (value * alpha) + (ema * (1 - alpha))

    return ema


def get_historical_levels(symbol):
    today = market_now().date().isoformat()

    with BOT_LOCK:
        if (
            BOT_STATE["historical_levels_day"] == today
            and BOT_STATE["historical_levels_symbol"] == symbol
            and BOT_STATE["historical_levels"]
        ):
            return dict(BOT_STATE["historical_levels"])

    try:
        end = market_now().date()
        start = end - timedelta(days=14)
        r = requests.get(
            f"{BASE_URL}/markets/history",
            params={
                "symbol": symbol,
                "interval": "daily",
                "start": start.isoformat(),
                "end": end.isoformat()
            },
            headers=headers()
        )
        if r.status_code != 200:
            return {}

        history = r.json().get("history", {}).get("day", [])
        if isinstance(history, dict):
            history = [history]

        completed_days = [
            day for day in history
            if day.get("date") and day.get("date") < end.isoformat()
        ]

        if not completed_days:
            return {}

        previous_day = completed_days[-1]
        previous_week = completed_days[-5:] if len(completed_days) >= 5 else completed_days

        levels = {
            "previous_week_high": max(float(day["high"]) for day in previous_week if day.get("high") is not None),
            "previous_week_low": min(float(day["low"]) for day in previous_week if day.get("low") is not None),
            "previous_day_high": float(previous_day["high"]) if previous_day.get("high") is not None else None,
            "previous_day_low": float(previous_day["low"]) if previous_day.get("low") is not None else None
        }

        with BOT_LOCK:
            BOT_STATE["historical_levels"] = dict(levels)
            BOT_STATE["historical_levels_day"] = today
            BOT_STATE["historical_levels_symbol"] = symbol

        return levels
    except:
        return {}


def parse_tradier_time(value):
    if not value:
        return None

    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"]:
        try:
            return datetime.strptime(value[:19], fmt).replace(tzinfo=MARKET_TZ)
        except:
            pass

    return None


def get_intraday_bars(symbol):
    today = market_now().date()

    try:
        r = requests.get(
            f"{BASE_URL}/markets/timesales",
            params={
                "symbol": symbol,
                "interval": "1min",
                "start": f"{today.isoformat()} 04:00",
                "end": market_now().strftime("%Y-%m-%d %H:%M")
            },
            headers=headers()
        )
        if r.status_code != 200:
            return []

        payload = r.json()
        series_obj = payload.get("series", {})
        if isinstance(series_obj, dict):
            series = series_obj.get("data", [])
        else:
            series = series_obj or payload.get("data", [])
        if isinstance(series, dict):
            series = [series]

        bars = []
        for bar in series:
            bar_time = parse_tradier_time(bar.get("time") or bar.get("timestamp") or bar.get("date"))
            if not bar_time:
                continue
            try:
                bars.append({
                    "time": bar_time,
                    "open": float(bar.get("open")),
                    "high": float(bar.get("high")),
                    "low": float(bar.get("low")),
                    "close": float(bar.get("close")),
                    "volume": float(bar.get("volume") or 0)
                })
            except:
                pass

        return bars
    except:
        return []


def high_low_from_bars(bars):
    if not bars:
        return None, None
    return max(bar["high"] for bar in bars), min(bar["low"] for bar in bars)


def build_real_market_structure(symbol, quote, current_price, config=None):
    now = market_now()
    today = now.date()
    opening_range_start = datetime_time(9, 30)
    opening_range_end = parse_clock((config or {}).get("decision_time", "09:35"))
    bars = get_intraday_bars(symbol)
    history_levels = get_historical_levels(symbol)
    regular_bars = [
        bar for bar in bars
        if bar["time"].date() == today and datetime_time(9, 30) <= bar["time"].time() <= datetime_time(16, 0)
    ]
    premarket_bars = [
        bar for bar in bars
        if bar["time"].date() == today and bar["time"].time() < datetime_time(9, 30)
    ]
    opening_bars = [
        bar for bar in regular_bars
        if opening_range_start <= bar["time"].time() < opening_range_end
    ]
    last_hour_bars = [
        bar for bar in bars
        if bar["time"] >= now - timedelta(hours=1)
    ]

    regular_high, regular_low = high_low_from_bars(regular_bars)
    premarket_high, premarket_low = high_low_from_bars(premarket_bars)
    opening_high, opening_low = high_low_from_bars(opening_bars)
    last_hour_high, last_hour_low = high_low_from_bars(last_hour_bars)
    today_open = regular_bars[0]["open"] if regular_bars else None

    print("opening_range_start:", opening_range_start.strftime("%H:%M"))
    print("opening_range_end:", opening_range_end.strftime("%H:%M"))
    print("bars_used_count:", len(opening_bars))
    print("opening_range_high:", opening_high)
    print("opening_range_low:", opening_low)

    today_high = regular_high
    today_low = regular_low

    if quote:
        for key in ["high", "day_high"]:
            if quote.get(key) is not None:
                try:
                    today_high = float(quote.get(key))
                    break
                except:
                    pass
        for key in ["low", "day_low"]:
            if quote.get(key) is not None:
                try:
                    today_low = float(quote.get(key))
                    break
                except:
                    pass
        for key in ["open", "day_open"]:
            if quote.get(key) is not None:
                try:
                    today_open = float(quote.get(key))
                    break
                except:
                    pass

    levels = {
        "previous_week_high": history_levels.get("previous_week_high"),
        "previous_week_low": history_levels.get("previous_week_low"),
        "previous_day_high": history_levels.get("previous_day_high"),
        "previous_day_low": history_levels.get("previous_day_low"),
        "today_high": today_high if today_high is not None else current_price,
        "today_low": today_low if today_low is not None else current_price,
        "today_open": today_open if today_open is not None else current_price,
        "premarket_high": premarket_high,
        "premarket_low": premarket_low,
        "last_hour_high": last_hour_high if last_hour_high is not None else current_price,
        "last_hour_low": last_hour_low if last_hour_low is not None else current_price,
        "opening_range_high": opening_high,
        "opening_range_low": opening_low
    }

    return {
        "levels": levels,
        "data_source": "Tradier daily/intraday bars",
        "market_date": today.isoformat(),
        "last_updated": now.strftime("%Y-%m-%d %H:%M:%S")
    }


def empty_market_context(price=None):
    levels = {
        "previous_week_high": None,
        "previous_week_low": None,
        "previous_day_high": None,
        "previous_day_low": None,
        "today_high": price,
        "today_low": price,
        "today_open": price,
        "premarket_high": None,
        "premarket_low": None,
        "last_hour_high": price,
        "last_hour_low": price,
        "opening_range_high": price,
        "opening_range_low": price
    }
    return {
        "price": price,
        "ema_state": "NEUTRAL",
        "ma_state": "NEUTRAL",
        "macd_state": "NEUTRAL",
        "vwap_state": "NEUTRAL",
        "volume_state": "NEUTRAL",
        "opening_direction": "NEUTRAL",
        "tick_statistics": {"green_ticks": 0, "red_ticks": 0, "green_percent": 0, "red_percent": 0},
        "levels": levels,
        "market_structure_source": "Tradier daily/intraday bars",
        "market_date": market_now().date().isoformat(),
        "market_structure_last_updated": "",
        "level_distances": {key: None for key in levels},
        "current_pl": 0,
        "distance_to_trailing_stop": None,
        "current_range_size": 0,
        "bullish_score": 0,
        "bearish_score": 0,
        "confidence": 0,
        "dominance_percent": 0,
        "market_state": "NEUTRAL",
        "current_signal": "NONE",
        "decision": "DO NOTHING",
        "reasons": ["Need more market context"],
        "ema_bullish": False,
        "ema_bearish": False,
        "ma_bullish": False,
        "ma_bearish": False,
        "vwap": None
    }


def add_score(reasons, side, text, bullish_score, bearish_score, amount=1):
    reasons.append(text)
    if side == "bullish":
        bullish_score += amount
    elif side == "bearish":
        bearish_score += amount
    return bullish_score, bearish_score


def calculate_dominance_percent(bullish_score, bearish_score):
    total_score = bullish_score + bearish_score
    if total_score <= 0:
        return 0
    return (max(bullish_score, bearish_score) / total_score) * 100


def build_market_context(config, positions=None):
    s = config["strategy"]
    e = config["entry_rules"]
    symbol = config.get("symbol", "SPY")

    with BOT_LOCK:
        samples = list(BOT_STATE["samples"])

    prices = [sample["price"] for sample in samples]
    volumes = [sample["volume"] for sample in samples if sample["volume"] is not None]
    current_price = prices[-1] if prices else None

    if len(prices) < 2:
        return empty_market_context(current_price)

    bullish_score = 0
    bearish_score = 0
    reasons = []
    first_price = prices[0] if prices else None
    vwap = None

    ema_fast = exponential_average(prices, s.get("ema_fast", 1))
    ema_medium = exponential_average(prices, s.get("ema_medium", 5))
    ema_slow = exponential_average(prices, s.get("ema_slow", 10))
    ma_fast = moving_average(prices, s.get("ma_fast", 5))
    ma_medium = moving_average(prices, s.get("ma_medium", 10))
    ma_slow = moving_average(prices, s.get("ma_slow", 20))

    ema_bullish = ema_fast > ema_medium > ema_slow
    ema_bearish = ema_fast < ema_medium < ema_slow
    ma_bullish = ma_fast > ma_medium > ma_slow
    ma_bearish = ma_fast < ma_medium < ma_slow
    ema_state = "BULLISH" if ema_bullish else "BEARISH" if ema_bearish else "NEUTRAL"
    ma_state = "BULLISH" if ma_bullish else "BEARISH" if ma_bearish else "NEUTRAL"

    if e.get("ema_alignment", True):
        if ema_bullish:
            bullish_score, bearish_score = add_score(reasons, "bullish", "EMA aligned bullish", bullish_score, bearish_score)
        elif ema_bearish:
            bullish_score, bearish_score = add_score(reasons, "bearish", "EMA aligned bearish", bullish_score, bearish_score)

    if ma_bullish:
        bullish_score, bearish_score = add_score(reasons, "bullish", "MA aligned bullish", bullish_score, bearish_score)
    elif ma_bearish:
        bullish_score, bearish_score = add_score(reasons, "bearish", "MA aligned bearish", bullish_score, bearish_score)

    macd_state = "NEUTRAL"
    if s.get("use_macd", False) and e.get("macd_confirmation", True):
        macd_fast = exponential_average(prices, 12)
        macd_slow = exponential_average(prices, 26)
        macd_value = macd_fast - macd_slow

        if macd_value > 0:
            macd_state = "BULLISH"
            bullish_score, bearish_score = add_score(reasons, "bullish", "MACD bullish", bullish_score, bearish_score)
        elif macd_value < 0:
            macd_state = "BEARISH"
            bullish_score, bearish_score = add_score(reasons, "bearish", "MACD bearish", bullish_score, bearish_score)

    total_volume = sum(sample["volume"] or 0 for sample in samples)
    if total_volume > 0:
        vwap = sum(sample["price"] * (sample["volume"] or 0) for sample in samples) / total_volume

    vwap_state = "NEUTRAL"
    if s.get("use_vwap", True) and e.get("vwap_confirmation", True) and vwap:
        if current_price > vwap:
            vwap_state = "BULLISH"
            bullish_score, bearish_score = add_score(reasons, "bullish", "Price above VWAP", bullish_score, bearish_score)
        elif current_price < vwap:
            vwap_state = "BEARISH"
            bullish_score, bearish_score = add_score(reasons, "bearish", "Price below VWAP", bullish_score, bearish_score)

    volume_state = "NEUTRAL"
    if s.get("use_volume", False) and e.get("volume_confirmation", False) and len(volumes) >= 2:
        avg_volume = sum(volumes[:-1]) / len(volumes[:-1])
        if volumes[-1] > avg_volume and current_price > prices[-2]:
            volume_state = "BULLISH"
            bullish_score, bearish_score = add_score(reasons, "bullish", "Volume confirmation bullish", bullish_score, bearish_score)
        elif volumes[-1] > avg_volume and current_price < prices[-2]:
            volume_state = "BEARISH"
            bullish_score, bearish_score = add_score(reasons, "bearish", "Volume confirmation bearish", bullish_score, bearish_score)

    green_ticks = 0
    red_ticks = 0

    for previous, current in zip(prices, prices[1:]):
        if current > previous:
            green_ticks += 1
        elif current < previous:
            red_ticks += 1

    total_ticks = green_ticks + red_ticks
    bullish_percent = (green_ticks / total_ticks) * 100 if total_ticks else 0
    bearish_percent = (red_ticks / total_ticks) * 100 if total_ticks else 0
    threshold = float(s.get("direction_threshold_percent", 60))
    opening_direction = "NEUTRAL"

    if first_price is not None and current_price > first_price and bullish_percent >= threshold:
        opening_direction = "BULLISH"
        bullish_score, bearish_score = add_score(reasons, "bullish", f"Green ticks {bullish_percent:.0f}%", bullish_score, bearish_score)
    elif first_price is not None and current_price < first_price and bearish_percent >= threshold:
        opening_direction = "BEARISH"
        bullish_score, bearish_score = add_score(reasons, "bearish", f"Red ticks {bearish_percent:.0f}%", bullish_score, bearish_score)

    structure = build_real_market_structure(symbol, get_market_quote(symbol), current_price, config)
    levels = structure["levels"]

    level_distances = {
        key: current_price - value if value is not None and current_price is not None else None
        for key, value in levels.items()
    }

    for key in ["previous_day_high", "previous_week_high", "last_hour_high", "opening_range_high"]:
        if levels.get(key) is not None and current_price > levels[key]:
            bullish_score, bearish_score = add_score(
                reasons, "bullish", f"Broke {key.replace('_', ' ')}", bullish_score, bearish_score
            )

    for key in ["previous_day_low", "previous_week_low", "last_hour_low", "opening_range_low"]:
        if levels.get(key) is not None and current_price < levels[key]:
            bullish_score, bearish_score = add_score(
                reasons, "bearish", f"Broke {key.replace('_', ' ')}", bullish_score, bearish_score
            )

    current_range_size = levels["today_high"] - levels["today_low"]
    stagnant_range = current_price * 0.001 if current_price else 0
    stagnant_market = current_range_size <= stagnant_range

    current_pl = 0
    distance_to_trailing_stop = None

    if positions:
        pos = positions[0]
        position_symbol = pos.get("symbol", "")
        qty = float(pos.get("quantity", 0) or 0)
        cost_basis = float(pos.get("cost_basis", 0) or 0)
        position_quote = get_market_quote(position_symbol)
        position_price = get_quote_price(position_quote)
        if position_price is not None and qty:
            entry_price = option_entry_price(cost_basis, qty) if len(position_symbol) > 6 else cost_basis / qty
            multiplier = 100 if len(position_symbol) > 6 else 1
            current_pl = (position_price - entry_price) * qty * multiplier
            with BOT_LOCK:
                peak = max(BOT_STATE["position_peaks"].get(position_symbol, entry_price), position_price)
                BOT_STATE["position_peaks"][position_symbol] = peak
                max_profit = max(BOT_STATE["position_max_profit"].get(position_symbol, current_pl), current_pl)
                max_drawdown = min(BOT_STATE["position_max_drawdown"].get(position_symbol, current_pl), current_pl)
                BOT_STATE["position_max_profit"][position_symbol] = max_profit
                BOT_STATE["position_max_drawdown"][position_symbol] = max_drawdown
            trailing_stop_price = peak * (1 - (float(s.get("trailing_stop_percent", 15)) / 100))
            distance_to_trailing_stop = position_price - trailing_stop_price
            if current_pl > 0:
                reasons.append("Current P/L supports holding")

    minimum_signals = int(e.get("minimum_signals", 3))
    minimum_confidence = int(config.get("minimum_confidence", 2))
    minimum_dominance_percent = int(config.get("minimum_dominance_percent", 60))
    confidence = abs(bullish_score - bearish_score)
    dominance_percent = calculate_dominance_percent(bullish_score, bearish_score)
    market_state = "NEUTRAL"

    if bullish_score >= minimum_signals and confidence >= minimum_confidence and dominance_percent >= minimum_dominance_percent:
        market_state = "BULLISH"
        current_signal = "CALL"
    elif bearish_score >= minimum_signals and confidence >= minimum_confidence and dominance_percent >= minimum_dominance_percent:
        market_state = "BEARISH"
        current_signal = "PUT"
    else:
        current_signal = "NONE"
        if confidence <= 1:
            market_state = "CHOPPY"
            reasons.append(f"Confidence {confidence} is too low")
        elif dominance_percent < minimum_dominance_percent:
            reasons.append(f"Dominance {dominance_percent:.1f}% is below minimum {minimum_dominance_percent}%")
        elif stagnant_market:
            market_state = "STAGNANT"
        elif bullish_percent < 55 and bearish_percent < 55:
            market_state = "CHOPPY"
            reasons.append("Ticks and scores are choppy")

    return {
        "price": current_price,
        "ema_state": ema_state,
        "ma_state": ma_state,
        "macd_state": macd_state,
        "vwap_state": vwap_state,
        "volume_state": volume_state,
        "opening_direction": opening_direction,
        "tick_statistics": {
            "green_ticks": green_ticks,
            "red_ticks": red_ticks,
            "green_percent": bullish_percent,
            "red_percent": bearish_percent
        },
        "levels": levels,
        "market_structure_source": structure["data_source"],
        "market_date": structure["market_date"],
        "market_structure_last_updated": structure["last_updated"],
        "level_distances": level_distances,
        "current_pl": current_pl,
        "distance_to_trailing_stop": distance_to_trailing_stop,
        "current_range_size": current_range_size,
        "bullish_score": bullish_score,
        "bearish_score": bearish_score,
        "confidence": confidence,
        "dominance_percent": dominance_percent,
        "bullish_percent": bullish_percent,
        "bearish_percent": bearish_percent,
        "market_state": market_state,
        "current_signal": current_signal,
        "reasons": reasons,
        "ema_bullish": ema_bullish,
        "ema_bearish": ema_bearish,
        "ma_bullish": ma_bullish,
        "ma_bearish": ma_bearish,
        "vwap": vwap
    }


def decide_surfer_action(config, positions, market_context):
    market_context = normalize_signal(market_context)
    minimum_signals = int(config["entry_rules"].get("minimum_signals", 3))
    minimum_confidence = int(config.get("minimum_confidence", 2))
    minimum_dominance_percent = int(config.get("minimum_dominance_percent", 60))
    hard_stop_percent = float(config["strategy"].get("hard_stop_percent", 20))
    trailing_stop_percent = float(config["strategy"].get("trailing_stop_percent", 15))
    confidence = market_context.get("confidence", 0)
    dominance_percent = float(market_context.get("dominance_percent") or 0)

    if positions:
        pos = positions[0]
        symbol = pos.get("symbol", "")
        qty = float(pos.get("quantity", 0) or 0)
        cost_basis = float(pos.get("cost_basis", 0) or 0)
        quote = get_market_quote(symbol)
        current_price = get_quote_price(quote)
        entry_price = option_entry_price(cost_basis, qty) if len(symbol) > 6 and qty else cost_basis / qty if qty else 0

        with BOT_LOCK:
            peak = max(BOT_STATE["position_peaks"].get(symbol, entry_price), current_price or entry_price)
            BOT_STATE["position_peaks"][symbol] = peak

        pnl_percent = ((current_price - entry_price) / entry_price) * 100 if current_price and entry_price else 0
        trailing_stop_price = peak * (1 - trailing_stop_percent / 100) if peak else 0
        trailing_drawdown = ((peak - current_price) / peak) * 100 if current_price and peak else 0
        trailing_stop_active = trailing_stop_price >= entry_price

        if pnl_percent <= -hard_stop_percent:
            return "SELL", ["Hard stop hit", f"P/L {pnl_percent:.2f}%"]
        if trailing_stop_active and trailing_drawdown >= trailing_stop_percent:
            return "SELL", ["Trailing stop hit", f"Drawdown {trailing_drawdown:.2f}%"]

        trailing_reason = "Trailing stop not hit" if trailing_stop_active else "Trailing stop inactive"
        return "HOLD", ["Hard stop not hit", trailing_reason] + market_context.get("reasons", [])

    if (
        market_context.get("bullish_score", 0) >= minimum_signals
        and confidence >= minimum_confidence
        and dominance_percent >= minimum_dominance_percent
    ):
        return "BUY CALL", market_context.get("reasons", [])
    if (
        market_context.get("bearish_score", 0) >= minimum_signals
        and confidence >= minimum_confidence
        and dominance_percent >= minimum_dominance_percent
    ):
        return "BUY PUT", market_context.get("reasons", [])

    return "DO NOTHING", market_context.get("reasons", [])


def calculate_surfer_signal(config, positions=None):
    market_context = normalize_signal(build_market_context(config, positions))
    decision, decision_reasons = decide_surfer_action(config, positions or [], market_context)
    market_context["decision"] = decision
    market_context["decision_reasons"] = decision_reasons
    return normalize_signal(market_context)


def normalize_signal(signal):
    signal = dict(signal or {})
    tick_statistics = dict(signal.get("tick_statistics") or {})
    green_percent = signal.get("green_percent", tick_statistics.get("green_percent", signal.get("bullish_percent", 0)))
    red_percent = signal.get("red_percent", tick_statistics.get("red_percent", signal.get("bearish_percent", 0)))
    reasons = signal.get("decision_reasons") or signal.get("reasons") or ["Need more market context"]

    signal.setdefault("price", None)
    signal.setdefault("bullish_score", 0)
    signal.setdefault("bearish_score", 0)
    signal.setdefault("confidence", 0)
    signal.setdefault("dominance_percent", calculate_dominance_percent(signal.get("bullish_score", 0), signal.get("bearish_score", 0)))
    signal.setdefault("current_signal", "NONE")
    signal.setdefault("market_state", "UNKNOWN")
    signal.setdefault("decision", "DO NOTHING")
    signal.setdefault("decision_reasons", reasons)
    signal.setdefault("reasons", reasons)
    signal.setdefault("level_distances", {})
    signal.setdefault("levels", {})
    signal.setdefault("current_pl", 0)
    signal.setdefault("distance_to_trailing_stop", None)
    signal.setdefault("current_range_size", 0)
    signal.setdefault("bullish_percent", green_percent)
    signal.setdefault("bearish_percent", red_percent)
    signal.setdefault("green_percent", green_percent)
    signal.setdefault("red_percent", red_percent)
    signal.setdefault("tick_statistics", {
        "green_ticks": tick_statistics.get("green_ticks", 0),
        "red_ticks": tick_statistics.get("red_ticks", 0),
        "green_percent": green_percent,
        "red_percent": red_percent
    })
    return signal


def format_market_reason_log(market_context):
    market_context = normalize_signal(market_context)
    reasons = market_context.get("decision_reasons") or market_context.get("reasons", [])
    breakdown_lines, bullish_breakdown_score, bearish_breakdown_score = format_indicator_breakdown(reasons)
    lines = [
        f"Market State: {market_context.get('market_state', 'UNKNOWN')}",
        f"Bullish Score: {market_context.get('bullish_score', 0)} / 10",
        f"Bearish Score: {market_context.get('bearish_score', 0)} / 10",
        f"Confidence: {market_context.get('confidence', 0)}",
        f"Dominance: {float(market_context.get('dominance_percent') or 0):.1f}%",
        f"Decision: {market_context.get('decision', 'DO NOTHING')}",
        "",
        "Indicator Breakdown"
    ]
    lines.extend(breakdown_lines)
    lines.extend([
        "----------------------------",
        f"Bullish Score: {bullish_breakdown_score} / 10",
        f"Bearish Score: {bearish_breakdown_score} / 10",
        "",
        "Reason Log"
    ])
    lines.extend(reasons)
    return lines[-40:]


def format_indicator_breakdown(reasons):
    reason_text = "\n".join(str(reason) for reason in reasons)
    rules = [
        ("EMA Alignment", "EMA aligned bullish", "EMA aligned bearish"),
        ("MA Alignment", "MA aligned bullish", "MA aligned bearish"),
        ("MACD", "MACD bullish", "MACD bearish"),
        ("VWAP", "Price above VWAP", "Price below VWAP"),
        ("Volume Confirmation", "Volume confirmation bullish", "Volume confirmation bearish"),
        ("Green/Red Tick Threshold", "Green ticks", "Red ticks"),
        ("Previous Day High/Low", "Broke previous day high", "Broke previous day low"),
        ("Previous Week High/Low", "Broke previous week high", "Broke previous week low"),
        ("Last Hour High/Low", "Broke last hour high", "Broke last hour low"),
        ("Opening Range High/Low", "Broke opening range high", "Broke opening range low")
    ]

    lines = []
    bullish_score = 0
    bearish_score = 0
    for label, bullish_phrase, bearish_phrase in rules:
        bullish_hit = bullish_phrase in reason_text
        bearish_hit = bearish_phrase in reason_text
        bullish_points = 1 if bullish_hit else 0
        bearish_points = 1 if bearish_hit else 0
        bullish_score += bullish_points
        bearish_score += bearish_points
        status = "✅" if bullish_hit or bearish_hit else "❌"
        direction = "bullish" if bullish_hit else "bearish" if bearish_hit else ""
        direction_label = f" {direction}" if direction else ""
        points = bullish_points or bearish_points
        lines.append(f"{label:<28} {status} +{points}{direction_label}")

    return lines, bullish_score, bearish_score


def update_bot_signal_state(signal, call_cost=None, put_cost=None):
    signal = normalize_signal(signal)
    with BOT_LOCK:
        BOT_STATE["bullish_score"] = signal.get("bullish_score", 0)
        BOT_STATE["bearish_score"] = signal.get("bearish_score", 0)
        BOT_STATE["confidence"] = signal.get("confidence", 0)
        BOT_STATE["dominance_percent"] = signal.get("dominance_percent", 0)
        BOT_STATE["bullish_percent"] = signal.get("bullish_percent", signal.get("green_percent", 0))
        BOT_STATE["bearish_percent"] = signal.get("bearish_percent", signal.get("red_percent", 0))
        BOT_STATE["current_signal"] = signal.get("current_signal", "NONE")
        BOT_STATE["next_call_cost"] = call_cost
        BOT_STATE["next_put_cost"] = put_cost
        BOT_STATE["market_state"] = signal.get("market_state", "UNKNOWN")
        BOT_STATE["market_context"] = signal
        BOT_STATE["level_distances"] = signal.get("level_distances", {})
        BOT_STATE["reason_log"] = format_market_reason_log(signal)


def log_accepted_trade(action, symbol, qty, price, pnl=""):
    log_trade(action, symbol, qty, price, pnl, source="BOT", market_context=current_market_context_snapshot())
    add_bot_reason(f"{action} logged {symbol} qty {qty} price {price}")


def execute_entry_buy(config, decision, side, contract, contracts, reference_price, market_context, label="ENTRY"):
    if not option_market_is_open():
        add_bot_reason(f"{label} skipped: options market is closed")
        log_bot_audit(
            "SKIP",
            decision,
            config.get("symbol", ""),
            market_context,
            config,
            option_symbol=contract.get("symbol", ""),
            skip_reason="options market closed"
        )
        return False

    ok, order_status, status, text = submit_and_parse_option_order(
        contract["symbol"],
        contracts,
        "buy_to_open",
        label
    )
    order_id = extract_order_id(text)

    if ok:
        entry_price, entry_price_source, estimated_entry_price = resolve_actual_entry_price(
            contract["symbol"],
            contracts,
            reference_price
        )
        trade_row = log_trade(
            "BUY",
            contract["symbol"],
            contracts,
            entry_price,
            source="BOT",
            market_context=market_context,
            entry_price_source=entry_price_source,
            estimated_entry_price=estimated_entry_price
        )
        log_bot_audit(
            decision,
            decision,
            config.get("symbol", ""),
            market_context,
            config,
            option_symbol=contract["symbol"],
            entry_grade=trade_row.get("EntryGrade", ""),
            entry_price=entry_price if entry_price_source == "BROKER_COST_BASIS" else "",
            entry_price_source=entry_price_source,
            estimated_entry_price=estimated_entry_price,
            order_status=order_status,
            order_id=order_id,
            spy_price_at_entry=market_context.get("price", ""),
            market_state_at_entry=market_context.get("market_state", ""),
            reason_log=market_context.get("decision_reasons", [])
        )
        add_bot_reason(f"BUY logged {contract['symbol']} qty {contracts} price {entry_price} source {entry_price_source}")
        add_bot_reason(f"SIGNAL entered {side}: {'; '.join(market_context.get('decision_reasons', []))}")
        return True

    add_bot_reason(f"SIGNAL entry rejected or not accepted: {order_status}")
    skip_reason = order_status if order_status == "Max Open Contracts reached" else "entry rejected or not accepted"
    log_bot_audit(
        "SKIP",
        decision,
        config.get("symbol", ""),
        market_context,
        config,
        option_symbol=contract.get("symbol", ""),
        skip_reason=skip_reason,
        order_status=order_status,
        order_id=order_id
    )
    return False


def try_surfer_entry(config, positions, market_context, call, put):
    if positions:
        add_bot_reason("SIGNAL no entry: already holding one position")
        log_bot_audit("SKIP", market_context.get("decision", "DO NOTHING"), config.get("symbol", ""), market_context, config, skip_reason="already holding one position")
        return

    if not is_after_decision_time(config):
        add_bot_reason("SIGNAL no entry: waiting for decision_time")
        log_bot_audit("SKIP", market_context.get("decision", "DO NOTHING"), config.get("symbol", ""), market_context, config, skip_reason="waiting for decision_time")
        return

    trades_today, spent_today = sync_trade_limits_from_file(config)
    max_trades = int(config["entry_rules"].get("max_trades_per_day", 10))

    if trades_today >= max_trades:
        add_bot_reason("SIGNAL no entry: max trades reached")
        log_bot_audit("SKIP", market_context.get("decision", "DO NOTHING"), config.get("symbol", ""), market_context, config, skip_reason="max trades reached")
        return

    last_trade_timestamp, cooldown_remaining_seconds = get_cooldown_state(config)
    if cooldown_remaining_seconds > 0:
        add_bot_reason(f"SIGNAL no entry: cooldown active {cooldown_remaining_seconds}s remaining")
        log_bot_audit(
            "SKIP",
            market_context.get("decision", "DO NOTHING"),
            config.get("symbol", ""),
            market_context,
            config,
            skip_reason="cooldown active"
        )
        return

    contracts = int(config.get("contracts", 1))
    bot_budget = float(config.get("bot_budget", 100))
    max_contract_price = float(config.get("max_contract_price", 1))
    decision = market_context.get("decision", "DO NOTHING")

    if decision == "BUY CALL" and not config["entry_rules"].get("allow_calls", True):
        add_bot_reason("SIGNAL no entry: calls disabled")
        log_bot_audit("SKIP", decision, config.get("symbol", ""), market_context, config, skip_reason="calls disabled")
        return

    if decision == "BUY PUT" and not config["entry_rules"].get("allow_puts", True):
        add_bot_reason("SIGNAL no entry: puts disabled")
        log_bot_audit("SKIP", decision, config.get("symbol", ""), market_context, config, skip_reason="puts disabled")
        return

    side = "CALL" if decision == "BUY CALL" else "PUT" if decision == "BUY PUT" else "NONE"
    contract = select_entry_contract(config, decision, call, put)

    if not contract:
        add_bot_reason(f"SIGNAL no entry: {decision}")
        log_bot_audit("DO NOTHING", decision, config.get("symbol", ""), market_context, config, skip_reason="no eligible contract")
        return

    ask = get_option_trade_price(contract)
    if ask is None:
        add_bot_reason(f"BUDGET CHECK skipped {side}: no option price")
        log_bot_audit("SKIP", decision, config.get("symbol", ""), market_context, config, skip_reason="no option price")
        return

    real_cost = ask * 100 * contracts
    print("BUDGET CHECK")
    print("side:", side)
    print("ask:", ask)
    print("contracts:", contracts)
    print("real_cost:", real_cost)
    print("bot_budget:", bot_budget)
    print("spent_today:", spent_today)

    if ask > max_contract_price:
        add_bot_reason(f"BUDGET CHECK skipped {side}: ask {ask} > max_contract_price {max_contract_price}")
        log_bot_audit("SKIP", decision, config.get("symbol", ""), market_context, config, option_symbol=contract.get("symbol", ""), skip_reason="contract price above max")
        return

    if real_cost > bot_budget or spent_today + real_cost > bot_budget:
        add_bot_reason(f"BUDGET CHECK skipped {side}: cost {real_cost:.2f} exceeds budget")
        log_bot_audit("SKIP", decision, config.get("symbol", ""), market_context, config, option_symbol=contract.get("symbol", ""), skip_reason="budget exceeded")
        return

    if config.get("option_momentum_confirmation_enabled", True):
        create_pending_entry(config, decision, side, contract, contracts, ask, market_context)
        return

    execute_entry_buy(config, decision, side, contract, contracts, ask, market_context, label="ENTRY")


def process_pending_entry(config):
    pending = get_pending_entry()
    if not pending.get("active"):
        return False

    decision = pending.get("decision", "DO NOTHING")
    side = pending.get("direction", "")
    option_symbol = pending.get("option_symbol", "")
    contract = pending.get("contract") or {"symbol": option_symbol}
    contracts = int(pending.get("contracts") or config.get("contracts", 1))
    market_context = pending.get("market_context") or current_market_context_snapshot()
    start_price = safe_float(pending.get("starting_option_price"))
    confirmation_price = safe_float(pending.get("confirmation_price"))

    if not config.get("bot_enabled"):
        pending["active"] = False
        pending["status"] = "CANCELLED"
        pending["reason"] = "bot disabled"
        refresh_pending_time_remaining(pending)
        set_pending_entry(pending)
        add_bot_reason("PENDING BUY cancelled: bot disabled")
        log_bot_audit("SKIP", decision, config.get("symbol", ""), market_context, config, option_symbol=option_symbol, skip_reason="pending entry cancelled: bot disabled")
        return True

    if not option_market_is_open():
        pending["active"] = False
        pending["status"] = "CANCELLED"
        pending["reason"] = "options market closed"
        refresh_pending_time_remaining(pending)
        set_pending_entry(pending)
        add_bot_reason("PENDING BUY cancelled: options market closed")
        log_bot_audit("SKIP", decision, config.get("symbol", ""), market_context, config, option_symbol=option_symbol, skip_reason="pending entry cancelled: options market closed")
        return True

    quote = get_market_quote(option_symbol)
    current_price = get_quote_price(quote)
    pending["current_option_price"] = current_price
    refresh_pending_time_remaining(pending)

    if time.time() >= float(pending.get("expires_epoch") or 0):
        pending["active"] = False
        pending["status"] = "CANCELLED"
        pending["reason"] = "confirmation timeout expired"
        set_pending_entry(pending)
        add_bot_reason("PENDING BUY cancelled: confirmation timeout expired")
        log_bot_audit("SKIP", decision, config.get("symbol", ""), market_context, config, option_symbol=option_symbol, current_price=current_price, skip_reason="pending entry cancelled: confirmation timeout expired")
        return True

    if current_price is None:
        pending["status"] = "WAITING FOR MOMENTUM"
        pending["reason"] = "option quote unavailable"
        set_pending_entry(pending)
        print("PENDING ENTRY")
        print("status:", pending["status"])
        print("reason:", pending["reason"])
        return True

    print("PENDING ENTRY")
    print("option_symbol:", option_symbol)
    print("direction:", side)
    print("starting_option_price:", start_price)
    print("required_confirmation_percent:", pending.get("required_confirmation_percent"))
    print("confirmation_price:", confirmation_price)
    print("current_option_price:", current_price)
    print("time_remaining_seconds:", pending.get("time_remaining_seconds"))

    if current_price < start_price:
        pending["active"] = False
        pending["status"] = "CANCELLED"
        pending["reason"] = "option price fell before confirmation"
        set_pending_entry(pending)
        add_bot_reason(f"PENDING BUY cancelled: option fell {current_price:.2f} < start {start_price:.2f}")
        log_bot_audit("SKIP", decision, config.get("symbol", ""), market_context, config, option_symbol=option_symbol, current_price=current_price, skip_reason="pending entry cancelled: option price fell before confirmation")
        return True

    if current_price >= confirmation_price:
        pending["status"] = "CONFIRMED"
        pending["reason"] = "option momentum confirmed"
        pending["active"] = False
        set_pending_entry(pending)
        add_bot_reason(f"PENDING BUY confirmed: {current_price:.2f} >= {confirmation_price:.2f}")
        execute_entry_buy(config, decision, side, contract, contracts, current_price, market_context, label="MOMENTUM ENTRY")
        return True

    pending["status"] = "WAITING FOR MOMENTUM"
    pending["reason"] = "waiting for option price confirmation"
    set_pending_entry(pending)
    return True


def try_surfer_exit(config, positions, market_context):
    if not positions:
        log_bot_audit("DO NOTHING", market_context.get("decision", "DO NOTHING"), config.get("symbol", ""), market_context, config, skip_reason="no open position")
        return False

    decision = market_context.get("decision", "DO NOTHING")
    if decision != "SELL":
        add_bot_reason(f"EXIT hold: {'; '.join(market_context.get('decision_reasons', []))}")
        log_bot_audit("DO NOTHING", decision, config.get("symbol", ""), market_context, config, skip_reason="hold current position")
        return False

    for pos in positions:
        symbol = pos.get("symbol", "")
        if len(symbol) <= 6:
            continue

        qty = int(float(pos.get("quantity", 1)))
        cost_basis = float(pos.get("cost_basis", 0) or 0)
        entry_price = option_entry_price(cost_basis, qty)
        quote = get_market_quote(symbol)
        current_price = get_quote_price(quote) or entry_price

        exit_reasons = market_context.get("decision_reasons", [])
        reason = "; ".join(exit_reasons)
        print("EXIT REASON:", reason)

        if not option_market_is_open():
            add_bot_reason("EXIT skipped: options market is closed")
            log_bot_audit("SKIP", decision, config.get("symbol", ""), market_context, config, option_symbol=symbol, skip_reason="options market closed", exit_reason=reason)
            return True

        ok, order_status, status, text = submit_and_parse_option_order(
            symbol,
            qty,
            "sell_to_close",
            "EXIT"
        )
        order_id = extract_order_id(text)

        if ok:
            pnl = option_pnl(current_price, cost_basis, qty)
            entry_row = find_last_buy(symbol)
            trade_row = log_trade("SELL", symbol, qty, current_price, pnl, source="BOT", market_context=market_context)
            pnl_percent = ((current_price - entry_price) / entry_price) * 100 if entry_price else 0
            with BOT_LOCK:
                max_profit = BOT_STATE["position_max_profit"].get(symbol, pnl)
                max_drawdown = BOT_STATE["position_max_drawdown"].get(symbol, pnl)
            log_bot_audit(
                "SELL",
                decision,
                config.get("symbol", ""),
                market_context,
                config,
                option_symbol=symbol,
                exit_grade=trade_row.get("ExitGrade", ""),
                pnl=pnl,
                pnl_percent=pnl_percent,
                current_price=current_price,
                entry_price=entry_price,
                current_value=option_current_value(current_price, qty),
                cost_basis=cost_basis,
                exit_reason=reason,
                order_status=order_status,
                order_id=order_id,
                max_unrealized_profit=max_profit,
                max_drawdown=max_drawdown,
                hold_time=trade_row.get("HoldTime", ""),
                spy_price_at_exit=market_context.get("price", ""),
                market_state_at_exit=market_context.get("market_state", "")
            )
            update_last_trade_review({
                "entry_reason": entry_row.get("GradeReason", "") if entry_row else "",
                "exit_reason": reason,
                "entry_grade": entry_row.get("EntryGrade", "") if entry_row else "",
                "exit_grade": trade_row.get("ExitGrade", ""),
                "final_pl": pnl,
                "max_profit": max_profit,
                "max_drawdown": max_drawdown,
                "hold_time": trade_row.get("HoldTime", ""),
                "human_or_bot": "BOT"
            })
            add_bot_reason(f"EXIT sold {symbol}: {reason}")
            with BOT_LOCK:
                BOT_STATE["position_peaks"].pop(symbol, None)
                BOT_STATE["position_max_profit"].pop(symbol, None)
                BOT_STATE["position_max_drawdown"].pop(symbol, None)
            return True
        else:
            add_bot_reason(f"EXIT rejected or not accepted: {order_status}")
            log_bot_audit("SKIP", decision, config.get("symbol", ""), market_context, config, option_symbol=symbol, skip_reason="exit rejected or not accepted", exit_reason=reason, order_status=order_status, order_id=order_id)
            return True

    return False


def fast_exit_audit_fields(
    symbol,
    qty,
    cost_basis,
    entry_price,
    current_price,
    peak,
    trailing_stop_percent,
    trailing_stop_price,
    trailing_drawdown,
    stop_armed,
    no_sell_reason="",
    sell_trigger_reason=""
):
    return {
        "option_symbol": symbol,
        "entry_price": entry_price,
        "current_price": current_price,
        "current_value": option_current_value(current_price, qty),
        "cost_basis": cost_basis,
        "highest_option_price_since_entry": peak,
        "trailing_stop_percent": trailing_stop_percent,
        "calculated_stop_price": trailing_stop_price,
        "stop_armed": stop_armed,
        "no_sell_reason": no_sell_reason,
        "sell_trigger_reason": sell_trigger_reason,
        "trailing_drawdown_percent": trailing_drawdown
    }


def fast_exit_poll(config, positions):
    tick_started_at = time.perf_counter()
    set_last_error(None)
    if not positions:
        record_tick_finished(tick_started_at)
        return False

    pos = positions[0]
    symbol = pos.get("symbol", "")
    if len(symbol) <= 6:
        market_context = current_market_context_snapshot()
        reason = "Fast exit skipped: open position is not an option contract"
        print("FAST EXIT POLL")
        print("symbol:", symbol)
        print("no_sell_reason:", reason)
        log_bot_audit(
            "SKIP",
            "HOLD",
            config.get("symbol", ""),
            market_context,
            config,
            option_symbol=symbol,
            reason_log=[reason],
            skip_reason=reason,
            no_sell_reason=reason
        )
        record_tick_finished(tick_started_at)
        return False

    qty = int(float(pos.get("quantity", 1)))
    cost_basis = float(pos.get("cost_basis", 0) or 0)
    entry_price = option_entry_price(cost_basis, qty)
    quote = get_market_quote(symbol)
    current_price = get_quote_price(quote) or entry_price
    hard_stop_percent = float(config["strategy"].get("hard_stop_percent", 20))
    trailing_stop_percent = float(config["strategy"].get("trailing_stop_percent", 15))
    pnl = option_pnl(current_price, cost_basis, qty)
    pnl_percent = ((current_price - entry_price) / entry_price) * 100 if entry_price else 0

    with BOT_LOCK:
        peak = max(BOT_STATE["position_peaks"].get(symbol, entry_price), current_price)
        BOT_STATE["position_peaks"][symbol] = peak
        BOT_STATE["position_max_profit"][symbol] = max(BOT_STATE["position_max_profit"].get(symbol, pnl), pnl)
        BOT_STATE["position_max_drawdown"][symbol] = min(BOT_STATE["position_max_drawdown"].get(symbol, pnl), pnl)
        BOT_STATE["thread_alive"] = True

    hard_stop_price = entry_price * (1 - hard_stop_percent / 100)
    trailing_stop_price = peak * (1 - trailing_stop_percent / 100)
    trailing_drawdown = ((peak - current_price) / peak) * 100 if peak else 0
    distance_to_trailing_stop = current_price - trailing_stop_price
    stop_armed = trailing_stop_price >= entry_price
    no_sell_reason = ""
    sell_trigger_reason = ""
    decision = "HOLD"
    reasons = [
        "Fast exit poll",
        f"Entry {entry_price:.2f}",
        f"Current {current_price:.2f}",
        f"Peak {peak:.2f}",
        f"Hard stop price {hard_stop_price:.2f}",
        f"Trailing stop price {trailing_stop_price:.2f}",
        f"Drawdown {trailing_drawdown:.2f}%"
    ]

    if pnl_percent <= -hard_stop_percent:
        decision = "SELL"
        sell_trigger_reason = f"Hard stop hit: P/L {pnl_percent:.2f}% <= -{hard_stop_percent:.2f}%"
        reasons = ["Hard stop hit", f"P/L {pnl_percent:.2f}% <= -{hard_stop_percent:.2f}%"] + reasons
    elif stop_armed and trailing_drawdown >= trailing_stop_percent:
        decision = "SELL"
        sell_trigger_reason = f"Trailing stop hit: drawdown {trailing_drawdown:.2f}% >= {trailing_stop_percent:.2f}%"
        reasons = ["Trailing stop hit", f"Drawdown {trailing_drawdown:.2f}% >= {trailing_stop_percent:.2f}%"] + reasons
    else:
        if stop_armed:
            no_sell_reason = f"Hard stop not hit; trailing stop not hit; stop_armed={stop_armed}; drawdown {trailing_drawdown:.2f}% < {trailing_stop_percent:.2f}%"
            reasons = [
                "Trailing stop not hit.",
                f"Drawdown {trailing_drawdown:.2f}% < {trailing_stop_percent:.2f}%."
            ] + reasons
        else:
            no_sell_reason = f"Hard stop not hit; trailing stop inactive; stop_armed={stop_armed}; trailing_stop_price {trailing_stop_price:.2f} <= entry {entry_price:.2f}"
            reasons = [
                "Trailing stop inactive.",
                f"Trailing stop price {trailing_stop_price:.2f} <= entry {entry_price:.2f}."
            ] + reasons

    market_context = current_market_context_snapshot()
    market_context.update({
        "decision": decision,
        "decision_reasons": reasons,
        "current_pl": pnl,
        "distance_to_trailing_stop": distance_to_trailing_stop
    })

    with BOT_LOCK:
        BOT_STATE["market_context"] = dict(market_context)
        BOT_STATE["last_action"] = f"FAST EXIT {decision}: {reasons[0]}"

    print("FAST EXIT POLL")
    print("symbol:", symbol)
    print("entry_price:", entry_price)
    print("current_option_price:", current_price)
    print("highest_option_price_since_entry:", peak)
    print("hard_stop_price:", hard_stop_price)
    print("trailing_stop_percent:", trailing_stop_percent)
    print("calculated_stop_price:", trailing_stop_price)
    print("drawdown_percent:", trailing_drawdown)
    print("stop_armed:", stop_armed)
    print("no_sell_reason:", no_sell_reason)
    print("sell_trigger_reason:", sell_trigger_reason)
    print("decision:", decision)

    if decision != "SELL":
        log_bot_audit(
            "DO NOTHING",
            decision,
            config.get("symbol", ""),
            market_context,
            config,
            reason_log=reasons,
            skip_reason="fast exit hold",
            **fast_exit_audit_fields(
                symbol,
                qty,
                cost_basis,
                entry_price,
                current_price,
                peak,
                trailing_stop_percent,
                trailing_stop_price,
                trailing_drawdown,
                stop_armed,
                no_sell_reason=no_sell_reason
            )
        )
        record_tick_finished(tick_started_at)
        return False

    reason = "; ".join(reasons)
    print("EXIT REASON:", reason)

    if not option_market_is_open():
        add_bot_reason("FAST EXIT skipped: options market is closed")
        log_bot_audit(
            "SKIP",
            decision,
            config.get("symbol", ""),
            market_context,
            config,
            reason_log=reasons,
            skip_reason="options market closed",
            exit_reason=reason,
            **fast_exit_audit_fields(
                symbol,
                qty,
                cost_basis,
                entry_price,
                current_price,
                peak,
                trailing_stop_percent,
                trailing_stop_price,
                trailing_drawdown,
                stop_armed,
                sell_trigger_reason=sell_trigger_reason
            )
        )
        record_tick_finished(tick_started_at)
        return True

    ok, order_status, status, text = submit_and_parse_option_order(
        symbol,
        qty,
        "sell_to_close",
        "FAST EXIT"
    )
    order_id = extract_order_id(text)

    if ok:
        entry_row = find_last_buy(symbol)
        trade_row = log_trade("SELL", symbol, qty, current_price, pnl, source="BOT", market_context=market_context)
        with BOT_LOCK:
            max_profit = BOT_STATE["position_max_profit"].get(symbol, pnl)
            max_drawdown = BOT_STATE["position_max_drawdown"].get(symbol, pnl)
        log_bot_audit(
            "SELL",
            decision,
            config.get("symbol", ""),
            market_context,
            config,
            exit_grade=trade_row.get("ExitGrade", ""),
            pnl=pnl,
            pnl_percent=pnl_percent,
            exit_reason=reason,
            order_status=order_status,
            order_id=order_id,
            max_unrealized_profit=max_profit,
            max_drawdown=max_drawdown,
            hold_time=trade_row.get("HoldTime", ""),
            spy_price_at_exit=market_context.get("price", ""),
            market_state_at_exit=market_context.get("market_state", ""),
            reason_log=reasons,
            **fast_exit_audit_fields(
                symbol,
                qty,
                cost_basis,
                entry_price,
                current_price,
                peak,
                trailing_stop_percent,
                trailing_stop_price,
                trailing_drawdown,
                stop_armed,
                sell_trigger_reason=sell_trigger_reason
            )
        )
        update_last_trade_review({
            "entry_reason": entry_row.get("GradeReason", "") if entry_row else "",
            "exit_reason": reason,
            "entry_grade": entry_row.get("EntryGrade", "") if entry_row else "",
            "exit_grade": trade_row.get("ExitGrade", ""),
            "final_pl": pnl,
            "max_profit": max_profit,
            "max_drawdown": max_drawdown,
            "hold_time": trade_row.get("HoldTime", ""),
            "human_or_bot": "BOT"
        })
        add_bot_reason(f"FAST EXIT sold {symbol}: {reason}")
        with BOT_LOCK:
            BOT_STATE["position_peaks"].pop(symbol, None)
            BOT_STATE["position_max_profit"].pop(symbol, None)
            BOT_STATE["position_max_drawdown"].pop(symbol, None)
        record_tick_finished(tick_started_at)
        return True

    add_bot_reason(f"FAST EXIT rejected or not accepted: {order_status}")
    log_bot_audit(
        "SKIP",
        decision,
        config.get("symbol", ""),
        market_context,
        config,
        reason_log=reasons,
        skip_reason="fast exit rejected or not accepted",
        exit_reason=reason,
        order_status=order_status,
        order_id=order_id,
        **fast_exit_audit_fields(
            symbol,
            qty,
            cost_basis,
            entry_price,
            current_price,
            peak,
            trailing_stop_percent,
            trailing_stop_price,
            trailing_drawdown,
            stop_armed,
            sell_trigger_reason=sell_trigger_reason
        )
    )
    record_tick_finished(tick_started_at)
    return True


def surfer_bot_tick(allow_entry=True):
    tick_started_at = time.perf_counter()
    set_last_error(None)
    config = load_config()
    sync_trade_limits_from_file(config)

    if config.get("strategy_mode") != "SURFER":
        print("BOT TICK")
        print("SURFER disabled: strategy_mode is not SURFER")
        record_tick_finished(tick_started_at)
        return

    symbol = config.get("symbol", "SPY")
    quote = get_market_quote(symbol)
    price = get_quote_price(quote)

    print("BOT TICK")
    print("thread alive:", threading.current_thread().is_alive())
    print("symbol:", symbol)
    print("current price:", price)

    if price is None:
        add_bot_reason("BOT TICK skipped: quote unavailable")
        record_tick_finished(tick_started_at)
        return

    volume = None
    try:
        volume = float(quote.get("volume")) if quote.get("volume") is not None else None
    except:
        volume = None

    with BOT_LOCK:
        BOT_STATE["samples"].append({
            "time": market_now(),
            "price": price,
            "volume": volume
        })
        BOT_STATE["samples"] = BOT_STATE["samples"][-500:]
        BOT_STATE["thread_alive"] = True
        BOT_STATE["samples_length"] = len(BOT_STATE["samples"])

    print("samples length:", len(BOT_STATE["samples"]))

    call = None
    put = None
    if allow_entry:
        call = select_atm_contract(symbol, "CALL")
        put = select_atm_contract(symbol, "PUT")
        call_price = get_option_trade_price(call)
        put_price = get_option_trade_price(put)
        contracts = int(config.get("contracts", 1))
        call_cost = call_price * 100 * contracts if call_price is not None else None
        put_cost = put_price * 100 * contracts if put_price is not None else None
    else:
        with BOT_LOCK:
            call_cost = BOT_STATE["next_call_cost"]
            put_cost = BOT_STATE["next_put_cost"]
    positions = get_position()
    market_scan_started_at = time.perf_counter()
    signal = calculate_surfer_signal(config, positions)
    market_scan_ms = int((time.perf_counter() - market_scan_started_at) * 1000)
    signal_started_at = time.perf_counter()
    signal = normalize_signal(signal)
    update_bot_signal_state(signal, call_cost, put_cost)
    signal_ms = int((time.perf_counter() - signal_started_at) * 1000)
    with BOT_LOCK:
        BOT_STATE["last_market_scan_ms"] = market_scan_ms
        BOT_STATE["last_indicators_ms"] = market_scan_ms
        BOT_STATE["last_signal_ms"] = signal_ms

    print("DIRECTION")
    print("green_percent:", signal.get("bullish_percent", signal.get("green_percent", 0)))
    print("red_percent:", signal.get("bearish_percent", signal.get("red_percent", 0)))
    print("SIGNAL")
    print("bullish_score:", signal.get("bullish_score", 0))
    print("bearish_score:", signal.get("bearish_score", 0))
    print("confidence:", signal.get("confidence", 0))
    print("current_signal:", signal.get("current_signal", "NONE"))
    print("market_state:", signal.get("market_state", "UNKNOWN"))
    print("signal:", signal.get("current_signal", "NONE"))
    print("decision:", signal.get("decision", "DO NOTHING"))

    if not config.get("bot_enabled"):
        add_bot_reason("BOT scan only: bot_enabled is false")
        log_bot_audit("SKIP", signal.get("decision", "DO NOTHING"), symbol, signal, config, skip_reason="bot_enabled is false")
        record_tick_finished(tick_started_at)
        return

    exited_this_tick = try_surfer_exit(config, positions, signal)
    if exited_this_tick:
        add_bot_reason("ENTRY skipped: exit happened this tick")
        log_bot_audit("SKIP", signal.get("decision", "DO NOTHING"), symbol, signal, config, skip_reason="exit happened this tick")
        record_tick_finished(tick_started_at)
        return

    if not allow_entry:
        record_tick_finished(tick_started_at)
        return

    positions = get_position()
    try_surfer_entry(config, positions, signal, call, put)
    record_tick_finished(tick_started_at)


def surfer_bot_loop():
    with BOT_LOCK:
        if BOT_STATE["running"]:
            return
        BOT_STATE["running"] = True
        BOT_STATE["thread_alive"] = True

    print("SURFER BOT LOOP STARTED")
    last_full_position_scan = 0

    while True:
        try:
            config = load_config()
            interval = int(config.get("scanner", {}).get("interval_seconds", 60))
            exit_poll_interval_ms = clamp_int(config["strategy"].get("exit_poll_interval_ms", 1000), 100, 5000, 1000)
            positions = get_position()
            if positions:
                if get_pending_entry().get("active"):
                    clear_pending_entry("CANCELLED", "broker position exists")
                sync_trade_limits_from_file(config)
                if config.get("strategy_mode") == "SURFER" and config.get("bot_enabled"):
                    now_ts = time.time()
                    if now_ts - last_full_position_scan >= max(1, interval):
                        surfer_bot_tick(allow_entry=False)
                        last_full_position_scan = now_ts
                    else:
                        fast_exit_poll(config, positions)
                else:
                    idle_started_at = time.perf_counter()
                    with BOT_LOCK:
                        BOT_STATE["thread_alive"] = True
                    record_tick_finished(idle_started_at)
                time.sleep(exit_poll_interval_ms / 1000)
            else:
                last_full_position_scan = 0
                if get_pending_entry().get("active"):
                    process_pending_entry(config)
                    time.sleep(exit_poll_interval_ms / 1000)
                else:
                    surfer_bot_tick()
                    time.sleep(max(1, interval))
        except Exception as exc:
            set_last_error(exc)
            add_bot_reason(f"BOT ERROR {exc}")
            time.sleep(10)


def sell_all_positions():
    positions = get_position()
    if not positions:
        return False, "No open positions"

    results = []

    for pos in positions:
        symbol = pos.get("symbol")
        qty = int(float(pos.get("quantity", 1)))
        cost_basis = float(pos.get("cost_basis", 0) or 0)
        is_option = len(symbol) > 6
        entry_price = option_entry_price(cost_basis, qty) if is_option else cost_basis / qty if qty else 0
        quote = get_market_quote(symbol)
        sell_price = entry_price

        if quote:
            for key in ["last", "bid", "ask"]:
                if quote.get(key) is not None:
                    try:
                        sell_price = float(quote.get(key))
                        break
                    except:
                        pass

        multiplier = 100 if is_option else 1
        pnl = option_pnl(sell_price, cost_basis, qty) if is_option else (sell_price - entry_price) * qty

        print("SELL DEBUG")
        print("symbol:", symbol)
        print("qty:", qty)
        print("cost_basis:", cost_basis)
        print("sell_price:", sell_price)
        print("pnl:", pnl)

        if is_option:
            ok, order_status, status, text = submit_and_parse_option_order(
                symbol,
                qty,
                "sell_to_close",
                "SELL"
            )
            if ok:
                log_trade("SELL", symbol, qty, sell_price, pnl)
            results.append(text)
        else:
            data = {
                "class": "equity",
                "symbol": symbol,
                "side": "sell",
                "quantity": str(qty),
                "type": "market",
                "duration": "day",
                "tag": f"MANUAL_SELL_{symbol}"
            }

            r = requests.post(
                f"{BASE_URL}/accounts/{ACCOUNT}/orders",
                headers=headers(),
                data=data
            )
            print("SELL STATUS:", r.status_code)
            print("SELL RESPONSE:", r.text)
            ok, order_status = parse_order_response(r.status_code, r.text, "SELL")
            if ok:
                log_trade("SELL", symbol, qty, sell_price, pnl)
            results.append(r.text)

    return True, " | ".join(results)


@app.route("/api/expirations")
def api_expirations():
    config = load_config()
    symbol = request.args.get("symbol", config["symbol"]).upper()
    return jsonify({"dates": get_expirations(symbol)})


@app.route("/api/orders")
def api_orders():
    r = requests.get(
        f"{BASE_URL}/accounts/{ACCOUNT}/orders",
        headers=headers()
    )
    return r.text


@app.route("/api/bot-state")
def api_bot_state():
    config = load_config()
    sync_trade_limits_from_file(config)

    with BOT_LOCK:
        pending_entry = refresh_pending_time_remaining(dict(BOT_STATE.get("pending_entry") or default_pending_entry()))
        return jsonify({
            "bullish_score": BOT_STATE["bullish_score"],
            "bearish_score": BOT_STATE["bearish_score"],
            "confidence": BOT_STATE["confidence"],
            "dominance_percent": BOT_STATE["dominance_percent"],
            "current_signal": BOT_STATE["current_signal"],
            "last_action": BOT_STATE["last_action"],
            "trades_today": BOT_STATE["trades_today"],
            "spent_today": BOT_STATE["spent_today"],
            "budget_remaining": BOT_STATE["budget_remaining"],
            "next_call_cost": BOT_STATE["next_call_cost"],
            "next_put_cost": BOT_STATE["next_put_cost"],
            "market_state": BOT_STATE["market_state"],
            "thread_alive": BOT_STATE["thread_alive"],
            "last_tick": BOT_STATE["last_tick"],
            "samples_length": BOT_STATE["samples_length"],
            "last_trade_timestamp": BOT_STATE["last_trade_timestamp"],
            "cooldown_remaining_seconds": BOT_STATE["cooldown_remaining_seconds"],
            "last_trade_review": dict(BOT_STATE["last_trade_review"]),
            "reason_log": list(BOT_STATE["reason_log"]),
            "last_quote_time": BOT_STATE["last_quote_time"],
            "last_quote_epoch": BOT_STATE["last_quote_epoch"],
            "last_quote_latency_ms": BOT_STATE["last_quote_latency_ms"],
            "last_quote_status": BOT_STATE["last_quote_status"],
            "last_position_time": BOT_STATE["last_position_time"],
            "last_position_epoch": BOT_STATE["last_position_epoch"],
            "last_position_latency_ms": BOT_STATE["last_position_latency_ms"],
            "last_position_status": BOT_STATE["last_position_status"],
            "last_tick_epoch": BOT_STATE["last_tick_epoch"],
            "last_tick_duration_ms": BOT_STATE["last_tick_duration_ms"],
            "last_error": BOT_STATE["last_error"],
            "quote_request_count": BOT_STATE["quote_request_count"],
            "quote_latency_total_ms": BOT_STATE["quote_latency_total_ms"],
            "quote_latency_slowest_ms": BOT_STATE["quote_latency_slowest_ms"],
            "quote_failed_count": BOT_STATE["quote_failed_count"],
            "quote_rate_limited_count": BOT_STATE["quote_rate_limited_count"],
            "last_order_submit_ms": BOT_STATE["last_order_submit_ms"],
            "last_broker_confirm_ms": BOT_STATE["last_broker_confirm_ms"],
            "last_market_scan_ms": BOT_STATE["last_market_scan_ms"],
            "last_indicators_ms": BOT_STATE["last_indicators_ms"],
            "last_signal_ms": BOT_STATE["last_signal_ms"],
            "pending_entry": pending_entry,
            "config_strategy": dict(config.get("strategy", {}))
        })


@app.route("/api/developer-diagnostics")
def api_developer_diagnostics():
    config = load_config()
    positions = get_position()
    with BOT_LOCK:
        bot_snapshot = {
            "current_signal": BOT_STATE["current_signal"],
            "last_action": BOT_STATE["last_action"],
            "market_state": BOT_STATE["market_state"],
            "market_context": dict(BOT_STATE["market_context"]),
            "thread_alive": BOT_STATE["thread_alive"],
            "last_tick": BOT_STATE["last_tick"],
            "last_quote_time": BOT_STATE["last_quote_time"],
            "last_quote_epoch": BOT_STATE["last_quote_epoch"],
            "last_quote_latency_ms": BOT_STATE["last_quote_latency_ms"],
            "last_quote_status": BOT_STATE["last_quote_status"],
            "last_position_time": BOT_STATE["last_position_time"],
            "last_position_epoch": BOT_STATE["last_position_epoch"],
            "last_position_latency_ms": BOT_STATE["last_position_latency_ms"],
            "last_position_status": BOT_STATE["last_position_status"],
            "last_tick_epoch": BOT_STATE["last_tick_epoch"],
            "last_tick_duration_ms": BOT_STATE["last_tick_duration_ms"],
            "last_error": BOT_STATE["last_error"],
            "quote_request_count": BOT_STATE["quote_request_count"],
            "quote_latency_total_ms": BOT_STATE["quote_latency_total_ms"],
            "quote_latency_slowest_ms": BOT_STATE["quote_latency_slowest_ms"],
            "quote_failed_count": BOT_STATE["quote_failed_count"],
            "quote_rate_limited_count": BOT_STATE["quote_rate_limited_count"],
            "last_order_submit_ms": BOT_STATE["last_order_submit_ms"],
            "last_broker_confirm_ms": BOT_STATE["last_broker_confirm_ms"],
            "last_market_scan_ms": BOT_STATE["last_market_scan_ms"],
            "last_indicators_ms": BOT_STATE["last_indicators_ms"],
            "last_signal_ms": BOT_STATE["last_signal_ms"],
            "pending_entry": refresh_pending_time_remaining(dict(BOT_STATE.get("pending_entry") or default_pending_entry())),
            "config_strategy": dict(config.get("strategy", {}))
        }
    bot_health = get_bot_health_data(positions, bot_snapshot)
    return jsonify(get_developer_diagnostics(config, positions, bot_snapshot, bot_health))


@app.route("/api/current-position")
def api_current_position():
    positions = get_position()
    if not positions:
        return jsonify({"positions": []})

    return jsonify({
        "positions": [get_position_pl_data(pos) for pos in positions]
    })


@app.route("/api/quote")
def api_quote():
    config = load_config()
    symbol = request.args.get("symbol", config["symbol"]).upper()
    quote = get_market_quote(symbol)
    return jsonify({"quote": quote})


@app.route("/api/market-structure")
def api_market_structure():
    quote = get_market_quote(load_config()["symbol"])

    with BOT_LOCK:
        market_context = dict(BOT_STATE["market_context"])

    if not market_context:
        market_context = empty_market_context(get_quote_price(quote))

    return jsonify({
        "levels": market_context.get("levels", {}),
        "market_structure_source": market_context.get("market_structure_source"),
        "market_date": market_context.get("market_date"),
        "market_structure_last_updated": market_context.get("market_structure_last_updated"),
        "level_distances": market_context.get("level_distances", {}),
        "current_pl": market_context.get("current_pl"),
        "distance_to_trailing_stop": market_context.get("distance_to_trailing_stop"),
        "current_range_size": market_context.get("current_range_size"),
        "market_state": market_context.get("market_state"),
        "bullish_score": market_context.get("bullish_score"),
        "bearish_score": market_context.get("bearish_score"),
        "confidence": market_context.get("confidence"),
        "dominance_percent": market_context.get("dominance_percent"),
        "current_signal": market_context.get("current_signal"),
        "reasons": market_context.get("decision_reasons") or market_context.get("reasons", [])
    })


@app.route("/api/chain")
def api_chain():
    config = load_config()
    symbol = request.args.get("symbol", config["symbol"]).upper()
    expiration = request.args.get("expiration")
    option_type = request.args.get("option_type", "call")

    if not expiration:
        return jsonify({"options": []})

    chain = get_option_chain(symbol, expiration)
    filtered = []

    for opt in chain:
        if opt.get("option_type") == option_type:
            filtered.append({
                "symbol": opt.get("symbol"),
                "strike": opt.get("strike"),
                "bid": opt.get("bid"),
                "ask": opt.get("ask"),
                "last": opt.get("last"),
                "volume": opt.get("volume"),
                "open_interest": opt.get("open_interest"),
                "expiration": expiration,
                "option_type": option_type
            })

    filtered.sort(key=lambda x: float(x["strike"]))
    return jsonify({"options": filtered})


@app.route("/manual-buy-selected", methods=["POST"])
def manual_buy_selected():
    option_symbol = request.form.get("option_symbol")
    qty = int(request.form.get("manual_qty", 1))

    if option_symbol:
        ok, order_status, status, text = submit_and_parse_option_order(
            option_symbol,
            qty,
            "buy_to_open",
            "ORDER"
        )
        if ok:
            quote = get_market_quote(option_symbol)
            estimated_price = get_quote_price(quote) or ""
            entry_price, entry_price_source, estimated_entry_price = resolve_actual_entry_price(
                option_symbol,
                qty,
                estimated_price
            )
            log_trade(
                "BUY",
                option_symbol,
                qty,
                entry_price,
                entry_price_source=entry_price_source,
                estimated_entry_price=estimated_entry_price
            )

    return redirect("/")


@app.route("/manual-buy-call", methods=["POST"])
def manual_buy_call():
    config = load_config()
    contract = select_atm_contract(config["symbol"], "CALL")
    if contract:
        ok, order_status, status, text = submit_and_parse_option_order(
            contract["symbol"],
            config["contracts"],
            "buy_to_open",
            "ORDER"
        )
        if ok:
            estimated_price = get_option_trade_price(contract) or ""
            entry_price, entry_price_source, estimated_entry_price = resolve_actual_entry_price(
                contract["symbol"],
                config["contracts"],
                estimated_price
            )
            log_trade(
                "BUY",
                contract["symbol"],
                config["contracts"],
                entry_price,
                entry_price_source=entry_price_source,
                estimated_entry_price=estimated_entry_price
            )
    return redirect("/")


@app.route("/manual-buy-put", methods=["POST"])
def manual_buy_put():
    config = load_config()
    contract = select_atm_contract(config["symbol"], "PUT")
    if contract:
        ok, order_status, status, text = submit_and_parse_option_order(
            contract["symbol"],
            config["contracts"],
            "buy_to_open",
            "ORDER"
        )
        if ok:
            estimated_price = get_option_trade_price(contract) or ""
            entry_price, entry_price_source, estimated_entry_price = resolve_actual_entry_price(
                contract["symbol"],
                config["contracts"],
                estimated_price
            )
            log_trade(
                "BUY",
                contract["symbol"],
                config["contracts"],
                entry_price,
                entry_price_source=entry_price_source,
                estimated_entry_price=estimated_entry_price
            )
    return redirect("/")


@app.route("/manual-sell", methods=["POST"])
def manual_sell():
    sell_all_positions()
    return redirect("/")


@app.route("/clear-trades", methods=["POST"])
def clear_trades():
    clear_recent_trades()
    return redirect("/")


@app.route("/restore-cleared-trades", methods=["POST"])
def restore_trades():
    restore_cleared_trades()
    return redirect("/")


@app.route("/clear-bot-trades", methods=["POST"])
def clear_bot_trades():
    clear_visible_trades_by_source("BOT")
    sync_trade_limits_from_file(load_config())
    return redirect("/")


@app.route("/restore-bot-trades", methods=["POST"])
def restore_bot_trades():
    restore_visible_trades_by_source("BOT")
    sync_trade_limits_from_file(load_config())
    return redirect("/")


@app.route("/clear-human-trades", methods=["POST"])
def clear_human_trades():
    clear_visible_trades_by_source("HUMAN")
    return redirect("/")


@app.route("/restore-human-trades", methods=["POST"])
def restore_human_trades():
    restore_visible_trades_by_source("HUMAN")
    return redirect("/")


@app.route("/clear-trade-history-view", methods=["POST"])
def clear_trade_history_view_route():
    clear_trade_history_view()
    return redirect("/")


@app.route("/restore-trade-history-view", methods=["POST"])
def restore_trade_history_view_route():
    restore_trade_history_view()
    return redirect("/")


@app.route("/save-settings", methods=["POST"])
def save_settings():
    config = load_config()

    symbol_choice = request.form.get("symbol_choice", "SPY")
    custom_symbol = request.form.get("custom_symbol", "").strip().upper()

    config["symbol"] = custom_symbol if custom_symbol else symbol_choice
    config["mode"] = request.form.get("mode", "sandbox")
    config["asset_type"] = request.form.get("asset_type", "option")
    config["contracts"] = int(request.form.get("contracts", 1))
    config["bot_enabled"] = request.form.get("bot_enabled") == "on"
    config["strategy_mode"] = request.form.get("strategy_mode", "SURFER")
    config["decision_time"] = request.form.get("decision_time", "09:35")
    config["bot_budget"] = float(request.form.get("bot_budget", 100))
    config["human_daily_trading_budget"] = float(request.form.get("human_daily_trading_budget", 500))
    config["bot_starting_account_balance"] = float(request.form.get("bot_starting_account_balance", 500))
    config["human_starting_account_balance"] = float(request.form.get("human_starting_account_balance", 500))
    config["max_contract_price"] = float(request.form.get("max_contract_price", 1))
    config["max_open_contracts"] = clamp_int(request.form.get("max_open_contracts", 1), 1, 5, 1)
    config["contract_selection_mode"] = request.form.get("contract_selection_mode", "strict_atm")
    config["option_momentum_confirmation_enabled"] = request.form.get("option_momentum_confirmation_enabled") == "on"
    config["option_momentum_percent"] = max(0.1, min(20.0, safe_float(request.form.get("option_momentum_percent", 2.0), 2.0)))
    config["confirmation_timeout_seconds"] = clamp_int(request.form.get("confirmation_timeout_seconds", 10), 1, 300, 10)
    config["minimum_confidence"] = clamp_int(request.form.get("minimum_confidence", 2), 1, 10, 2)
    config["minimum_dominance_percent"] = clamp_int(request.form.get("minimum_dominance_percent", 60), 50, 100, 60)

    s = config["strategy"]
    s["ema_fast"] = int(request.form.get("ema_fast", 1))
    s["ema_medium"] = int(request.form.get("ema_medium", 5))
    s["ema_slow"] = int(request.form.get("ema_slow", 10))
    s["ma_fast"] = int(request.form.get("ma_fast", 5))
    s["ma_medium"] = int(request.form.get("ma_medium", 10))
    s["ma_slow"] = int(request.form.get("ma_slow", 20))
    s["use_macd"] = request.form.get("use_macd") == "on"
    s["use_vwap"] = request.form.get("use_vwap") == "on"
    s["use_volume"] = request.form.get("use_volume") == "on"
    s["hard_stop_percent"] = float(request.form.get("hard_stop_percent", 20))
    s["trailing_stop_percent"] = float(request.form.get("trailing_stop_percent", 15))
    s["exit_poll_interval_ms"] = clamp_int(request.form.get("exit_poll_interval_ms", 1000), 100, 5000, 1000)
    s["direction_threshold_percent"] = float(request.form.get("direction_threshold_percent", 60))

    e = config["entry_rules"]
    e["ema_alignment"] = request.form.get("ema_alignment") == "on"
    e["macd_confirmation"] = request.form.get("macd_confirmation") == "on"
    e["vwap_confirmation"] = request.form.get("vwap_confirmation") == "on"
    e["volume_confirmation"] = request.form.get("volume_confirmation") == "on"
    e["minimum_signals"] = int(request.form.get("minimum_signals", 3))
    e["allow_calls"] = request.form.get("allow_calls") == "on"
    e["allow_puts"] = request.form.get("allow_puts") == "on"
    cooldown = request.form.get("cooldown_minutes", "").strip()
    e["cooldown_minutes"] = int(cooldown) if cooldown.isdigit() else 5
    max_trades = request.form.get("max_trades_per_day", "").strip()
    e["max_trades_per_day"] = int(max_trades) if max_trades else 10

    config["scanner"]["interval_seconds"] = int(request.form.get("interval_seconds", 60))

    save_config(config)
    return redirect("/")

def average(values):
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else 0


def safe_float(value, default=0.0):
    try:
        return float(value)
    except:
        return default


def trade_source(row):
    return (row.get("Source") or "HUMAN").upper()


def trade_grade(row):
    return row.get("OverallGrade") or row.get("BotGrade") or row.get("ExitGrade") or row.get("LiveGrade") or row.get("EntryGrade") or "N/A"


def trade_entry_grade(row):
    return row.get("EntryGrade") or "N/A"


def trade_exit_grade(row):
    return row.get("ExitGrade") or "N/A"


def overall_grade_for_trades(rows):
    scores = [grade_value(trade_grade(row)) for row in rows if grade_value(trade_grade(row)) > 0]
    return grade_from_score(average(scores)) if scores else "N/A"


def trade_entry_amount(row):
    price = safe_float(row.get("Entry") or row.get("Price"))
    qty = safe_float(row.get("Qty"), 1)
    return price * qty * 100


def fmt_premium(value):
    if value in (None, ""):
        return "N/A"
    return fmt_money(safe_float(value))


def fmt_percent(value):
    if value in (None, ""):
        return "N/A"
    return f"{safe_float(value):+.2f}%"


def enrich_trade_rows(rows):
    open_buys = {}
    enriched = []

    for row in rows:
        display = dict(row)
        action = row.get("Action", "")
        source = trade_source(row)
        symbol = row.get("Symbol", "")
        key = (source, symbol)
        price = row.get("Price", "")

        display["Trader"] = source
        display["Grade"] = trade_grade(row)
        display["Entry"] = ""
        display["Exit"] = ""

        if action == "BUY":
            display["Entry"] = price
            open_buys.setdefault(key, []).append(row)
        elif action == "SELL":
            matching_buys = open_buys.get(key, [])
            buy_row = matching_buys.pop(0) if matching_buys else None
            display["Entry"] = buy_row.get("Price", "") if buy_row else ""
            if buy_row:
                display["EntryPriceSource"] = buy_row.get("EntryPriceSource", "")
                display["EstimatedEntryPrice"] = buy_row.get("EstimatedEntryPrice", "")
                if not display.get("EntryGrade"):
                    display["EntryGrade"] = buy_row.get("EntryGrade", "")
            display["Exit"] = price
            if not display["Entry"]:
                exit_price = safe_float(price, None)
                pnl = safe_float(row.get("PnL"), None)
                qty = safe_float(row.get("Qty"), None)
                if exit_price is not None and pnl is not None and qty:
                    display["Entry"] = exit_price - (pnl / (qty * 100))

        enriched.append(display)

    return enriched


def summarize_ledger(rows, source, config):
    starting_key = "bot_starting_account_balance" if source == "BOT" else "human_starting_account_balance"
    starting_balance = safe_float(config.get(starting_key), 500.0)
    today_budget_key = "bot_budget" if source == "BOT" else "human_daily_trading_budget"
    today_budget = safe_float(config.get(today_budget_key), 100.0 if source == "BOT" else 500.0)
    source_rows = [row for row in rows if trade_source(row) == source]
    enriched_rows = enrich_trade_rows(source_rows)
    sell_rows = [row for row in enriched_rows if row.get("Action") == "SELL"]
    pnl_values = [safe_float(row.get("PnL")) for row in sell_rows]
    total_pnl = sum(pnl_values)
    today = market_now().strftime("%Y-%m-%d")
    today_pnl = sum(safe_float(row.get("PnL")) for row in sell_rows if str(row.get("Time", "")).startswith(today))
    wins = [pnl for pnl in pnl_values if pnl > 0]
    trade_list = []
    running_balance = starting_balance
    _, spent_today = daily_buy_totals(rows, source, today)

    for index, row in enumerate(sell_rows, start=1):
        pnl = safe_float(row.get("PnL"))
        entry_amount = trade_entry_amount(row)
        return_percent = (pnl / entry_amount * 100) if entry_amount else 0
        running_balance += pnl
        trade_list.append({
            "number": index,
            "pnl": pnl,
            "return_percent": return_percent,
            "entry_amount": entry_amount,
            "entry_price": row.get("Entry", ""),
            "entry_price_source": row.get("EntryPriceSource", ""),
            "exit_price": row.get("Exit", ""),
            "balance": running_balance,
            "hold_time": row.get("HoldTime") or "N/A",
            "entry_grade": trade_entry_grade(row),
            "exit_grade": trade_exit_grade(row),
            "overall_grade": trade_grade(row)
        })

    return {
        "starting_balance": starting_balance,
        "current_balance": starting_balance + total_pnl,
        "net_profit": total_pnl,
        "today_pnl": today_pnl,
        "total_pnl": total_pnl,
        "today_budget": today_budget,
        "spent_today": spent_today,
        "budget_remaining": max(0, today_budget - spent_today),
        "win_rate": (len(wins) / len(sell_rows) * 100) if sell_rows else 0,
        "overall_grade": overall_grade_for_trades(sell_rows),
        "number_of_trades": len(sell_rows),
        "trade_list": trade_list
    }


def get_trade_performance():
    config = load_config()
    rows = get_recent_trades(limit=None)
    return {
        "BOT": summarize_ledger(rows, "BOT", config),
        "HUMAN": summarize_ledger(rows, "HUMAN", config)
    }


def escape_html(value):
    return html_lib.escape(str(value or ""))


def fmt_trade_price(value):
    if value in (None, ""):
        return "N/A"
    return fmt_money(safe_float(value))


def entry_source_label(trade):
    source = trade.get("EntryPriceSource", "")
    if source == "ESTIMATED_ASK":
        return " (ESTIMATED)"
    if source == "BROKER_COST_BASIS":
        return " (BROKER)"
    return ""


def parse_entry_reason_log(trade):
    reason_log = trade.get("EntryReasonLog") or ""
    if reason_log:
        return [line for line in reason_log.splitlines() if line]

    fallback = trade.get("GradeReason") or ""
    if fallback:
        return [part.strip() for part in fallback.split(";") if part.strip()]

    return []


def parse_entry_indicator_breakdown(trade):
    raw_breakdown = trade.get("EntryIndicatorBreakdown") or ""
    if raw_breakdown:
        try:
            parsed = json.loads(raw_breakdown)
            if isinstance(parsed, list):
                return parsed
        except:
            pass

    reasons = parse_entry_reason_log(trade)
    lines, _, _ = format_indicator_breakdown(reasons)
    rows = []
    for line in lines:
        status = "✅" in line
        points = 1 if "+1" in line else 0
        direction = "bullish" if "bullish" in line else "bearish" if "bearish" in line else "neutral"
        label = line.split("✅")[0].split("❌")[0].strip()
        rows.append({
            "label": label,
            "passed": status,
            "points": points,
            "direction": direction,
        })
    return rows


def render_entry_indicator_breakdown(trade):
    rows = parse_entry_indicator_breakdown(trade)
    bullish_score = 0
    bearish_score = 0
    lines = []

    for row in rows:
        label = row.get("label", "")
        points = int(row.get("points") or 0)
        direction = row.get("direction") or "neutral"
        passed = bool(row.get("passed"))
        if passed and direction == "bullish":
            bullish_score += points
        elif passed and direction == "bearish":
            bearish_score += points

        icon = "✅" if passed else "❌"
        direction_text = f" {direction}" if passed and direction in ["bullish", "bearish"] else ""
        lines.append(f"{escape_html(label)} {icon} +{points}{direction_text}")

    return "<br>".join(lines), bullish_score, bearish_score


def render_buy_trade_card(trade):
    trader = trade.get("Trader", "HUMAN")
    trader_class = trader.lower()
    reasons = parse_entry_reason_log(trade)
    breakdown_html, bullish_score, bearish_score = render_entry_indicator_breakdown(trade)
    entry_bullish_score = trade.get("EntryBullishScore") or bullish_score
    entry_bearish_score = trade.get("EntryBearishScore") or bearish_score
    entry_confidence = trade.get("EntryConfidence") or abs(int(safe_float(entry_bullish_score)) - int(safe_float(entry_bearish_score)))
    entry_dominance = trade.get("EntryDominancePercent") or calculate_dominance_percent(int(safe_float(entry_bullish_score)), int(safe_float(entry_bearish_score)))
    entry_decision = trade.get("EntryDecision") or "N/A"
    market_state = trade.get("EntryMarketState") or "N/A"

    return f"""
<div class="trade-card {trader_class}">
<b>BUY</b>
<span class="badge {trader_class}">Trader: {escape_html(trader)}</span><br>
Symbol: {escape_html(trade.get("Symbol", ""))}<br>
Qty: {escape_html(trade.get("Qty", ""))}<br>
Entry: {fmt_trade_price(trade.get("Entry"))}{entry_source_label(trade)}<br>
Entry Grade: {escape_html(trade_entry_grade(trade))}<br>
Overall Grade: {escape_html(trade_grade(trade))}<br>
Timestamp: {escape_html(trade.get("Time", ""))}<br>
<br>
Market State: {escape_html(market_state)}<br>
Bullish Score: {escape_html(entry_bullish_score)} / 10<br>
Bearish Score: {escape_html(entry_bearish_score)} / 10<br>
Confidence: {escape_html(entry_confidence)} / 10<br>
Dominance: {float(safe_float(entry_dominance)):.1f}%<br>
Decision: {escape_html(entry_decision)}<br>
<br>
Indicator Breakdown<br>
{breakdown_html}<br>
----------------------------<br>
Bullish Score: {escape_html(entry_bullish_score)} / 10<br>
Bearish Score: {escape_html(entry_bearish_score)} / 10<br>
<br>
Reason Log<br>
{escape_html(chr(10).join(reasons)).replace(chr(10), "<br>")}
</div>
"""


def render_sell_trade_card(trade):
    trader = trade.get("Trader", "HUMAN")
    trader_class = trader.lower()
    pnl = safe_float(trade.get("PnL"))
    pnl_display = fmt_money(pnl) if trade.get("PnL") not in (None, "") else "N/A"
    pnl_percent = trade.get("PnLPercent")
    if not pnl_percent and trade.get("Entry") not in (None, ""):
        entry_amount = trade_entry_amount(trade)
        pnl_percent = (pnl / entry_amount) * 100 if entry_amount else ""
    pnl_class = "good" if pnl > 0 else "bad" if pnl < 0 else ""
    exit_reason = trade.get("ExitReason") or trade.get("GradeReason") or "N/A"

    return f"""
<div class="trade-card {trader_class}">
<b>SELL</b>
<span class="badge {trader_class}">Trader: {escape_html(trader)}</span><br>
Symbol: {escape_html(trade.get("Symbol", ""))}<br>
Qty: {escape_html(trade.get("Qty", ""))}<br>
Entry: {fmt_trade_price(trade.get("Entry"))}{entry_source_label(trade)}<br>
Exit: {fmt_trade_price(trade.get("Exit"))}<br>
Peak Price: {fmt_trade_price(trade.get("PeakPrice"))}<br>
Hard Stop Price: {fmt_trade_price(trade.get("HardStopPrice"))}<br>
Trailing Stop Price: {fmt_trade_price(trade.get("TrailingStopPrice"))}<br>
Max Drawdown From Peak: {fmt_percent(trade.get("MaxDrawdownFromPeakPercent"))}<br>
PnL: <span class="{pnl_class}">{pnl_display} ({fmt_percent(pnl_percent)})</span><br>
Hold Time: {escape_html(trade.get("HoldTime") or "N/A")}<br>
Entry Grade: {escape_html(trade_entry_grade(trade))}<br>
Exit Grade: {escape_html(trade_exit_grade(trade))}<br>
Overall Grade: {escape_html(trade_grade(trade))}<br>
Exit Reason:<br>
{escape_html(exit_reason).replace(chr(10), "<br>")}<br>
Timestamp: {escape_html(trade.get("Time", ""))}
</div>
"""


def render_recent_trade_card(trade):
    if trade.get("Action") == "BUY":
        return render_buy_trade_card(trade)
    if trade.get("Action") == "SELL":
        return render_sell_trade_card(trade)

    trader = trade.get("Trader", "HUMAN")
    trader_class = trader.lower()
    pnl = safe_float(trade.get("PnL"))
    pnl_display = fmt_money(pnl) if trade.get("PnL") not in (None, "") else "N/A"
    pnl_percent = trade.get("PnLPercent")
    if not pnl_percent and trade.get("Entry") not in (None, ""):
        entry_amount = trade_entry_amount(trade)
        pnl_percent = (pnl / entry_amount) * 100 if entry_amount else ""
    pnl_class = "good" if pnl > 0 else "bad" if pnl < 0 else ""
    exit_reason = trade.get("ExitReason") or trade.get("GradeReason") or "N/A"

    return f"""
<div class="trade-card {trader_class}">
<b>{escape_html(trade.get("Action", ""))}</b>
<span class="badge {trader_class}">Trader: {escape_html(trader)}</span><br>
Symbol: {escape_html(trade.get("Symbol", ""))}<br>
Qty: {escape_html(trade.get("Qty", ""))}<br>
Entry: {fmt_trade_price(trade.get("Entry"))}{entry_source_label(trade)}<br>
Exit: {fmt_trade_price(trade.get("Exit"))}<br>
Peak Price: {fmt_trade_price(trade.get("PeakPrice"))}<br>
Hard Stop Price: {fmt_trade_price(trade.get("HardStopPrice"))}<br>
Trailing Stop Price: {fmt_trade_price(trade.get("TrailingStopPrice"))}<br>
Max Drawdown From Peak: {fmt_percent(trade.get("MaxDrawdownFromPeakPercent"))}<br>
PnL: <span class="{pnl_class}">{pnl_display} ({fmt_percent(pnl_percent)})</span><br>
Hold Time: {escape_html(trade.get("HoldTime") or "N/A")}<br>
Entry Grade: {escape_html(trade_entry_grade(trade))}<br>
Exit Grade: {escape_html(trade_exit_grade(trade))}<br>
Overall Grade: {escape_html(trade_grade(trade))}<br>
Exit Reason:<br>
{escape_html(exit_reason).replace(chr(10), "<br>")}<br>
Time: {escape_html(trade.get("Time", ""))}
</div>
"""


def render_trade_list(summary):
    if not summary["trade_list"]:
        return "No closed trades yet."

    lines = []
    for trade in summary["trade_list"]:
        pnl_class = "good" if trade["pnl"] > 0 else "bad" if trade["pnl"] < 0 else ""
        entry_label = entry_source_label({"EntryPriceSource": trade["entry_price_source"]})
        lines.append(
            f"""<div class="trade-line">#{trade["number"]} | """
            f"""<span class="{pnl_class}">{fmt_money(trade["pnl"])} ({trade["return_percent"]:+.1f}%)</span> """
            f"""| Entry: {fmt_premium(trade["entry_price"])}{entry_label} """
            f"""| Exit: {fmt_premium(trade["exit_price"])} """
            f"""| Balance: {fmt_money(trade["balance"])} """
            f"""| Hold: {escape_html(trade["hold_time"])} """
            f"""| Entry Grade: {escape_html(trade["entry_grade"])} """
            f"""| Exit Grade: {escape_html(trade["exit_grade"])} """
            f"""| Overall Grade: {escape_html(trade["overall_grade"])}</div>"""
        )
    return "".join(lines)


def render_performance_panel(label, summary):
    panel_class = label.lower()
    clear_action = "/clear-bot-trades" if label == "BOT" else "/clear-human-trades"
    restore_action = "/restore-bot-trades" if label == "BOT" else "/restore-human-trades"
    clear_text = "Clear Bot Trades" if label == "BOT" else "Clear Human Trades"
    restore_text = "Undo Clear Bot Trades" if label == "BOT" else "Undo Clear Human Trades"
    if label == "BOT":
        stats_html = f"""
Starting Account Balance: {fmt_money(summary["starting_balance"])}<br>
Current Account Balance: {fmt_money(summary["current_balance"])}<br>
Today's Trading Budget: {fmt_money(summary["today_budget"])}<br>
Today's Budget Remaining: {fmt_money(summary["budget_remaining"])}<br>
Spent Today: {fmt_money(summary["spent_today"])}<br>
Win Rate: {summary["win_rate"]:.2f}%<br>
Today's PnL: {fmt_money(summary["today_pnl"])}<br>
Total PnL: {fmt_money(summary["total_pnl"])}<br>
Overall Grade: {escape_html(summary["overall_grade"])}<br>
Number of Trades: {summary["number_of_trades"]}<br>
"""
    else:
        stats_html = f"""
Starting Account Balance: {fmt_money(summary["starting_balance"])}<br>
Current Account Balance: {fmt_money(summary["current_balance"])}<br>
Net Profit: {fmt_money(summary["net_profit"])}<br>
Today's Trading Budget: {fmt_money(summary["today_budget"])}<br>
Today's Budget Remaining: {fmt_money(summary["budget_remaining"])}<br>
Spent Today: {fmt_money(summary["spent_today"])}<br>
Win Rate: {summary["win_rate"]:.2f}%<br>
Total PnL: {fmt_money(summary["total_pnl"])}<br>
Number of Trades: {summary["number_of_trades"]}<br>
"""
    return f"""
<div class="performance-panel {panel_class}">
<h3>{escape_html(label.title())}</h3>
<form method="POST" action="{clear_action}" style="display:inline;">
<button type="submit" class="red">{clear_text}</button>
</form>
<form method="POST" action="{restore_action}" style="display:inline;">
<button type="submit" class="yellow">{restore_text}</button>
</form>
<br><br>
{stats_html}
Trade List:<br>
{render_trade_list(summary)}
</div>
"""


def get_position_pl_data(pos):
    symbol = pos.get("symbol", "")
    qty = float(pos.get("quantity", 0) or 0)
    cost_basis = float(pos.get("cost_basis", 0) or 0)

    quote = get_market_quote(symbol)

    current_price = None
    if quote:
        for key in ["last", "bid", "ask"]:
            if quote.get(key) is not None:
                try:
                    current_price = float(quote.get(key))
                    break
                except:
                    pass

    is_option = len(symbol) > 6

    if qty == 0:
        entry_price = 0
    elif is_option:
        entry_price = option_entry_price(cost_basis, qty)
    else:
        entry_price = cost_basis / qty

    if current_price is None:
        current_price = entry_price

    if is_option:
        current_value = option_current_value(current_price, qty)
    else:
        current_value = current_price * qty

    pnl = option_pnl(current_price, cost_basis, qty) if is_option else current_value - cost_basis

    if cost_basis != 0:
        pnl_percent = (pnl / cost_basis) * 100
    else:
        pnl_percent = 0

    stop_values = stop_debug_values(symbol, entry_price, current_price)
    hold_time = position_hold_time(symbol)
    current_exit_decision, current_exit_reason = current_exit_display(symbol, stop_values)

    position_data = {
        "symbol": symbol,
        "qty": qty,
        "entry_price": entry_price,
        "current_price": current_price,
        "hard_stop_percent": stop_values["hard_stop_percent"],
        "hard_stop_price": stop_values["hard_stop_price"],
        "peak_price": stop_values["peak_price"],
        "trailing_stop_percent": stop_values["trailing_stop_percent"],
        "trailing_stop_price": stop_values["trailing_stop_price"],
        "drawdown_from_peak_percent": stop_values["drawdown_from_peak_percent"],
        "cost_basis": cost_basis,
        "current_value": current_value,
        "pnl": pnl,
        "pnl_percent": pnl_percent,
        "hold_time": hold_time,
        "current_exit_decision": current_exit_decision,
        "current_exit_reason": current_exit_reason,
        "is_option": is_option
    }

    live_grade, live_score, live_reason = grade_live_trade(
        position_data,
        current_market_context_snapshot(),
        symbol
    )
    entry_row = find_last_buy(symbol)

    position_data["entry_grade"] = entry_row.get("EntryGrade", "") if entry_row else ""
    position_data["live_grade"] = live_grade
    position_data["live_score"] = live_score
    position_data["live_reason"] = live_reason

    return position_data


def get_bot_health_data(positions, bot_snapshot):
    broker_has_position = bool(positions)
    with BOT_LOCK:
        internal_symbols = list(BOT_STATE["position_peaks"].keys())
    internal_has_position = bool(internal_symbols)
    position_sync = "OK" if internal_has_position == broker_has_position else "MISMATCH"
    last_quote_age_ms = age_ms(bot_snapshot.get("last_quote_epoch"))
    api_connected = str(bot_snapshot.get("last_quote_status", "")).startswith("OK")
    broker_connected = str(bot_snapshot.get("last_position_status", "")).startswith("OK")

    health = {
        "engine_running": bool(bot_snapshot.get("thread_alive")),
        "last_tick": bot_snapshot.get("last_tick", ""),
        "last_tick_duration_ms": bot_snapshot.get("last_tick_duration_ms"),
        "last_quote_age_ms": last_quote_age_ms,
        "last_decision": (bot_snapshot.get("market_context") or {}).get("decision", "DO NOTHING"),
        "last_error": bot_snapshot.get("last_error", "None"),
        "api_status": "Connected" if api_connected else "Disconnected",
        "broker_status": "Connected" if broker_connected else "Disconnected",
        "internal_has_position": internal_has_position,
        "broker_has_position": broker_has_position,
        "position_sync": position_sync,
        "warning": position_sync == "MISMATCH",
        "symbol": "",
        "entry_price": None,
        "current_price": None,
        "peak_price": None,
        "trailing_stop_price": None,
        "distance_to_stop_percent": None,
        "poll_interval_ms": clamp_int(
            (bot_snapshot.get("config_strategy") or {}).get("exit_poll_interval_ms", 1000),
            100,
            5000,
            1000
        ) if broker_has_position else None,
        "trailing_stop_percent": None,
        "stop_armed": False,
        "hold_time": "N/A"
    }

    if positions:
        pl = get_position_pl_data(positions[0])
        health.update({
            "symbol": pl.get("symbol", ""),
            "entry_price": pl.get("entry_price"),
            "current_price": pl.get("current_price"),
            "peak_price": pl.get("peak_price"),
            "trailing_stop_price": pl.get("trailing_stop_price"),
            "distance_to_stop_percent": (
                ((pl.get("current_price") - pl.get("trailing_stop_price")) / pl.get("current_price")) * 100
                if pl.get("current_price") and pl.get("trailing_stop_price") else None
            ),
            "trailing_stop_percent": pl.get("trailing_stop_percent"),
            "stop_armed": bool(pl.get("trailing_stop_price") and pl.get("entry_price") and pl.get("trailing_stop_price") >= pl.get("entry_price")),
            "hold_time": pl.get("hold_time", "N/A")
        })

    return health


def get_developer_diagnostics(config, positions, bot_snapshot, bot_health):
    market_context = bot_snapshot.get("market_context") or {}
    broker_sync_status = bot_snapshot.get("last_position_status", "UNKNOWN")
    quote_count = int(bot_snapshot.get("quote_request_count") or 0)
    quote_total = int(bot_snapshot.get("quote_latency_total_ms") or 0)
    average_quote_ms = int(quote_total / quote_count) if quote_count else None
    position_cap = get_position_cap_status(positions, config)

    return {
        "engine_running": bool(bot_snapshot.get("thread_alive")),
        "polling_interval": int(config.get("scanner", {}).get("interval_seconds", 60)),
        "last_tick": bot_snapshot.get("last_tick", ""),
        "last_tick_duration_ms": bot_snapshot.get("last_tick_duration_ms"),
        "last_quote_time": bot_snapshot.get("last_quote_time", ""),
        "last_quote_age_ms": age_ms(bot_snapshot.get("last_quote_epoch")),
        "last_quote_latency_ms": bot_snapshot.get("last_quote_latency_ms"),
        "last_quote_status": bot_snapshot.get("last_quote_status", "UNKNOWN"),
        "last_position_time": bot_snapshot.get("last_position_time", ""),
        "last_position_latency_ms": bot_snapshot.get("last_position_latency_ms"),
        "last_position_status": broker_sync_status,
        "broker_sync": "OK" if str(broker_sync_status).startswith("OK") else broker_sync_status,
        "last_decision": market_context.get("decision", "DO NOTHING"),
        "last_action": bot_snapshot.get("last_action", ""),
        "internal_position": bot_health.get("internal_has_position", False),
        "broker_position": bot_health.get("broker_has_position", False),
        "position_sync": bot_health.get("position_sync", "UNKNOWN"),
        "current_signal": bot_snapshot.get("current_signal", "NONE"),
        "market_state": bot_snapshot.get("market_state", "UNKNOWN"),
        "last_error": bot_snapshot.get("last_error", "None"),
        "api_status": bot_health.get("api_status", "Disconnected"),
        "broker_status": bot_health.get("broker_status", "Disconnected"),
        "average_quote_latency_ms": average_quote_ms,
        "slowest_quote_latency_ms": bot_snapshot.get("quote_latency_slowest_ms"),
        "quote_failed_count": bot_snapshot.get("quote_failed_count"),
        "quote_rate_limited_count": bot_snapshot.get("quote_rate_limited_count"),
        "market_scan_ms": bot_snapshot.get("last_market_scan_ms"),
        "indicators_ms": bot_snapshot.get("last_indicators_ms"),
        "signal_ms": bot_snapshot.get("last_signal_ms"),
        "order_submit_ms": bot_snapshot.get("last_order_submit_ms"),
        "broker_confirm_ms": bot_snapshot.get("last_broker_confirm_ms"),
        "position_monitor": bot_health,
        "current_total_option_contracts": position_cap["current_total_option_contracts"],
        "max_open_contracts": position_cap["max_open_contracts"],
        "position_cap_status": position_cap["position_cap_status"]
    }


@app.route("/")
def dashboard():
    config = load_config()

    mode = config["mode"]
    symbol = config["symbol"]
    asset = config["asset_type"]
    contracts = config["contracts"]
    bot_enabled = config["bot_enabled"]
    strategy_mode = config["strategy_mode"]

    s = config["strategy"]
    e = config["entry_rules"]

    quote = get_market_quote(symbol)
    positions = get_position()
    call = select_atm_contract(symbol, "CALL")
    put = select_atm_contract(symbol, "PUT")
    sync_trade_limits_from_file(config)

    with BOT_LOCK:
        bot_snapshot = {
            "bullish_score": BOT_STATE["bullish_score"],
            "bearish_score": BOT_STATE["bearish_score"],
            "confidence": BOT_STATE["confidence"],
            "dominance_percent": BOT_STATE["dominance_percent"],
            "current_signal": BOT_STATE["current_signal"],
            "last_action": BOT_STATE["last_action"],
            "trades_today": BOT_STATE["trades_today"],
            "spent_today": BOT_STATE["spent_today"],
            "budget_remaining": BOT_STATE["budget_remaining"],
            "next_call_cost": BOT_STATE["next_call_cost"],
            "next_put_cost": BOT_STATE["next_put_cost"],
            "market_state": BOT_STATE["market_state"],
            "market_context": dict(BOT_STATE["market_context"]),
            "level_distances": dict(BOT_STATE["level_distances"]),
            "thread_alive": BOT_STATE["thread_alive"],
            "last_tick": BOT_STATE["last_tick"],
            "samples_length": BOT_STATE["samples_length"],
            "last_trade_timestamp": BOT_STATE["last_trade_timestamp"],
            "cooldown_remaining_seconds": BOT_STATE["cooldown_remaining_seconds"],
            "last_trade_review": dict(BOT_STATE["last_trade_review"]),
            "reason_log": list(BOT_STATE["reason_log"]),
            "last_quote_time": BOT_STATE["last_quote_time"],
            "last_quote_epoch": BOT_STATE["last_quote_epoch"],
            "last_quote_latency_ms": BOT_STATE["last_quote_latency_ms"],
            "last_quote_status": BOT_STATE["last_quote_status"],
            "last_position_time": BOT_STATE["last_position_time"],
            "last_position_epoch": BOT_STATE["last_position_epoch"],
            "last_position_latency_ms": BOT_STATE["last_position_latency_ms"],
            "last_position_status": BOT_STATE["last_position_status"],
            "last_tick_epoch": BOT_STATE["last_tick_epoch"],
            "last_tick_duration_ms": BOT_STATE["last_tick_duration_ms"],
            "last_error": BOT_STATE["last_error"],
            "quote_request_count": BOT_STATE["quote_request_count"],
            "quote_latency_total_ms": BOT_STATE["quote_latency_total_ms"],
            "quote_latency_slowest_ms": BOT_STATE["quote_latency_slowest_ms"],
            "quote_failed_count": BOT_STATE["quote_failed_count"],
            "quote_rate_limited_count": BOT_STATE["quote_rate_limited_count"],
            "last_order_submit_ms": BOT_STATE["last_order_submit_ms"],
            "last_broker_confirm_ms": BOT_STATE["last_broker_confirm_ms"],
            "last_market_scan_ms": BOT_STATE["last_market_scan_ms"],
            "last_indicators_ms": BOT_STATE["last_indicators_ms"],
            "last_signal_ms": BOT_STATE["last_signal_ms"],
            "config_strategy": dict(config.get("strategy", {}))
        }

    call_price = get_option_trade_price(call)
    put_price = get_option_trade_price(put)
    call_cost = bot_snapshot["next_call_cost"]
    put_cost = bot_snapshot["next_put_cost"]

    if call_cost is None and call_price is not None:
        call_cost = call_price * 100 * contracts
    if put_cost is None and put_price is not None:
        put_cost = put_price * 100 * contracts

    reason_log_html = "<br>".join(bot_snapshot["reason_log"]) or "No bot actions yet."
    market_context = bot_snapshot["market_context"] or empty_market_context(get_quote_price(quote))
    trade_performance = get_trade_performance()
    bot_health = get_bot_health_data(positions, bot_snapshot)
    developer_diagnostics = get_developer_diagnostics(config, positions, bot_snapshot, bot_health)
    position_cap = get_position_cap_status(positions, config)
    levels = market_context.get("levels", {})
    distances = market_context.get("level_distances", {})

    def level_line(label, key):
        value = levels.get(key)
        distance = distances.get(key)
        return f"{label}: {fmt_money(value)} | Distance: {fmt_money(distance)}"

    level_rows = "<br>".join([
        level_line("Previous Week High", "previous_week_high"),
        level_line("Previous Week Low", "previous_week_low"),
        level_line("Previous Day High", "previous_day_high"),
        level_line("Previous Day Low", "previous_day_low"),
        level_line("Today High", "today_high"),
        level_line("Today Low", "today_low"),
        level_line("Today Open", "today_open"),
        level_line("Premarket High", "premarket_high"),
        level_line("Premarket Low", "premarket_low"),
        level_line("Last Hour High", "last_hour_high"),
        level_line("Last Hour Low", "last_hour_low"),
        level_line("Opening Range High", "opening_range_high"),
        level_line("Opening Range Low", "opening_range_low")
    ])

    def checked(value):
        return "checked" if value else ""

    def selected(value, current):
        return "selected" if value == current else ""

    html = f"""
<html>
<head>
<title>Trading Bot Dashboard</title>
<style>
body {{
    font-family: Arial;
    margin: 30px;
    background: #202124;
    color: white;
}}
h1 {{ color: #00ff88; }}
.card {{
    border: 1px solid #555;
    padding: 15px;
    margin-bottom: 18px;
    border-radius: 10px;
    background: #2a2a2a;
}}
.grid {{
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 10px;
}}
.item {{
    background: #1b1b1b;
    padding: 10px;
    border-radius: 8px;
}}
.label {{ color: #aaa; font-size: 13px; }}
.value {{ font-size: 18px; font-weight: bold; }}
input, select {{
    padding: 7px;
    margin: 4px;
    border-radius: 5px;
    border: none;
}}
button {{
    padding: 10px 14px;
    margin: 4px;
    border: none;
    border-radius: 8px;
    background: #00ff88;
    font-weight: bold;
    cursor: pointer;
}}
.red {{ background: #ff5555; }}
.yellow {{ background: #ffd166; }}
.good {{ color: #00ff88; }}
.bad {{ color: #ff6666; }}
.warning {{
    background: #7a1111;
    color: white;
    padding: 12px;
    border-radius: 8px;
    font-size: 22px;
    font-weight: bold;
    margin-bottom: 12px;
    text-align: center;
}}
.trade-card {{
    border-left: 5px solid #777;
    padding: 12px;
    margin: 10px 0;
    border-radius: 8px;
    background: #1b1b1b;
}}
.trade-card.bot {{ border-left-color: #00b7ff; }}
.trade-card.human {{ border-left-color: #ffd166; }}
.badge {{
    display: inline-block;
    padding: 3px 8px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: bold;
    color: #111;
}}
.badge.bot {{ background: #00b7ff; }}
.badge.human {{ background: #ffd166; }}
.performance-grid {{
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 15px;
}}
.performance-panel {{
    background: #1b1b1b;
    border-radius: 8px;
    padding: 12px;
}}
.performance-panel.bot {{ border: 1px solid #00b7ff; }}
.performance-panel.human {{ border: 1px solid #ffd166; }}
.trade-line {{ margin: 4px 0; }}
</style>
</head>
<body>

<h1>Trading Bot Dashboard</h1>

<div class="card">
<h2>Status</h2>
Bot: <span class="{ "good" if bot_enabled else "bad" }">{ "ENABLED" if bot_enabled else "DISABLED" }</span><br>
Mode: {mode}<br>
Strategy Mode: {strategy_mode}<br>
Ticker: {symbol}
</div>

<div class="card">
<h2>SURFER Bot</h2>
Today's Bot Trading Budget: {fmt_money(config.get("bot_budget", 100))}<br>
Spent Today: <span id="bot-spent-today">{fmt_money(bot_snapshot["spent_today"])}</span><br>
Today's Budget Remaining: <span id="bot-budget-remaining">{fmt_money(bot_snapshot["budget_remaining"])}</span><br>
Cost of Next ATM CALL: <span id="bot-next-call-cost">{fmt_money(call_cost)}</span><br>
Cost of Next ATM PUT: <span id="bot-next-put-cost">{fmt_money(put_cost)}</span><br>
Thread Alive: <span id="bot-thread-alive">{bot_snapshot["thread_alive"]}</span><br>
Last Tick: <span id="bot-last-tick">{bot_snapshot["last_tick"]}</span><br>
Samples Length: <span id="bot-samples-length">{bot_snapshot["samples_length"]}</span><br>
Last Trade Timestamp: <span id="bot-last-trade-timestamp">{bot_snapshot["last_trade_timestamp"]}</span><br>
Cooldown Remaining Seconds: <span id="bot-cooldown-remaining">{bot_snapshot["cooldown_remaining_seconds"]}</span><br>
Market State: <span id="bot-market-state">{bot_snapshot["market_state"]}</span><br>
Bullish Score: <span id="bot-bullish-score">{bot_snapshot["bullish_score"]}</span><br>
Bearish Score: <span id="bot-bearish-score">{bot_snapshot["bearish_score"]}</span><br>
Current Confidence: <span id="bot-confidence">{bot_snapshot["confidence"]}</span> / 10<br>
Minimum Confidence Required: <span id="bot-minimum-confidence">{config.get("minimum_confidence", 2)}</span> / 10<br>
Dominance: <span id="bot-dominance">{float(bot_snapshot.get("dominance_percent") or 0):.1f}</span>%<br>
Minimum Dominance Required: <span id="bot-minimum-dominance">{config.get("minimum_dominance_percent", 60)}</span>%<br>
Current Signal: <span id="bot-current-signal">{bot_snapshot["current_signal"]}</span><br>
Pending Entry: <span id="pending-entry-active">{ "YES" if bot_snapshot.get("pending_entry", {}).get("active") else "NO" }</span><br>
Pending Direction: <span id="pending-entry-direction">{bot_snapshot.get("pending_entry", {}).get("direction") or "N/A"}</span><br>
Starting Option Price: <span id="pending-entry-start-price">{fmt_premium(bot_snapshot.get("pending_entry", {}).get("starting_option_price"))}</span><br>
Required Confirmation: <span id="pending-entry-required">{fmt_percent(bot_snapshot.get("pending_entry", {}).get("required_confirmation_percent"))}</span><br>
Confirmation Price: <span id="pending-entry-confirm-price">{fmt_premium(bot_snapshot.get("pending_entry", {}).get("confirmation_price"))}</span><br>
Current Option Price: <span id="pending-entry-current-price">{fmt_premium(bot_snapshot.get("pending_entry", {}).get("current_option_price"))}</span><br>
Time Remaining: <span id="pending-entry-time-remaining">{bot_snapshot.get("pending_entry", {}).get("time_remaining_seconds", 0)}</span> sec<br>
Pending Status: <span id="pending-entry-status">{bot_snapshot.get("pending_entry", {}).get("status", "NONE")}</span><br>
Last Bot Action: <span id="bot-last-action">{bot_snapshot["last_action"]}</span><br>
Trades Today: <span id="bot-trades-today">{bot_snapshot["trades_today"]}</span><br>
Current P/L: <span id="bot-current-pl">{fmt_money(market_context.get("current_pl"))}</span><br>
Distance to Trailing Stop: <span id="bot-distance-trailing">{fmt_money(market_context.get("distance_to_trailing_stop"))}</span><br>
Current Range Size: <span id="bot-range-size">{fmt_money(market_context.get("current_range_size"))}</span><br>
<br>
Market Structure:<br>
Data Source: <span id="market-structure-source">{market_context.get("market_structure_source", "Tradier daily/intraday bars")}</span><br>
Market Date: <span id="market-date">{market_context.get("market_date", "")}</span><br>
Last Updated: <span id="market-structure-last-updated">{market_context.get("market_structure_last_updated", "")}</span><br>
<div class="item" id="market-structure">{level_rows}</div>
<br>
Bot Reason Log:<br>
<div class="item" id="bot-reason-log">{reason_log_html}</div>
</div>

<div class="card">
<h2>Manual Contract Picker</h2>

<form method="POST" action="/manual-buy-selected">

Expiration:
<select id="expiration_select"></select>

Option Type:
<select id="option_type_select">
<option value="call">CALL</option>
<option value="put">PUT</option>
</select>

Strike:
<select id="strike_select"></select>

Quantity:
<input type="number" name="manual_qty" value="1" min="1">

<input type="hidden" id="option_symbol_input" name="option_symbol">

<br><br>

Selected Contract:
<div id="selected_contract">Loading...</div>

Bid: <span id="bid">N/A</span>
Ask: <span id="ask">N/A</span>
Last: <span id="last">N/A</span>
Volume: <span id="volume">N/A</span>
Open Interest: <span id="open_interest">N/A</span>

<br><br>

<button type="submit">BUY SELECTED CONTRACT</button>

</form>
</div>

<div class="card">
<h2>Manual Controls</h2>
<form method="POST" action="/manual-buy-call" style="display:inline;">
<button type="submit">Auto Buy ATM CALL</button>
</form>

<form method="POST" action="/manual-buy-put" style="display:inline;">
<button type="submit" class="yellow">Auto Buy ATM PUT</button>
</form>

<form method="POST" action="/manual-sell" style="display:inline;">
<button type="submit" class="red">Sell All Positions</button>
</form>
</div>

<div class="card">
<h2>Live Market</h2>
"""

    if quote:
        html += f"""
<div class="grid">
<div class="item"><div class="label">Symbol</div><div class="value" id="quote-symbol">{quote.get("symbol")}</div></div>
<div class="item"><div class="label">Last</div><div class="value" id="quote-last">{fmt_money(quote.get("last"))}</div></div>
<div class="item"><div class="label">Bid</div><div class="value" id="quote-bid">{fmt_money(quote.get("bid"))}</div></div>
<div class="item"><div class="label">Ask</div><div class="value" id="quote-ask">{fmt_money(quote.get("ask"))}</div></div>
<div class="item"><div class="label">Volume</div><div class="value" id="quote-volume">{fmt_int(quote.get("volume"))}</div></div>
<div class="item"><div class="label">Avg Volume</div><div class="value" id="quote-average-volume">{fmt_int(quote.get("average_volume"))}</div></div>
</div>
"""
    else:
        html += "Market quote unavailable."

    html += "</div>"

    html += """
<div class="card">
<h2>Selected ATM Options</h2>
<div class="grid">
"""

    for title, opt in [("CALL", call), ("PUT", put)]:
        if opt:
            spread = None
            if opt.get("ask") is not None and opt.get("bid") is not None:
                spread = float(opt.get("ask")) - float(opt.get("bid"))

            html += f"""
<div class="item">
<div class="label">{title}</div>
<div class="value">{opt.get("symbol")}</div>
Strike: {opt.get("strike")}<br>
Exp: {opt.get("expiration")}<br>
Bid: {fmt_money(opt.get("bid"))}<br>
Ask: {fmt_money(opt.get("ask"))}<br>
Last: {fmt_money(opt.get("last"))}<br>
Spread: {fmt_money(spread)}<br>
Volume: {fmt_int(opt.get("volume"))}<br>
Open Interest: {fmt_int(opt.get("open_interest"))}
</div>
"""
        else:
            html += f"<div class='item'>{title}: No contract found</div>"

    html += f"""
</div>
</div>

<div class="card">
<h2>Strategy Summary</h2>
EMA Stack: {s.get("ema_fast")} / {s.get("ema_medium")} / {s.get("ema_slow")}<br>
MA Stack: {s.get("ma_fast")} / {s.get("ma_medium")} / {s.get("ma_slow")}<br>
MACD: {s.get("use_macd")}<br>
VWAP: {s.get("use_vwap")}<br>
Volume: {s.get("use_volume")}<br>
Hard Stop: {s.get("hard_stop_percent")}%<br>
Trailing Stop: {s.get("trailing_stop_percent")}%<br>
Direction Threshold: {s.get("direction_threshold_percent")}%<br>
</div>

<div class="card">
<h2>Entry Rules</h2>
EMA Alignment: {e.get("ema_alignment")}<br>
MACD Confirmation: {e.get("macd_confirmation")}<br>
VWAP Confirmation: {e.get("vwap_confirmation")}<br>
Volume Confirmation: {e.get("volume_confirmation")}<br>
Minimum Signals: {e.get("minimum_signals")}<br>
Allow Calls: {e.get("allow_calls")}<br>
Allow Puts: {e.get("allow_puts")}<br>
Cooldown Minutes: {e.get("cooldown_minutes")}<br>
Max Trades Per Day: {e.get("max_trades_per_day")}
</div>

<div class="card">
<h2>Bot Health</h2>
<div class="grid">
<div class="item"><div class="label">Status</div><div class="value good" id="health-engine-status">{ "🟢 Bot Running" if bot_health["engine_running"] else "🔴 Bot Stopped" }</div></div>
<div class="item"><div class="label">Last Tick</div><div class="value" id="health-last-tick">{bot_health["last_tick"] or "N/A"}</div></div>
<div class="item"><div class="label">Tick Duration</div><div class="value" id="health-tick-duration">{fmt_int(bot_health["last_tick_duration_ms"])} ms</div></div>
<div class="item"><div class="label">Last Quote</div><div class="value" id="health-last-quote-age">{fmt_int(bot_health["last_quote_age_ms"])} ms ago</div></div>
<div class="item"><div class="label">Last Decision</div><div class="value" id="health-last-decision">{bot_health["last_decision"]}</div></div>
<div class="item"><div class="label">Last Error</div><div class="value" id="health-last-error">{bot_health["last_error"]}</div></div>
<div class="item"><div class="label">API Status</div><div class="value" id="health-api-status">{bot_health["api_status"]}</div></div>
<div class="item"><div class="label">Broker</div><div class="value" id="health-broker-status">{bot_health["broker_status"]}</div></div>
<div class="item"><div class="label">Position Sync</div><div class="value" id="health-position-sync">{bot_health["position_sync"]}</div></div>
</div>
</div>

<div class="card">
<h2>Position Monitor</h2>
<div id="position-monitor-warning">{ '<div class="warning">🚨 DESYNC</div>' if bot_health["warning"] else '' }</div>
<div class="grid">
<div class="item"><div class="label">Broker Position</div><div class="value" id="monitor-broker-position">{ "YES" if bot_health["broker_has_position"] else "NO" }</div></div>
<div class="item"><div class="label">Local Position</div><div class="value" id="monitor-local-position">{ "YES" if bot_health["internal_has_position"] else "NO" }</div></div>
<div class="item"><div class="label">Status</div><div class="value" id="monitor-position-status">{ "IN SYNC" if bot_health["position_sync"] == "OK" else "🚨 DESYNC" }</div></div>
<div class="item"><div class="label">Current Open Contracts</div><div class="value" id="monitor-total-contracts">{position_cap["current_total_option_contracts"]}</div></div>
<div class="item"><div class="label">Max Open Contracts</div><div class="value" id="monitor-max-contracts">{position_cap["max_open_contracts"]}</div></div>
<div class="item"><div class="label">Position Cap</div><div class="value" id="monitor-cap-status">{position_cap["position_cap_status"]}</div></div>
</div>
</div>

<div class="card">
<h2>Exit Monitor</h2>
<div class="grid">
<div class="item"><div class="label">Entry</div><div class="value" id="exit-entry">{fmt_money(bot_health["entry_price"])}</div></div>
<div class="item"><div class="label">Current</div><div class="value" id="exit-current">{fmt_money(bot_health["current_price"])}</div></div>
<div class="item"><div class="label">Peak</div><div class="value" id="exit-peak">{fmt_money(bot_health["peak_price"])}</div></div>
<div class="item"><div class="label">Trailing Stop</div><div class="value" id="exit-trailing-stop">{fmt_money(bot_health["trailing_stop_price"])}</div></div>
<div class="item"><div class="label">Distance to Stop</div><div class="value" id="exit-distance-stop">{fmt_int(bot_health["distance_to_stop_percent"])}%</div></div>
<div class="item"><div class="label">Poll Interval</div><div class="value" id="exit-poll-interval">{fmt_int(bot_health["poll_interval_ms"])} ms</div></div>
<div class="item"><div class="label">Last Quote</div><div class="value" id="exit-last-quote-age">{fmt_int(bot_health["last_quote_age_ms"])} ms ago</div></div>
<div class="item"><div class="label">Stop Armed</div><div class="value" id="exit-stop-armed">{ "YES" if bot_health["stop_armed"] else "NO" }</div></div>
<div class="item"><div class="label">Time In Trade</div><div class="value" id="exit-time-in-trade">{bot_health["hold_time"]}</div></div>
</div>
</div>

<div class="card">
<h2>Performance</h2>
<div class="grid">
<div class="item"><div class="label">Average Quote Request</div><div class="value" id="perf-average-quote">{fmt_int(developer_diagnostics["average_quote_latency_ms"])} ms</div></div>
<div class="item"><div class="label">Slowest</div><div class="value" id="perf-slowest-quote">{fmt_int(developer_diagnostics["slowest_quote_latency_ms"])} ms</div></div>
<div class="item"><div class="label">Failed</div><div class="value" id="perf-quote-failed">{developer_diagnostics["quote_failed_count"]}</div></div>
<div class="item"><div class="label">Rate Limited</div><div class="value" id="perf-rate-limited">{developer_diagnostics["quote_rate_limited_count"]}</div></div>
</div>
</div>

<div class="card">
<h2>Decision Timer</h2>
<div class="grid">
<div class="item"><div class="label">Market Scan</div><div class="value" id="timer-market-scan">{fmt_int(developer_diagnostics["market_scan_ms"])} ms</div></div>
<div class="item"><div class="label">Indicators</div><div class="value" id="timer-indicators">{fmt_int(developer_diagnostics["indicators_ms"])} ms</div></div>
<div class="item"><div class="label">Signal</div><div class="value" id="timer-signal">{fmt_int(developer_diagnostics["signal_ms"])} ms</div></div>
<div class="item"><div class="label">Order Submit</div><div class="value" id="timer-order-submit">{fmt_int(developer_diagnostics["order_submit_ms"])} ms</div></div>
<div class="item"><div class="label">Broker Confirm</div><div class="value" id="timer-broker-confirm">{fmt_int(developer_diagnostics["broker_confirm_ms"])} ms</div></div>
</div>
</div>

<div class="card">
<h2>Current Position</h2>
<div id="current-position-content">

"""

    if positions:
        for pos in positions:
            pl = get_position_pl_data(pos)
            pnl_color = "#00ff88" if pl["pnl"] >= 0 else "#ff5555"
            html += f"""
<div class="item">
<div class="label">Symbol</div>
<div class="value">{pl["symbol"]}</div>
Qty: {pl["qty"]}<br>
Entry: {fmt_money(pl["entry_price"])}<br>
Current: {fmt_money(pl["current_price"])}<br>
Hard Stop %: {pl["hard_stop_percent"]:.2f}%<br>
Hard Stop Price: {fmt_money(pl["hard_stop_price"])}<br>
Peak Price: {fmt_money(pl["peak_price"])}<br>
Trailing Stop %: {pl["trailing_stop_percent"]:.2f}%<br>
Trailing Stop Price: {fmt_money(pl["trailing_stop_price"])}<br>
Drawdown From Peak %: {pl["drawdown_from_peak_percent"]:.2f}%<br>
<span style="color:{pnl_color}; font-weight:bold;">
P/L: {fmt_money(pl["pnl"])}<br>
P/L %: {pl["pnl_percent"]:+.2f}%
</span><br>
Hold Time: {pl.get("hold_time") or "N/A"}<br>
Live Grade: {pl.get("live_grade") or "N/A"}<br>
Current Exit Decision: {pl.get("current_exit_decision") or "HOLD"}<br>
Current Exit Reason:<br>
{escape_html(pl.get("current_exit_reason") or "N/A").replace(chr(10), "<br>")}<br>
Status: OPEN
</div>
<br>
"""
    else:
        html += "No Position"

    html += """
</div>
</div>
"""

    html += f"""
<div class="card">
<h2>Bot vs Human Performance</h2>
<div class="performance-grid">
{render_performance_panel("HUMAN", trade_performance["HUMAN"])}
{render_performance_panel("BOT", trade_performance["BOT"])}
</div>
</div>
"""

    visible_trades = enrich_trade_rows(get_trade_history_trades(limit=None))
    html += """
<div class="card">
<h2>Trade History</h2>
<form method="POST" action="/clear-trade-history-view" style="display:inline;">
<button type="submit" class="red">Clear Trade History</button>
</form>
<form method="POST" action="/restore-trade-history-view" style="display:inline;">
<button type="submit" class="yellow">Undo Clear Trade History</button>
</form>
<br><br>
"""

    if visible_trades:
        for trade in reversed(visible_trades):
            html += render_recent_trade_card(trade)
    else:
        html += "No trades visible."

    html += """
</div>
"""

    html += f"""
<div class="card">
<h2>Settings</h2>

<form method="POST" action="/save-settings">

<h3>General</h3>

Bot Enabled:
<input type="checkbox" name="bot_enabled" {checked(bot_enabled)}><br>

Decision Time:
<input name="decision_time" value="{config.get("decision_time", "09:35")}"><br>

Bot Starting Account Balance $:
<input type="number" step="0.01" name="bot_starting_account_balance" value="{config.get("bot_starting_account_balance", 500)}"><br>

Human Starting Account Balance $:
<input type="number" step="0.01" name="human_starting_account_balance" value="{config.get("human_starting_account_balance", 500)}"><br>

Today's Bot Trading Budget $:
<input type="number" step="0.01" name="bot_budget" value="{config.get("bot_budget", 100)}"><br>

Today's Human Trading Budget $:
<input type="number" step="0.01" name="human_daily_trading_budget" value="{config.get("human_daily_trading_budget", 500)}"><br>

Max Contract Price:
<input type="number" step="0.01" name="max_contract_price" value="{config.get("max_contract_price", 1)}"><br>

Contract Selection Mode:
<select name="contract_selection_mode">
<option value="strict_atm" {selected("strict_atm", config.get("contract_selection_mode", "strict_atm"))}>Strict ATM (Skip if too expensive)</option>
<option value="closest_within_budget" {selected("closest_within_budget", config.get("contract_selection_mode", "strict_atm"))}>Closest Within Budget</option>
</select><br>

Max Open Contracts:
<input type="number" name="max_open_contracts" value="{config.get("max_open_contracts", 1)}" min="1" max="5"><br>

Minimum Confidence:
<input type="number" name="minimum_confidence" min="1" max="10" value="{config.get("minimum_confidence", 2)}"><br>

Minimum Dominance %:
<input type="number" name="minimum_dominance_percent" min="50" max="100" value="{config.get("minimum_dominance_percent", 60)}"><br>

Mode:
<select name="mode">
<option value="sandbox" {selected("sandbox", mode)}>Sandbox</option>
<option value="live" {selected("live", mode)}>Live</option>
</select><br>

Ticker:
<select name="symbol_choice">
<option value="SPY" {selected("SPY", symbol)}>SPY</option>
<option value="NVDA" {selected("NVDA", symbol)}>NVDA</option>
<option value="GOOG" {selected("GOOG", symbol)}>GOOG</option>
<option value="GOOGL" {selected("GOOGL", symbol)}>GOOGL</option>
<option value="AAPL" {selected("AAPL", symbol)}>AAPL</option>
<option value="TSLA" {selected("TSLA", symbol)}>TSLA</option>
</select>

Custom:
<input name="custom_symbol" placeholder="Type ticker here"><br>

Asset Type:
<select name="asset_type">
<option value="option" {selected("option", asset)}>Option</option>
<option value="stock" {selected("stock", asset)}>Stock</option>
</select><br>

Contracts:
<input type="number" name="contracts" value="{contracts}" min="1"><br>

Strategy Mode:
<select name="strategy_mode">
<option value="SURFER" {selected("SURFER", strategy_mode)}>SURFER</option>
<option value="TSUNAMI" {selected("TSUNAMI", strategy_mode)}>TSUNAMI</option>
</select><br>

<h3>Indicators</h3>

EMA Fast:
<input type="number" name="ema_fast" value="{s.get("ema_fast")}"><br>

EMA Medium:
<input type="number" name="ema_medium" value="{s.get("ema_medium")}"><br>

EMA Slow:
<input type="number" name="ema_slow" value="{s.get("ema_slow")}"><br>

MA Fast:
<input type="number" name="ma_fast" value="{s.get("ma_fast")}"><br>

MA Medium:
<input type="number" name="ma_medium" value="{s.get("ma_medium")}"><br>

MA Slow:
<input type="number" name="ma_slow" value="{s.get("ma_slow")}"><br>

Use MACD:
<input type="checkbox" name="use_macd" {checked(s.get("use_macd"))}><br>

Use VWAP:
<input type="checkbox" name="use_vwap" {checked(s.get("use_vwap"))}><br>

Use Volume:
<input type="checkbox" name="use_volume" {checked(s.get("use_volume"))}><br>

<h3>Entry Rules</h3>

EMA Alignment:
<input type="checkbox" name="ema_alignment" {checked(e.get("ema_alignment"))}><br>

MACD Confirmation:
<input type="checkbox" name="macd_confirmation" {checked(e.get("macd_confirmation"))}><br>

VWAP Confirmation:
<input type="checkbox" name="vwap_confirmation" {checked(e.get("vwap_confirmation"))}><br>

Volume Confirmation:
<input type="checkbox" name="volume_confirmation" {checked(e.get("volume_confirmation"))}><br>

Minimum Signals:
<input type="number" name="minimum_signals" value="{e.get("minimum_signals")}"><br>

Allow Calls:
<input type="checkbox" name="allow_calls" {checked(e.get("allow_calls"))}><br>

Allow Puts:
<input type="checkbox" name="allow_puts" {checked(e.get("allow_puts"))}><br>

Cooldown Minutes:
<input type="number" name="cooldown_minutes" value="{e.get("cooldown_minutes")}"><br>

Max Trades Per Day:
<input type="number" name="max_trades_per_day" value="{e.get("max_trades_per_day")}"><br>

<h3>Option Momentum Confirmation</h3>

Enable Option Momentum Confirmation:
<input type="checkbox" name="option_momentum_confirmation_enabled" {checked(config.get("option_momentum_confirmation_enabled", True))}><br>

Confirmation Percent:
<input type="number" step="0.1" min="0.1" max="20" name="option_momentum_percent" value="{config.get("option_momentum_percent", 2.0)}"><br>

Confirmation Timeout Seconds:
<input type="number" min="1" max="300" name="confirmation_timeout_seconds" value="{config.get("confirmation_timeout_seconds", 10)}"><br>

<h3>Risk</h3>

Hard Stop %:
<input type="number" step="0.1" name="hard_stop_percent" value="{s.get("hard_stop_percent")}"><br>

Trailing Stop %:
<input type="number" step="0.1" name="trailing_stop_percent" value="{s.get("trailing_stop_percent")}"><br>

<h3>Opening Direction</h3>

Exit Poll Interval (ms):
<input type="number" min="100" max="5000" step="50" name="exit_poll_interval_ms" value="{s.get("exit_poll_interval_ms", 1000)}"><br>

Direction Threshold %:
<input type="number" step="0.1" name="direction_threshold_percent" value="{s.get("direction_threshold_percent")}"><br>

Bot Scan Every Seconds:
<input type="number" name="interval_seconds" value="{config.get("scanner", {}).get("interval_seconds", 60)}"><br><br>

<button type="submit">Save Settings</button>

</form>
</div>

<script>
const CURRENT_SYMBOL = "{symbol}";

function setText(id, value) {{
    const el = document.getElementById(id);
    if (el) el.textContent = value ?? "N/A";
}}

function fmtMoney(value) {{
    if (value === null || value === undefined || value === "") return "N/A";
    const num = Number(value);
    if (Number.isNaN(num)) return "N/A";
    return `$${{num.toLocaleString(undefined, {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }})}}`;
}}

function fmtInt(value) {{
    if (value === null || value === undefined || value === "") return "N/A";
    const num = Number(value);
    if (Number.isNaN(num)) return "N/A";
    return Math.round(num).toLocaleString();
}}

function fmtPercent(value) {{
    if (value === null || value === undefined || value === "") return "N/A";
    const num = Number(value);
    if (Number.isNaN(num)) return "N/A";
    const sign = num > 0 ? "+" : "";
    return `${{sign}}${{num.toFixed(1)}}%`;
}}

async function getJson(url) {{
    const res = await fetch(url, {{ cache: "no-store" }});
    if (!res.ok) throw new Error(`${{url}} failed`);
    return await res.json();
}}

function renderReasonLog(lines) {{
    const el = document.getElementById("bot-reason-log");
    if (!el) return;
    el.innerHTML = (lines && lines.length) ? lines.join("<br>") : "No bot actions yet.";
}}

function renderMarketStructure(data) {{
    const el = document.getElementById("market-structure");
    if (!el) return;

    const labels = [
        ["Previous Week High", "previous_week_high"],
        ["Previous Week Low", "previous_week_low"],
        ["Previous Day High", "previous_day_high"],
        ["Previous Day Low", "previous_day_low"],
        ["Today High", "today_high"],
        ["Today Low", "today_low"],
        ["Today Open", "today_open"],
        ["Premarket High", "premarket_high"],
        ["Premarket Low", "premarket_low"],
        ["Last Hour High", "last_hour_high"],
        ["Last Hour Low", "last_hour_low"],
        ["Opening Range High", "opening_range_high"],
        ["Opening Range Low", "opening_range_low"]
    ];

    el.innerHTML = labels.map(([label, key]) => {{
        const value = data.levels?.[key];
        const distance = data.level_distances?.[key];
        return `${{label}}: ${{fmtMoney(value)}} | Distance: ${{fmtMoney(distance)}}`;
    }}).join("<br>");
}}

function renderPositions(positions) {{
    const el = document.getElementById("current-position-content");
    if (!el) return;

    if (!positions || positions.length === 0) {{
        el.innerHTML = "No Position";
        return;
    }}

    el.innerHTML = positions.map((pl) => {{
        const pnlColor = Number(pl.pnl || 0) >= 0 ? "#00ff88" : "#ff5555";
        return `
<div class="item">
<div class="label">Symbol</div>
<div class="value">${{pl.symbol}}</div>
Qty: ${{pl.qty}}<br>
Entry: ${{fmtMoney(pl.entry_price)}}<br>
Current: ${{fmtMoney(pl.current_price)}}<br>
Hard Stop %: ${{Number(pl.hard_stop_percent || 0).toFixed(2)}}%<br>
Hard Stop Price: ${{fmtMoney(pl.hard_stop_price)}}<br>
Peak Price: ${{fmtMoney(pl.peak_price)}}<br>
Trailing Stop %: ${{Number(pl.trailing_stop_percent || 0).toFixed(2)}}%<br>
Trailing Stop Price: ${{fmtMoney(pl.trailing_stop_price)}}<br>
Drawdown From Peak %: ${{Number(pl.drawdown_from_peak_percent || 0).toFixed(2)}}%<br>
<span style="color:${{pnlColor}}; font-weight:bold;">
P/L: ${{fmtMoney(pl.pnl)}}<br>
P/L %: ${{Number(pl.pnl_percent || 0).toFixed(2)}}%
</span><br>
Hold Time: ${{pl.hold_time || "N/A"}}<br>
Live Grade: ${{pl.live_grade || "N/A"}}<br>
Current Exit Decision: ${{pl.current_exit_decision || "HOLD"}}<br>
Current Exit Reason:<br>
${{String(pl.current_exit_reason || "N/A").replace(/\\n/g, "<br>")}}<br>
Status: OPEN
</div>
<br>`;
    }}).join("");
}}

async function updateBotState() {{
    const data = await getJson("/api/bot-state");
    setText("bot-spent-today", fmtMoney(data.spent_today));
    setText("bot-budget-remaining", fmtMoney(data.budget_remaining));
    setText("bot-next-call-cost", fmtMoney(data.next_call_cost));
    setText("bot-next-put-cost", fmtMoney(data.next_put_cost));
    setText("bot-thread-alive", data.thread_alive);
    setText("bot-last-tick", data.last_tick);
    setText("bot-samples-length", data.samples_length);
    setText("bot-last-trade-timestamp", data.last_trade_timestamp);
    setText("bot-cooldown-remaining", data.cooldown_remaining_seconds);
    setText("bot-market-state", data.market_state);
    setText("bot-bullish-score", data.bullish_score);
    setText("bot-bearish-score", data.bearish_score);
    setText("bot-confidence", data.confidence);
    setText("bot-dominance", Number(data.dominance_percent || 0).toFixed(1));
    setText("bot-current-signal", data.current_signal);
    const pending = data.pending_entry || {{}};
    setText("pending-entry-active", pending.active ? "YES" : "NO");
    setText("pending-entry-direction", pending.direction || "N/A");
    setText("pending-entry-start-price", fmtMoney(pending.starting_option_price));
    setText("pending-entry-required", `+${{Number(pending.required_confirmation_percent || 0).toFixed(2)}}%`);
    setText("pending-entry-confirm-price", fmtMoney(pending.confirmation_price));
    setText("pending-entry-current-price", fmtMoney(pending.current_option_price));
    setText("pending-entry-time-remaining", pending.time_remaining_seconds || 0);
    setText("pending-entry-status", pending.status || "NONE");
    setText("bot-last-action", data.last_action);
    setText("bot-trades-today", data.trades_today);
    renderReasonLog(data.reason_log);
}}

async function updateQuote() {{
    const data = await getJson(`/api/quote?symbol=${{CURRENT_SYMBOL}}`);
    const quote = data.quote;
    if (!quote) return;
    setText("quote-symbol", quote.symbol);
    setText("quote-last", fmtMoney(quote.last));
    setText("quote-bid", fmtMoney(quote.bid));
    setText("quote-ask", fmtMoney(quote.ask));
    setText("quote-volume", fmtInt(quote.volume));
    setText("quote-average-volume", fmtInt(quote.average_volume));
}}

async function updateCurrentPosition() {{
    const data = await getJson("/api/current-position");
    renderPositions(data.positions);
}}

async function updateMarketStructure() {{
    const data = await getJson("/api/market-structure");
    setText("bot-current-pl", fmtMoney(data.current_pl));
    setText("bot-distance-trailing", fmtMoney(data.distance_to_trailing_stop));
    setText("bot-range-size", fmtMoney(data.current_range_size));
    setText("bot-market-state", data.market_state);
    setText("bot-bullish-score", data.bullish_score);
    setText("bot-bearish-score", data.bearish_score);
    setText("bot-confidence", data.confidence);
    setText("bot-dominance", Number(data.dominance_percent || 0).toFixed(1));
    setText("bot-current-signal", data.current_signal);
    setText("market-structure-source", data.market_structure_source);
    setText("market-date", data.market_date);
    setText("market-structure-last-updated", data.market_structure_last_updated);
    renderMarketStructure(data);
}}

async function updateSelectedOptionQuote() {{
    const optionSymbol = document.getElementById("option_symbol_input")?.value;
    if (!optionSymbol) return;

    const data = await getJson(`/api/quote?symbol=${{optionSymbol}}`);
    const quote = data.quote;
    if (!quote) return;

    setText("bid", quote.bid ?? "N/A");
    setText("ask", quote.ask ?? "N/A");
    setText("last", quote.last ?? "N/A");
    setText("volume", quote.volume ?? "N/A");
    setText("open_interest", quote.open_interest ?? "N/A");
}}

async function updateDeveloperDiagnostics() {{
    const data = await getJson("/api/developer-diagnostics");
    const position = data.position_monitor || {{}};

    setText("health-engine-status", data.engine_running ? "🟢 Bot Running" : "🔴 Bot Stopped");
    setText("health-last-tick", data.last_tick || "N/A");
    setText("health-tick-duration", `${{fmtInt(data.last_tick_duration_ms)}} ms`);
    setText("health-last-quote-age", `${{fmtInt(data.last_quote_age_ms)}} ms ago`);
    setText("health-last-decision", data.last_decision || "DO NOTHING");
    setText("health-last-error", data.last_error || "None");
    setText("health-api-status", data.api_status || "Disconnected");
    setText("health-broker-status", data.broker_status || "Disconnected");
    setText("health-position-sync", data.position_sync || "UNKNOWN");

    const desynced = data.position_sync !== "OK";
    const warningEl = document.getElementById("position-monitor-warning");
    if (warningEl) warningEl.innerHTML = desynced ? '<div class="warning">🚨 DESYNC</div>' : "";
    setText("monitor-broker-position", data.broker_position ? "YES" : "NO");
    setText("monitor-local-position", data.internal_position ? "YES" : "NO");
    setText("monitor-position-status", desynced ? "🚨 DESYNC" : "IN SYNC");
    setText("monitor-total-contracts", data.current_total_option_contracts ?? 0);
    setText("monitor-max-contracts", data.max_open_contracts ?? 1);
    setText("monitor-cap-status", data.position_cap_status || "UNKNOWN");

    setText("exit-entry", fmtMoney(position.entry_price));
    setText("exit-current", fmtMoney(position.current_price));
    setText("exit-peak", fmtMoney(position.peak_price));
    setText("exit-trailing-stop", fmtMoney(position.trailing_stop_price));
    setText("exit-distance-stop", fmtPercent(position.distance_to_stop_percent));
    setText("exit-poll-interval", `${{fmtInt(position.poll_interval_ms)}} ms`);
    setText("exit-last-quote-age", `${{fmtInt(position.last_quote_age_ms)}} ms ago`);
    setText("exit-stop-armed", position.stop_armed ? "YES" : "NO");
    setText("exit-time-in-trade", position.hold_time || "N/A");

    setText("perf-average-quote", `${{fmtInt(data.average_quote_latency_ms)}} ms`);
    setText("perf-slowest-quote", `${{fmtInt(data.slowest_quote_latency_ms)}} ms`);
    setText("perf-quote-failed", data.quote_failed_count ?? 0);
    setText("perf-rate-limited", data.quote_rate_limited_count ?? 0);

    setText("timer-market-scan", `${{fmtInt(data.market_scan_ms)}} ms`);
    setText("timer-indicators", `${{fmtInt(data.indicators_ms)}} ms`);
    setText("timer-signal", `${{fmtInt(data.signal_ms)}} ms`);
    setText("timer-order-submit", `${{fmtInt(data.order_submit_ms)}} ms`);
    setText("timer-broker-confirm", `${{fmtInt(data.broker_confirm_ms)}} ms`);
}}

async function refreshLiveDashboard() {{
    try {{
        await Promise.all([
            updateBotState(),
            updateQuote(),
            updateCurrentPosition(),
            updateMarketStructure(),
            updateSelectedOptionQuote(),
            updateDeveloperDiagnostics()
        ]);
    }} catch (err) {{
        console.error("Live dashboard update failed", err);
    }}
}}

setInterval(refreshLiveDashboard, 1000);

async function loadExpirations() {{
    const res = await fetch(`/api/expirations?symbol=${{CURRENT_SYMBOL}}`);
    const data = await res.json();

    const expSelect = document.getElementById("expiration_select");
    expSelect.innerHTML = "";

    data.dates.forEach(date => {{
        const opt = document.createElement("option");
        opt.value = date;
        opt.textContent = date;
        expSelect.appendChild(opt);
    }});

    await loadChain();
}}

async function loadChain() {{
    const exp = document.getElementById("expiration_select").value;
    const type = document.getElementById("option_type_select").value;

    if (!exp) return;

    const res = await fetch(`/api/chain?symbol=${{CURRENT_SYMBOL}}&expiration=${{exp}}&option_type=${{type}}`);
    const data = await res.json();

    const strikeSelect = document.getElementById("strike_select");
    strikeSelect.innerHTML = "";

    data.options.forEach(o => {{
        const opt = document.createElement("option");
        opt.value = JSON.stringify(o);
        opt.textContent = `${{o.strike}} - ${{o.symbol}}`;
        strikeSelect.appendChild(opt);
    }});

    updateSelectedContract();
}}

function updateSelectedContract() {{
    const strikeSelect = document.getElementById("strike_select");

    if (!strikeSelect.value) return;

    const o = JSON.parse(strikeSelect.value);

    document.getElementById("selected_contract").textContent = o.symbol;
    document.getElementById("option_symbol_input").value = o.symbol;
    document.getElementById("bid").textContent = o.bid ?? "N/A";
    document.getElementById("ask").textContent = o.ask ?? "N/A";
    document.getElementById("last").textContent = o.last ?? "N/A";
    document.getElementById("volume").textContent = o.volume ?? "N/A";
    document.getElementById("open_interest").textContent = o.open_interest ?? "N/A";
}}

document.addEventListener("DOMContentLoaded", async () => {{
    await loadExpirations();
    await refreshLiveDashboard();

    document.getElementById("expiration_select").addEventListener("change", loadChain);
    document.getElementById("option_type_select").addEventListener("change", loadChain);
    document.getElementById("strike_select").addEventListener("change", updateSelectedContract);
}});
</script>

</body>
</html>
"""

    return html


if __name__ == "__main__":
    threading.Thread(target=surfer_bot_loop, daemon=True).start()
    app.run(host="127.0.0.1", port=5000)

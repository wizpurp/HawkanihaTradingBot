import re
import math
import copy
import hashlib
import uuid
from flask import Flask, request, redirect, jsonify
import requests
import json
import csv
import os
import shutil
import threading
import time
import html as html_lib
import traceback
import atexit
from functools import wraps
from broker_connection import BrokerGateway, BrokerConnectionError, resolve_connection, legacy_environment, entry_funds_limit
from dataclasses import asdict, replace
from datetime import datetime, timedelta, time as datetime_time
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from logs.trade_logger import *
from tournament.evaluator import evaluate_all_profiles, evaluate_profile
from tournament.execution import try_open_virtual_position
from tournament.exits import evaluate_virtual_exit, process_virtual_exits, stop_snapshot, update_virtual_position_price, valid_bid
from tournament.momentum import apply_tournament_momentum
from tournament.profiles import (
    PROFILE_ORDER,
    TOURNAMENT_PROFILES_FILE,
    build_tournament_profiles,
    copy_tournament_profile_settings,
    load_tournament_profile_settings,
    normalize_profile_settings,
    reset_tournament_profile_settings,
    save_tournament_profile_settings,
)
from tournament.recovery import (
    TEST_TYPE_PIPELINE_PROOF,
    apply_daily_resets,
    is_synthetic_proof_position,
    quote_is_fresh,
    reconcile_tournament_state,
    tournament_market_is_open,
)
from tournament.snapshot import build_market_snapshot_from_signal
from tournament.option_symbols import option_symbol_direction, option_symbol_matches_direction
from tournament import state as tournament_state_module
from tournament import trades as tournament_trades_module
from tournament.state import TOURNAMENT_STATE_FILE, create_all_initial_states, load_tournament_state, save_tournament_state
from tournament.trades import (
    TOURNAMENT_TRADES_FILE,
    append_tournament_trade,
    create_synthetic_tournament_trade,
    get_tournament_trade,
    list_tournament_trades,
    save_tournament_trades,
    update_tournament_trade,
)
from shadow_research.recorder import (
    SHADOW_CANDIDATE_QUOTES_FILE,
    SHADOW_CANDIDATES_FILE,
    recover_shadow_candidates,
    record_shadow_research,
    shadow_summary,
    update_shadow_gate_snapshot,
)

load_dotenv()


app = Flask(__name__)
MARKET_TZ = ZoneInfo("America/New_York")
CONFIG_SCHEMA_VERSION = 2
MARKET_CLOSED_SLEEP_SECONDS = 300


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
BROKER = BrokerGateway()
BROKER_OPERATION_LOCK = threading.RLock()
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_HISTORY_LIMIT = 20
BOT_AUDIT_FILE = os.path.join(APP_DIR, "bot_audit_log.csv")
EXIT_STATE_FILE = os.path.join(APP_DIR, "data", "option_exit_state.json")
EXIT_RECOVERY_FILE = os.path.join(APP_DIR, "data", "option_exit_recovery.json")
EXIT_STATE_VERSION = 3
EXIT_UNVERIFIED_SCOPE = "UNVERIFIED"
EXIT_CLOSED_FLAT_CONFIRMATIONS = 2
EXIT_PRICE_BASIS = "BID"
LEGACY_PEAK_BASIS = "LEGACY_UNVERIFIED"
EXIT_PRICE_TOLERANCE = 1e-9
DEFAULT_MAX_EXIT_QUOTE_AGE_SECONDS = 30
# Sandbox market data is delayed about 15 minutes by design.
DEFAULT_SANDBOX_MAX_EXIT_QUOTE_AGE_SECONDS = 1200
MAX_EXIT_QUOTE_FUTURE_SKEW_SECONDS = 5
DEFAULT_EXIT_ORDER_RETRY_SECONDS = 5
EXIT_FILL_PRICE_WAIT_SECONDS = 30
EXIT_INTENT_TAG_PREFIX = "hkexit-"
EXIT_UNSCOPED = "UNSCOPED"
EXIT_ORDER_LOOKUP_LIMIT = 1000
EXIT_ORDER_LOOKUP_MAX_PAGES = 5
EXIT_RECONCILE_INTERVAL_SECONDS = 5
EXIT_ORDER_CLOCK_SKEW_SECONDS = 120
# "No such order" needs several complete broker lists after the order had time to appear.
EXIT_ABSENCE_SETTLE_SECONDS = 30
EXIT_ABSENCE_CONFIRMATIONS = 3
EXIT_STATE_SAVE_LOCK = threading.RLock()
EXIT_STATE_ABSENT_CONFIRMATIONS = 3
EXIT_ENTRY_MATCH_TOLERANCE = 0.005
EXIT_ORDER_FAILED_STATUSES = {"rejected", "canceled", "cancelled", "expired", "error"}
BOT_AUDIT_VISIBLE_FILE = os.path.join(APP_DIR, "dashboard_bot_audit_visible.csv")
BOT_AUDIT_BACKUP_FILE = os.path.join(APP_DIR, "dashboard_bot_audit_last_cleared.csv")
PENDING_ENTRY_HISTORY_FILE = os.path.join(APP_DIR, "pending_entry_history.json")
PENDING_ENTRY_HISTORY_VISIBLE_FILE = os.path.join(APP_DIR, "dashboard_pending_entry_history_visible.json")
PENDING_ENTRY_HISTORY_BACKUP_FILE = os.path.join(APP_DIR, "dashboard_pending_entry_history_last_cleared.json")
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
    "entry_price_source", "estimated_entry_price",
    "profit_floor_enabled", "locked_profit_amount", "locked_profit_dollars",
    "required_premium_movement",
    "profit_floor_price", "profit_floor_activated",
    "percentage_trailing_stop", "effective_trailing_stop",
    "stop_control_rule"
]
BOT_LOCK = threading.Lock()
TOURNAMENT_LOCK = threading.RLock()
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
    "position_profit_floor_active": {},
    "position_effective_stops": {},
    "position_max_profit": {},
    "position_max_drawdown": {},
    "position_peak_basis": {},
    "position_exit_entry_prices": {},
    "pending_exit_orders": {},
    "exit_confirmed_remaining": {},
    "exit_intent_clear_result": "",
    "exit_closed_awaiting": {},
    "unscoped_exit_positions": {},
    "exit_recovery": None,
    "exit_quote_errors": {},
    "exit_retry_after": {},
    "exit_state_absent_counts": {},
    "exit_state_error": "",
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
        "id": "",
        "timestamp": "",
        "underlying": "",
        "direction": "",
        "decision": "",
        "option_symbol": "",
        "signal_generated": "",
        "momentum_status": "WAITING",
        "breakout_status": "PASS",
        "current_breakout_candle": 0,
        "required_breakout_candles": 0,
        "breakout_level": None,
        "starting_option_price": None,
        "confirmation_price_source": "",
        "required_confirmation_percent": 0,
        "confirmation_price": None,
        "current_option_price": None,
        "highest_option_price": None,
        "lowest_option_price": None,
        "current_momentum_gain_percent": 0,
        "current_pre_confirmation_drawdown_percent": 0,
        "momentum_confirmed": False,
        "breakout_confirmed": False,
        "maximum_allowed_drawdown_percent": 0,
        "momentum_timeout_seconds": 0,
        "elapsed_time_seconds": 0,
        "retry_cooldown_remaining_seconds": 0,
        "final_cancellation_trigger": "",
        "time_remaining_seconds": 0,
        "status": "NONE",
        "reason": "",
        "started_epoch": None,
        "updated_at": "",
        "expires_epoch": None,
        "contracts": 0,
        "contract": {},
        "market_context": {}
    },
    "pending_entry_history": [],
    "pending_entry_history_backup": [],
    "opening_range_confirmation": {
        "CALL": {
            "count": 0,
            "status": "WAITING",
            "level": None,
            "last_processed_candle": "",
            "reason": ""
        },
        "PUT": {
            "count": 0,
            "status": "WAITING",
            "level": None,
            "last_processed_candle": "",
            "reason": ""
        }
    },
    "tournament_decisions": {},
    "tournament_last_snapshot": {},
    "tournament_decisions_evaluated": 0,
    "tournament_decisions_evaluated_by_profile": {},
    "tournament_virtual_positions": [],
    "tournament_state_by_profile": {},
    "tournament_health": {},
    "market_session_open": None,
    "market_session_status": "UNKNOWN",
    "market_session_last_transition": "",
    "market_closed_sleep_seconds": MARKET_CLOSED_SLEEP_SECONDS,
    "shadow_research": {
        "candidates_today": 0,
        "completed_candidates": 0,
        "active_candidates": 0,
        "plus_5_before_minus_5_success_rate": 0,
        "average_5m_mfe": 0,
        "average_5m_mae": 0,
        "latest_candidates": [],
    }
}
TOURNAMENT_RUNTIME_STATES = {}
TOURNAMENT_RUNTIME_STATES_LOADED = False
TOURNAMENT_STATE_DIRTY = False
TOURNAMENT_EXIT_THREAD = None
TOURNAMENT_WATCHDOG_THREAD = None
TOURNAMENT_HEALTH = {
    "tournament_entry_loop_alive": False,
    "tournament_exit_loop_alive": False,
    "tournament_last_entry_cycle": None,
    "tournament_last_exit_poll": None,
    "tournament_last_successful_quote": None,
    "tournament_last_state_save": None,
    "tournament_last_error": None,
    "tournament_recovery_complete": False,
    "tournament_recovery_status": "NOT_RUN",
    "state_source": "EMPTY",
    "trades_source": "EMPTY",
    "state_save_failed": False,
    "stopping": False,
}


def clamp_int(value, minimum, maximum, default):
    try:
        number = int(value)
    except:
        number = default
    return max(minimum, min(maximum, number))


def normalize_history_config(config):
    history = config.setdefault("history", {})
    history["use_global_limit"] = bool(history.get("use_global_limit", True))
    history["global_limit"] = clamp_int(history.get("global_limit", DEFAULT_HISTORY_LIMIT), 1, 500, DEFAULT_HISTORY_LIMIT)
    history["pending_entry_limit"] = clamp_int(history.get("pending_entry_limit", history["global_limit"]), 1, 500, history["global_limit"])
    history["bot_trades_limit"] = clamp_int(history.get("bot_trades_limit", history["global_limit"]), 1, 500, history["global_limit"])
    history["trade_history_limit"] = clamp_int(history.get("trade_history_limit", history["global_limit"]), 1, 500, history["global_limit"])
    history["bot_audit_limit"] = clamp_int(history.get("bot_audit_limit", history["global_limit"]), 1, 500, history["global_limit"])
    return history


def history_limit(config, key):
    history = normalize_history_config(config)
    return history["global_limit"] if history.get("use_global_limit", True) else history.get(key, history["global_limit"])


def backup_config_file(path="config.json"):
    if not os.path.exists(path):
        return
    backup_path = path[:-5] + ".backup.json" if path.endswith(".json") else path + ".backup"
    try:
        shutil.copy2(path, backup_path)
    except OSError:
        pass


def migrate_original_money_config(config):
    schema_version = int(config.get("config_schema_version", 1) or 1)
    migrated = schema_version < CONFIG_SCHEMA_VERSION
    strategy = config.setdefault("strategy", {})

    if "maximum_position_cost_dollars" not in config:
        if "max_contract_price" in config:
            contracts = max(1, int(config.get("contracts", 1) or 1))
            config["maximum_position_cost_dollars"] = safe_float(config.get("max_contract_price", 1.0), 1.0) * 100 * contracts
            migrated = True
        else:
            config["maximum_position_cost_dollars"] = 100.0

    if "locked_profit_dollars" not in strategy:
        if "locked_profit_amount" in strategy:
            contracts = max(1, int(config.get("contracts", 1) or 1))
            strategy["locked_profit_dollars"] = safe_float(strategy.get("locked_profit_amount", 0.0), 0.0) * 100 * contracts
            migrated = True
        else:
            strategy["locked_profit_dollars"] = 0.0

    config.pop("max_contract_price", None)
    strategy.pop("locked_profit_amount", None)
    config["config_schema_version"] = CONFIG_SCHEMA_VERSION
    return migrated


def option_position_cost(option_premium, contracts):
    return float(option_premium) * 100 * int(contracts or 1)


def locked_profit_premium_increment(locked_profit_dollars, contracts):
    contracts = max(1, int(contracts or 1))
    return float(locked_profit_dollars or 0.0) / (100 * contracts)


def load_config():
    with open("config.json", "r") as f:
        config = json.load(f)
    migrated = migrate_original_money_config(config)

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
    # New automatic entries are a separate, opt-in switch. bot_enabled must stay on for
    # exit management of open positions; this flag only controls opening new ones.
    config.setdefault("auto_entries_enabled", False)
    config.setdefault("broker_environment", legacy_environment())
    config["mode"] = config["broker_environment"]
    config.setdefault("require_realtime_entry_quotes", False)
    config.setdefault("allow_delayed_sandbox_entries", False)
    config.setdefault("max_entry_quote_age_seconds", 30)
    config.setdefault("execution_anomaly_threshold_percent", 15.0)
    config.setdefault("strategy_mode", "SURFER")
    config.setdefault("scanner", {"interval_seconds": 60})
    config.setdefault("strategy", {})
    normalize_history_config(config)
    config.setdefault("decision_time", "09:35")
    config.setdefault("bot_budget", 100)
    config.setdefault("human_daily_trading_budget", 500)
    config.setdefault("bot_starting_account_balance", 500)
    config.setdefault("human_starting_account_balance", 500)
    config["maximum_position_cost_dollars"] = max(0.0, safe_float(config.get("maximum_position_cost_dollars", 100.0), 100.0))
    config["max_open_contracts"] = clamp_int(config.get("max_open_contracts", 1), 1, 5, 1)
    config.setdefault("contract_selection_mode", "strict_atm")
    config["option_momentum_confirmation_enabled"] = bool(config.get("option_momentum_confirmation_enabled", True))
    config["option_momentum_percent"] = max(0.1, min(20.0, safe_float(config.get("option_momentum_percent", 1.0), 1.0)))
    config["confirmation_timeout_seconds"] = clamp_int(config.get("confirmation_timeout_seconds", 60), 1, 300, 60)
    config["pre_confirmation_max_drawdown_percent"] = max(0.0, min(50.0, safe_float(config.get("pre_confirmation_max_drawdown_percent", 5.0), 5.0)))
    config["pending_entry_retry_cooldown_seconds"] = clamp_int(config.get("pending_entry_retry_cooldown_seconds", 60), 0, 600, 60)
    config["two_candle_or_confirmation_enabled"] = bool(config.get("two_candle_or_confirmation_enabled", True))
    config["required_breakout_candles"] = clamp_int(config.get("required_breakout_candles", 2), 1, 10, 2)
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
    config["strategy"]["enable_profit_floor_trailing_stop"] = bool(config["strategy"].get("enable_profit_floor_trailing_stop", True))
    config["strategy"]["locked_profit_dollars"] = max(0.0, safe_float(config["strategy"].get("locked_profit_dollars", 0.0), 0.0))
    config["strategy"]["exit_poll_interval_ms"] = clamp_int(
        config["strategy"].get("exit_poll_interval_ms", 1000),
        100,
        5000,
        1000
    )
    config["strategy"].setdefault("direction_threshold_percent", 60)
    if migrated:
        print(f"ORIGINAL CONFIG MIGRATION: upgraded config.json to schema {CONFIG_SCHEMA_VERSION}")
        backup_config_file("config.json")
        save_config(config)

    return config


def save_config(config):
    with open("config.json", "w") as f:
        json.dump(config, f, indent=4)


TOURNAMENT_BOOL_FIELDS = {
    "enabled",
    "option_momentum_confirmation_enabled",
    "two_candle_or_confirmation_enabled",
    "allow_calls",
    "allow_puts",
    "enable_profit_floor_trailing_stop",
}
TOURNAMENT_SETTING_FIELDS = [
    "display_name",
    "enabled",
    "minimum_confidence",
    "minimum_dominance_percent",
    "minimum_signals",
    "direction_threshold_percent",
    "option_momentum_confirmation_enabled",
    "option_momentum_percent",
    "confirmation_timeout_seconds",
    "pre_confirmation_max_drawdown_percent",
    "pending_entry_retry_cooldown_seconds",
    "two_candle_or_confirmation_enabled",
    "required_breakout_candles",
    "allow_calls",
    "allow_puts",
    "hard_stop_percent",
    "trailing_stop_percent",
    "enable_profit_floor_trailing_stop",
    "locked_profit_dollars",
    "cooldown_minutes",
    "max_trades_per_day",
    "maximum_position_cost_dollars",
    "contract_selection_mode",
    "contracts",
]


def tournament_settings_response(settings=None):
    return {
        "profiles": settings or load_tournament_profile_settings(TOURNAMENT_PROFILES_FILE),
        "profile_order": list(PROFILE_ORDER),
    }


def parse_tournament_settings_payload():
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        raw_profiles = payload.get("profiles", payload)
        if not isinstance(raw_profiles, dict):
            raise ValueError("profiles must be an object")
        return raw_profiles

    defaults = load_tournament_profile_settings(TOURNAMENT_PROFILES_FILE)
    parsed = {}
    for profile_id in PROFILE_ORDER:
        row = dict(defaults.get(profile_id, {}))
        for field in TOURNAMENT_SETTING_FIELDS:
            key = f"tournament__{profile_id}__{field}"
            if field in TOURNAMENT_BOOL_FIELDS:
                row[field] = request.form.get(key) == "on"
            elif key in request.form:
                row[field] = request.form.get(key)
        row["profile_id"] = profile_id
        parsed[profile_id] = row
    return parsed


def tournament_api_or_redirect(payload, status=200):
    wants_json = request.is_json or "application/json" in str(request.headers.get("Accept", ""))
    if wants_json:
        return jsonify(payload), status
    return redirect("/")


def tournament_trade_to_dict(trade):
    return asdict(trade)


def tournament_trades_response(trades):
    rows = [tournament_trade_to_dict(trade) for trade in trades]
    return {
        "ok": True,
        "count": len(rows),
        "trades": rows,
    }


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
    decision = market_context.get("decision", "")
    selected_direction = "CALL" if decision == "BUY CALL" else "PUT" if decision == "BUY PUT" else score_direction(bullish_score, bearish_score)
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

    bullish_reason_phrases = [
        "EMA aligned bullish",
        "MA aligned bullish",
        "MACD bullish",
        "Price above VWAP",
        "Volume confirmation bullish",
        "Green ticks",
        "Broke previous day high",
        "Broke previous week high",
        "Broke last hour high",
        "Broke opening range high"
    ]
    bearish_reason_phrases = [
        "EMA aligned bearish",
        "MA aligned bearish",
        "MACD bearish",
        "Price below VWAP",
        "Volume confirmation bearish",
        "Red ticks",
        "Broke previous day low",
        "Broke previous week low",
        "Broke last hour low",
        "Broke opening range low"
    ]
    matching_phrases = bullish_reason_phrases if selected_direction == "CALL" else bearish_reason_phrases if selected_direction == "PUT" else []
    opposing_phrases = bearish_reason_phrases if selected_direction == "CALL" else bullish_reason_phrases if selected_direction == "PUT" else []
    matching_confirmations = sum(1 for phrase in matching_phrases if any(phrase in reason for reason in reasons))
    opposing_confirmations = sum(1 for phrase in opposing_phrases if any(phrase in reason for reason in reasons))
    score += min(15, matching_confirmations * 3)
    if opposing_confirmations:
        score -= min(30, opposing_confirmations * 6)
        grade_reasons.append(f"{opposing_confirmations} opposing confirmations")
    if (selected_direction == "CALL" and bearish_score >= bullish_score) or (selected_direction == "PUT" and bullish_score >= bearish_score):
        score -= 30
        grade_reasons.append("Entry direction conflicts with score winner")

    def has_matching_phrase(phrases):
        return any(phrase in reason for phrase in phrases for reason in reasons)

    if has_matching_phrase([phrase for phrase in matching_phrases if phrase.startswith("Volume confirmation")]):
        score += 4
        grade_reasons.append("Volume confirmation")
    if has_matching_phrase([phrase for phrase in matching_phrases if phrase in ["Green ticks", "Red ticks"]]):
        score += 4
        grade_reasons.append("Opening direction confirmation")
    if has_matching_phrase([phrase for phrase in matching_phrases if phrase in [
        "Broke previous day high", "Broke previous week high", "Broke last hour high",
        "Broke previous day low", "Broke previous week low", "Broke last hour low"
    ]]):
        score += 5
        grade_reasons.append("Market structure confirmation")

    if market_state in ["CHOPPY", "STAGNANT"]:
        score = min(score, 59)
    elif confidence <= 1:
        score = min(score, 69)
    elif confidence == 2:
        score = min(score, 79)
    elif confidence == 3:
        score = min(score, 89)
    elif confidence == 4:
        score = min(score, 94)

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
    # Display/logging callers pass whatever price they have; this must stay read-only.
    config = config or load_config()
    return calculate_stop_state(symbol, entry_price, current_price, config, update_state=False)


def calculate_stop_state(symbol, entry_price, current_price, config=None, update_state=False, price_basis=None):
    """Compute stop levels. Shared peak/stop state changes only for a validated bid.

    Writing requires update_state=True AND price_basis=EXIT_PRICE_BASIS, so a caller
    holding a last, ask or fallback price cannot contaminate the stored values.
    """
    config = config or load_config()
    strategy = config.get("strategy", {})
    hard_stop_percent = float(strategy.get("hard_stop_percent", 20))
    trailing_stop_percent = float(strategy.get("trailing_stop_percent", 15))
    profit_floor_enabled = bool(strategy.get("enable_profit_floor_trailing_stop", True))
    contracts = max(1, int(config.get("contracts", 1) or 1))
    locked_profit_dollars = max(0.0, safe_float(strategy.get("locked_profit_dollars", 0.0), 0.0))
    required_premium_movement = locked_profit_premium_increment(locked_profit_dollars, contracts)

    if update_state and (price_basis != EXIT_PRICE_BASIS or current_price is None or exit_broker_scope(config) is None):
        print("STOP STATE WRITE REFUSED:", symbol, "price_basis:", price_basis, "price:", current_price)
        update_state = False
    state_key = exit_state_key(symbol, config)

    with BOT_LOCK:
        had_stored_peak = state_key in BOT_STATE["position_peaks"]
        peak_price = BOT_STATE["position_peaks"].get(state_key, entry_price)
        stored_peak = peak_price
        existing_profit_floor_active = bool(BOT_STATE["position_profit_floor_active"].get(state_key, False))
        existing_effective_stop = BOT_STATE["position_effective_stops"].get(state_key)
        peak_basis = BOT_STATE["position_peak_basis"].get(state_key)
        stored_entry = BOT_STATE["position_exit_entry_prices"].get(state_key)

    if had_stored_peak and not peak_basis:
        peak_basis = LEGACY_PEAK_BASIS

    if current_price is not None:
        peak_price = max(peak_price or entry_price, current_price)

    hard_stop_price = entry_price * (1 - hard_stop_percent / 100) if entry_price is not None else None
    percentage_trailing_stop = peak_price * (1 - trailing_stop_percent / 100) if peak_price is not None else None
    profit_floor_price = entry_price + required_premium_movement if profit_floor_enabled and entry_price is not None else None
    profit_floor_activated = (
        profit_floor_enabled
        and (
            existing_profit_floor_active
            or (
                peak_price is not None
                and profit_floor_price is not None
                and peak_price >= profit_floor_price
                and entry_price is not None
                and peak_price > entry_price
            )
        )
    )
    trailing_stop_price = percentage_trailing_stop
    stop_control_rule = "HARD STOP"

    if profit_floor_activated and profit_floor_price is not None and percentage_trailing_stop is not None:
        trailing_stop_price = max(percentage_trailing_stop, profit_floor_price)
        if existing_effective_stop is not None:
            trailing_stop_price = max(trailing_stop_price, existing_effective_stop)
        stop_control_rule = "PROFIT FLOOR" if profit_floor_price >= percentage_trailing_stop else "PERCENTAGE TRAILING STOP"
    elif percentage_trailing_stop is not None and entry_price is not None and percentage_trailing_stop >= entry_price:
        stop_control_rule = "PERCENTAGE TRAILING STOP"

    stop_armed = trailing_stop_price >= entry_price if trailing_stop_price is not None and entry_price is not None else False
    drawdown_from_peak_percent = ((peak_price - current_price) / peak_price) * 100 if peak_price and current_price is not None else 0

    if not had_stored_peak or (current_price is not None and current_price > (stored_peak or 0)):
        peak_basis = EXIT_PRICE_BASIS if update_state else peak_basis

    if update_state:
        with BOT_LOCK:
            changed = (
                not had_stored_peak
                or peak_price != stored_peak
                or profit_floor_activated != existing_profit_floor_active
                or peak_basis != BOT_STATE["position_peak_basis"].get(state_key)
                or stored_entry != entry_price
                or (profit_floor_activated and trailing_stop_price != existing_effective_stop)
            )
            BOT_STATE["position_peaks"][state_key] = peak_price
            BOT_STATE["position_peak_basis"][state_key] = peak_basis
            BOT_STATE["position_exit_entry_prices"][state_key] = entry_price
            BOT_STATE["position_profit_floor_active"][state_key] = profit_floor_activated
            if profit_floor_activated and trailing_stop_price is not None:
                BOT_STATE["position_effective_stops"][state_key] = trailing_stop_price
        if changed:
            persist_exit_state()

    return {
        "hard_stop_percent": hard_stop_percent,
        "hard_stop_price": hard_stop_price,
        "peak_price": peak_price,
        "peak_basis": peak_basis or "",
        "trailing_stop_percent": trailing_stop_percent,
        "trailing_stop_price": trailing_stop_price,
        "percentage_trailing_stop": percentage_trailing_stop,
        "effective_trailing_stop": trailing_stop_price,
        "current_price": current_price,
        "stop_armed": stop_armed,
        "drawdown_from_peak_percent": drawdown_from_peak_percent,
        "profit_floor_enabled": profit_floor_enabled,
        "locked_profit_dollars": locked_profit_dollars,
        "locked_profit_amount": locked_profit_dollars,
        "required_premium_movement": required_premium_movement,
        "profit_floor_activated": profit_floor_activated,
        "profit_floor_price": profit_floor_price,
        "stop_control_rule": stop_control_rule
    }


def current_exit_display(symbol, stop_values, quote_data_error=""):
    if quote_data_error:
        return "QUOTE DATA ERROR", f"{quote_data_error}\nStops are NOT being evaluated until a valid, fresh bid returns."

    context = current_market_context_snapshot()
    decision = context.get("decision") or "HOLD"
    reasons = context.get("decision_reasons") or []
    drawdown = stop_values.get("drawdown_from_peak_percent", 0)
    trailing_percent = stop_values.get("trailing_stop_percent", 0)
    stop_armed = stop_values.get("stop_armed", False)
    current_price = stop_values.get("current_price")
    effective_stop = stop_values.get("effective_trailing_stop")
    stop_control_rule = stop_values.get("stop_control_rule", "HARD STOP")

    if not stop_armed:
        trailing_reasons = ["Trailing stop inactive.", "Effective stop has not reached entry."]
    else:
        trailing_reasons = (
            ["Trailing stop hit.", f"Current {current_price:.2f} <= effective stop {effective_stop:.2f}.", f"Rule: {stop_control_rule}."]
            if current_price is not None and effective_stop is not None and current_price <= effective_stop
            else ["Trailing stop not hit.", f"Drawdown {drawdown:.2f}% < {trailing_percent:.2f}%.", f"Rule: {stop_control_rule}."]
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

    if market_context.get("broker_realized_pnl_percent") is not None:
        pnl_percent = market_context["broker_realized_pnl_percent"]

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


def write_audit_header(path):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=BOT_AUDIT_COLUMNS)
        writer.writeheader()


def ensure_audit_view_file(path):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        write_audit_header(path)
        return

    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames or []

    if fieldnames == BOT_AUDIT_COLUMNS:
        return

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=BOT_AUDIT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in BOT_AUDIT_COLUMNS})


def append_audit_row(path, row):
    ensure_audit_view_file(path)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=BOT_AUDIT_COLUMNS)
        writer.writerow({column: row.get(column, "") for column in BOT_AUDIT_COLUMNS})


def load_bot_audit_view_rows(limit=None):
    try:
        ensure_audit_file()
        if os.path.exists(BOT_AUDIT_VISIBLE_FILE):
            ensure_audit_view_file(BOT_AUDIT_VISIBLE_FILE)
        visible_path = BOT_AUDIT_VISIBLE_FILE if os.path.exists(BOT_AUDIT_VISIBLE_FILE) else BOT_AUDIT_FILE
        with open(visible_path, "r", newline="") as f:
            rows = list(csv.DictReader(f))
        return rows[-limit:] if limit else rows
    except:
        return []


def clear_bot_audit_view():
    current_rows = load_bot_audit_view_rows(limit=None)
    with open(BOT_AUDIT_BACKUP_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=BOT_AUDIT_COLUMNS)
        writer.writeheader()
        for row in current_rows:
            writer.writerow({column: row.get(column, "") for column in BOT_AUDIT_COLUMNS})
    write_audit_header(BOT_AUDIT_VISIBLE_FILE)


def restore_bot_audit_view():
    if not os.path.exists(BOT_AUDIT_BACKUP_FILE):
        return False
    ensure_audit_view_file(BOT_AUDIT_BACKUP_FILE)
    with open(BOT_AUDIT_BACKUP_FILE, "r", newline="") as src:
        backup_rows = list(csv.DictReader(src))

    with open(BOT_AUDIT_VISIBLE_FILE, "w", newline="") as dst:
        writer = csv.DictWriter(dst, fieldnames=BOT_AUDIT_COLUMNS)
        writer.writeheader()
        for row in backup_rows:
            writer.writerow({column: row.get(column, "") for column in BOT_AUDIT_COLUMNS})
    return True


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
        "estimated_entry_price": extra.get("estimated_entry_price", ""),
        "profit_floor_enabled": extra.get("profit_floor_enabled", ""),
        "locked_profit_amount": extra.get("locked_profit_amount", ""),
        "locked_profit_dollars": extra.get("locked_profit_dollars", extra.get("locked_profit_amount", "")),
        "required_premium_movement": extra.get("required_premium_movement", ""),
        "profit_floor_price": extra.get("profit_floor_price", ""),
        "profit_floor_activated": extra.get("profit_floor_activated", ""),
        "percentage_trailing_stop": extra.get("percentage_trailing_stop", ""),
        "effective_trailing_stop": extra.get("effective_trailing_stop", ""),
        "stop_control_rule": extra.get("stop_control_rule", "")
    }

    with open(BOT_AUDIT_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=BOT_AUDIT_COLUMNS)
        writer.writerow({column: row.get(column, "") for column in BOT_AUDIT_COLUMNS})
    if os.path.exists(BOT_AUDIT_VISIBLE_FILE):
        append_audit_row(BOT_AUDIT_VISIBLE_FILE, row)


def update_last_trade_review(review):
    with BOT_LOCK:
        BOT_STATE["last_trade_review"] = review


def option_market_is_open():
    return tournament_market_is_open(time.time())


def update_market_session_state(is_open, log_transition=True):
    status = "OPEN" if is_open else "CLOSED"
    message = None
    with BOT_LOCK:
        previous = BOT_STATE.get("market_session_open")
        if previous is not is_open:
            if log_transition:
                if is_open:
                    message = "Market open — scanner resumed"
                else:
                    message = "Market closed — scanner sleeping"
            BOT_STATE["market_session_open"] = is_open
            BOT_STATE["market_session_status"] = status
            BOT_STATE["market_session_last_transition"] = market_now().isoformat()
        else:
            BOT_STATE["market_session_status"] = status
    if message:
        print(message)
    return is_open


def scanner_market_is_open(log_transition=True):
    return update_market_session_state(option_market_is_open(), log_transition=log_transition)


def update_shadow_research(signal=None, call_contract=None, put_contract=None, spy_price=None):
    if not scanner_market_is_open(log_transition=False):
        return {"created": False, "updated": False, "active": 0, "completed": 0}

    if spy_price is None and put_contract is not None and not isinstance(put_contract, dict):
        spy_price = put_contract
        put_contract = None

    config = load_config()
    signal = dict(signal or {})
    if not signal.get("symbol"):
        signal["symbol"] = config.get("symbol", "SPY")
    contracts_by_direction = {
        "CALL": call_contract,
        "PUT": put_contract,
    }
    gate_snapshots_by_direction = {
        "CALL": shadow_entry_gate_snapshot(config, "CALL", call_contract),
        "PUT": shadow_entry_gate_snapshot(config, "PUT", put_contract),
    }
    summary = record_shadow_research(
        signal,
        contracts_by_direction,
        get_market_quote,
        spy_price,
        True,
        time.time(),
        market_now(),
        SHADOW_CANDIDATES_FILE,
        SHADOW_CANDIDATE_QUOTES_FILE,
        gate_snapshots_by_direction,
    )
    with BOT_LOCK:
        BOT_STATE["shadow_research"] = shadow_summary(
            SHADOW_CANDIDATES_FILE,
            today=market_now().date().isoformat(),
            limit=20,
        )
    return summary


def shadow_entry_gate_snapshot(config, direction, contract=None, pending_override=None):
    contract = contract if isinstance(contract, dict) else {}
    option_symbol = contract.get("symbol") or (pending_override or {}).get("option_symbol", "")
    pending = dict(pending_override or get_pending_entry())
    pending_matches = (
        pending.get("option_symbol") == option_symbol
        and pending.get("direction") == direction
        and pending.get("status") not in ["NONE", ""]
    )
    momentum_enabled = bool(config.get("option_momentum_confirmation_enabled", True))
    breakout_enabled = bool(config.get("two_candle_or_confirmation_enabled", True))
    retry_remaining = pending_retry_cooldown_remaining(config, direction, option_symbol) if option_symbol else 0
    momentum_state = "DISABLED" if not momentum_enabled else "WAITING"
    breakout_state = "DISABLED" if not breakout_enabled else "WAITING"
    observed = "N/A"
    breakout_current = 0 if breakout_enabled else "N/A"
    breakout_required = int(config.get("required_breakout_candles", 2)) if breakout_enabled else "N/A"
    max_observed_drawdown = "N/A"
    configured_max_drawdown = safe_float(config.get("pre_confirmation_max_drawdown_percent", 5.0), 5.0)
    timed_out = False
    timeout_elapsed = "N/A"
    drawdown_failed = False
    final_outcome = ""

    if pending_matches:
        momentum_state = "DISABLED" if not momentum_enabled else str(pending.get("momentum_status") or "WAITING").upper()
        breakout_state = "DISABLED" if not breakout_enabled else str(pending.get("breakout_status") or "WAITING").upper()
        observed = pending.get("current_momentum_gain_percent", "N/A")
        breakout_current = pending.get("current_breakout_candle", breakout_current)
        breakout_required = pending.get("required_breakout_candles", breakout_required)
        max_observed_drawdown = pending.get("current_pre_confirmation_drawdown_percent", "N/A")
        configured_max_drawdown = pending.get("maximum_allowed_drawdown_percent", configured_max_drawdown)
        trigger = str(pending.get("final_cancellation_trigger") or pending.get("reason") or "").lower()
        timed_out = "timeout" in trigger
        drawdown_failed = "drawdown" in trigger
        if timed_out:
            timeout_elapsed = pending.get("elapsed_time_seconds", "N/A")
            momentum_state = "TIMEOUT" if momentum_state != "PASS" else momentum_state

    if retry_remaining > 0:
        final_outcome = "REJECTED_COOLDOWN"
    elif timed_out:
        final_outcome = "REJECTED_TIMEOUT"
    elif drawdown_failed:
        final_outcome = "REJECTED_DRAWDOWN"
    elif momentum_state == "FAIL":
        final_outcome = "REJECTED_MOMENTUM"
    elif breakout_state == "FAIL":
        final_outcome = "REJECTED_BREAKOUT"
    elif momentum_state in ["PASS", "DISABLED"] and breakout_state in ["PASS", "DISABLED"]:
        final_outcome = "WOULD_ENTER"
    elif option_symbol:
        final_outcome = "WAITING"
    else:
        final_outcome = "N/A"

    return {
        "option_symbol": option_symbol,
        "momentum_confirmation_state": momentum_state,
        "momentum_percentage_observed": observed,
        "momentum_percentage_required": safe_float(config.get("option_momentum_percent", 1.0), 1.0) if momentum_enabled else "N/A",
        "breakout_confirmation_state": breakout_state,
        "breakout_progress_current_candles": breakout_current,
        "breakout_progress_required_candles": breakout_required,
        "pre_confirmation_max_observed_drawdown_percent": max_observed_drawdown,
        "pre_confirmation_configured_max_drawdown_percent": configured_max_drawdown,
        "pre_confirmation_drawdown_failed": drawdown_failed,
        "confirmation_timed_out": timed_out,
        "confirmation_timeout_elapsed_seconds": timeout_elapsed,
        "retry_cooldown_active": retry_remaining > 0,
        "retry_cooldown_remaining_seconds": retry_remaining,
        "final_live_gate_outcome": final_outcome,
    }


def record_shadow_gate_state_from_pending(config, pending):
    try:
        direction = pending.get("direction")
        option_symbol = pending.get("option_symbol")
        if direction not in ["CALL", "PUT"] or not option_symbol:
            return False
        snapshot = shadow_entry_gate_snapshot(config, direction, {"symbol": option_symbol}, pending_override=pending)
        return update_shadow_gate_snapshot(direction, option_symbol, snapshot, time.time(), SHADOW_CANDIDATES_FILE)
    except Exception as error:
        print("SHADOW GATE SNAPSHOT ERROR:", error)
        return False


def recover_shadow_research_on_startup():
    if not scanner_market_is_open(log_transition=False):
        with BOT_LOCK:
            BOT_STATE["shadow_research"] = shadow_summary(
                SHADOW_CANDIDATES_FILE,
                today=market_now().date().isoformat(),
                limit=20,
            )
        return False

    def spy_price_provider(symbol):
        return get_quote_price(get_market_quote(symbol))

    changed = recover_shadow_candidates(
        get_market_quote,
        spy_price_provider,
        True,
        time.time(),
        SHADOW_CANDIDATES_FILE,
        SHADOW_CANDIDATE_QUOTES_FILE,
    )
    with BOT_LOCK:
        BOT_STATE["shadow_research"] = shadow_summary(
            SHADOW_CANDIDATES_FILE,
            today=market_now().date().isoformat(),
            limit=20,
        )
    return changed


def submit_and_parse_option_order(option_symbol, qty, action, label, tag=None):
    recovery_block = exit_recovery_block_reason()
    if recovery_block:
        print(f"{label} ORDER BLOCKED:", recovery_block)
        add_bot_reason(f"{label} ORDER BLOCKED: {recovery_block}")
        return False, "RECOVERY_REQUIRED", 0, recovery_block

    if action == "buy_to_open":
        allowed, reason, current_total, max_open_contracts = validate_buy_position_cap(qty)
        if not allowed:
            text = f"{reason}; Current: {current_total}; Maximum: {max_open_contracts}"
            print(f"{label} ORDER BLOCKED:", reason)
            print("current_total_option_contracts:", current_total)
            print("max_open_contracts:", max_open_contracts)
            add_bot_reason(f"{label} BUY skipped: {reason}; Current: {current_total}; Maximum: {max_open_contracts}")
            return False, reason, 0, text

    status, text = submit_option_order(option_symbol, qty, action, tag=tag) if tag else submit_option_order(option_symbol, qty, action)
    print(f"{label} ORDER STATUS:", status)
    print(f"{label} ORDER RESPONSE:", text)
    ok, order_status = parse_order_response(status, text, label)
    return ok, order_status, status, text


def broker_session(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        with BROKER_OPERATION_LOCK:
            return function(*args, **kwargs)
    return guarded


def broker_request(method, path, **kwargs):
    with BROKER_OPERATION_LOCK:
        request_config = load_config()
        connection = resolve_connection(request_config)
        if method == "POST" and path == "/accounts/{account}/orders":
            recovery_block = exit_recovery_block_reason(request_config)
            if recovery_block:
                raise BrokerConnectionError(recovery_block)
        if method == "POST" and kwargs.get("data", {}).get("side") in ("buy", "buy_to_open") and not BROKER.account(connection)["validated"]:
            raise BrokerConnectionError("Broker account is not validated; order not submitted")
        try:
            response = BROKER.request(connection, method, path, **kwargs)
        except BrokerConnectionError:
            if path == "/markets/quotes":
                BROKER.quote_failed(connection, "Quote request failed")
            raise
        if path == "/markets/quotes":
            try:
                quote = response.json()["quotes"]["quote"]
                quote = quote[0] if isinstance(quote, list) else quote
                if response.status_code != 200 or not isinstance(quote, dict):
                    raise ValueError()
                BROKER.observe_quote(connection, quote)
            except Exception:
                BROKER.quote_failed(connection, "Quote unavailable or invalid")
        return response


def headers():
    return {
        "Accept": "application/json"
    }


def connected_account_snapshot():
    config = load_config()
    try:
        connection = resolve_connection(config)
        status = BROKER.account(connection)
        with BROKER.lock:
            cached_quote = dict(BROKER.quotes.get(connection.key, {}))
        quote = cached_quote.get("quote", {})
        observation = quote_execution_snapshot(quote)
        age = observation.get("quote_age_seconds")
        fetched = cached_quote.get("success")
        fresh = isinstance(age, (int, float)) and 0 <= age <= 30 and fetched is not None and time.time() - fetched <= 60
        status["market_data"] = {
            "environment": "SANDBOX — DELAYED (~15 minutes)" if connection.environment == "sandbox" else "PRODUCTION (freshness verified separately)",
            "symbol": quote.get("symbol"), "timestamp": observation.get("quote_timestamp"),
            "bid_timestamp": broker_timestamp(quote.get("bid_date")),
            "ask_timestamp": broker_timestamp(quote.get("ask_date")),
            "trade_timestamp": broker_timestamp(quote.get("trade_date")),
            "age_seconds": age, "price_source": observation.get("price_source"),
            "status": "CONNECTION ERROR" if cached_quote.get("error") else ("FRESH" if fresh else "STALE / UNVERIFIED"),
            "last_success": datetime.fromtimestamp(fetched, MARKET_TZ).isoformat() if fetched else None,
            "error": cached_quote.get("error"),
        }
    except BrokerConnectionError as error:
        status = {"environment_label": "UNKNOWN / CONNECTION ERROR", "masked_account": "N/A",
                  "validated": False, "stale": True, "balances": {}, "error": str(error),
                  "market_data": {}, "connection_status": "UNVERIFIED / ERROR"}
    status["bot_budget"] = config.get("bot_budget")
    status["bot_enabled"] = bool(config.get("bot_enabled"))
    status["auto_entries_enabled"] = bool(config.get("auto_entries_enabled"))
    with BOT_LOCK:
        spent_today = BOT_STATE.get("spent_today", 0)
    status["bot_spent_today"] = spent_today
    limit, binding = entry_funds_limit(config.get("bot_budget"), spent_today,
                                       status.get("balances", {}).get("entry_buying_power") if status.get("validated") else None)
    status["max_new_entry_cost"], status["max_new_entry_limited_by"] = limit, binding
    return status


def render_connected_account(status):
    fields = {
        "Total Account Value": "total_account_value", "Cash": "cash", "Settled Cash": "settled_cash",
        "Available Options Buying Power": "options_buying_power", "Cash Available (broker field)": "cash_available",
        "Positions Value": "positions_value", "Unrealized P/L": "unrealized_pnl",
    }
    balances = status.get("balances", {})
    metrics = {label: fmt_trade_price(balances.get(key)) for label, key in fields.items()}
    metrics.update({"Account Connection": status.get("connection_status", "N/A"),
                    "Balance Status": "STALE / UNVERIFIED" if status.get("stale") else "CURRENT",
                    "Last Successful Balance Update": status.get("last_success") or "N/A",
                    "Balance Age (seconds)": status.get("age_seconds") if status.get("age_seconds") is not None else "N/A",
                    "Configured Bot Budget (not account funds)": fmt_trade_price(status.get("bot_budget")),
                    "Bot Spent Today": fmt_trade_price(status.get("bot_spent_today")),
                    "Broker Buying Power Used For Entries": fmt_trade_price(balances.get("entry_buying_power")),
                    "Max New Bot Entry Cost": fmt_trade_price(status.get("max_new_entry_cost")),
                    "Entry Limit Set By": status.get("max_new_entry_limited_by") or "N/A"})
    market = status.get("market_data", {})
    quote_metrics = {"Market Data Environment": market.get("environment") or "UNKNOWN",
                     "Quote Status": market.get("status") or "UNVERIFIED", "Quote Symbol": market.get("symbol") or "N/A",
                     "Broker Quote Timestamp": market.get("timestamp") or "N/A",
                     "Quote Price Source": market.get("price_source") or "N/A",
                     "Quote Age (seconds)": market.get("age_seconds") if market.get("age_seconds") not in (None, "") else "N/A",
                     "Broker Bid Timestamp": market.get("bid_timestamp") or "N/A",
                     "Broker Ask Timestamp": market.get("ask_timestamp") or "N/A",
                     "Broker Last Trade Timestamp": market.get("trade_timestamp") or "N/A",
                     "Last Successful Quote Update": market.get("last_success") or "N/A"}
    return (f'<h3>{escape_html(status["environment_label"])}</h3>Account: {escape_html(status["masked_account"])}'
            + render_reporting_metrics(metrics) + '<h3>Market Data (separate from account funds)</h3>'
            + render_reporting_metrics(quote_metrics)
            + f'<p>{escape_html(status.get("error") or "")}</p><p>{escape_html(market.get("error") or "")}</p>'
            + '<p>Balances cached for 60 seconds, including outside market hours. Unreported fields are N/A; cash available is not assumed to mean settled cash or options buying power.</p>')


@app.route("/api/account")
def api_connected_account():
    status = connected_account_snapshot()
    return jsonify({"account": status, "html": render_connected_account(status)})


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
        r = broker_request("GET",
            "/markets/quotes",
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
            quote = quote[0]
        if isinstance(quote, dict):
            quote["_fetched_at"] = market_now().isoformat()
            TOURNAMENT_HEALTH["tournament_last_successful_quote"] = quote["_fetched_at"]
            with BOT_LOCK:
                BOT_STATE["tournament_health"] = dict(TOURNAMENT_HEALTH)
        return quote
    except Exception as exc:
        record_api_diagnostic("quote", started_at, f"ERROR {exc}")
        return None


@broker_session
def get_position():
    started_at = time.perf_counter()
    try:
        r = broker_request("GET",
            "/accounts/{account}/positions",
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
        positions = pos if isinstance(pos, list) else [pos]
        version = load_config().get("broker_selection_version", 0)
        return [dict(position, _broker_selection_version=version) for position in positions]
    except Exception as exc:
        record_api_diagnostic("position", started_at, f"ERROR {exc}")
        return None


def get_expirations(symbol):
    if not scanner_market_is_open():
        return []
    try:
        r = broker_request("GET",
            "/markets/options/expirations",
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
    if not scanner_market_is_open():
        return []
    try:
        r = broker_request("GET",
            "/markets/options/chains",
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
    if not scanner_market_is_open():
        return None
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


def select_closest_contract_within_budget(symbol, side, maximum_position_cost_dollars, contracts):
    if not scanner_market_is_open():
        return None
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
        if ask is not None and option_position_cost(ask, contracts) <= maximum_position_cost_dollars:
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

    contracts = int(config.get("contracts", 1) or 1)
    maximum_position_cost_dollars = float(config.get("maximum_position_cost_dollars", 100) or 100)
    strict_ask = get_option_trade_price(strict_contract)
    if strict_contract and strict_ask is not None and option_position_cost(strict_ask, contracts) <= maximum_position_cost_dollars:
        return strict_contract

    return select_closest_contract_within_budget(config.get("symbol", "SPY"), side, maximum_position_cost_dollars, contracts)


def submit_option_order(option_symbol, qty, action="buy_to_open", tag=None):
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
    if tag:
        data["tag"] = tag

    started_at = time.perf_counter()
    r = broker_request("POST",
        "/accounts/{account}/orders",
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


def valid_price(value):
    try:
        price = float(value)
        return price if price > 0 else None
    except:
        return None


def get_option_confirmation_price(quote_or_contract):
    if not quote_or_contract:
        return None, "NONE"

    bid = valid_price(quote_or_contract.get("bid"))
    ask = valid_price(quote_or_contract.get("ask"))
    if bid is not None and ask is not None:
        return (bid + ask) / 2, "MIDPOINT"

    last = valid_price(quote_or_contract.get("last"))
    if last is not None:
        return last, "LAST"

    if ask is not None:
        return ask, "ASK"

    if bid is not None:
        return bid, "BID"

    return None, "NONE"


def broker_timestamp(value):
    if value in (None, ""):
        return None
    try:
        epoch = float(value)
        if epoch > 10_000_000_000:
            epoch /= 1000
        return datetime.fromtimestamp(epoch, MARKET_TZ)
    except:
        pass
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=MARKET_TZ)
        return parsed.astimezone(MARKET_TZ)
    except:
        return None


def quote_execution_snapshot(quote):
    quote = quote if isinstance(quote, dict) else {}
    bid = valid_price(quote.get("bid"))
    ask = valid_price(quote.get("ask"))
    last = valid_price(quote.get("last"))
    midpoint = (bid + ask) / 2 if bid is not None and ask is not None else None
    spread_dollars = ask - bid if bid is not None and ask is not None and ask >= bid else None
    spread_percent = (spread_dollars / midpoint) * 100 if spread_dollars is not None and midpoint else None
    price_source = "NONE"
    selected_price = None
    for key, label in [("last", "LAST"), ("bid", "BID"), ("ask", "ASK")]:
        try:
            selected_price = float(quote[key])
            price_source = label
            break
        except (KeyError, TypeError, ValueError):
            selected_price = None
    timestamp_key = {"LAST": "trade_date", "BID": "bid_date", "ASK": "ask_date"}.get(price_source)
    parsed_timestamp = broker_timestamp(quote.get(timestamp_key)) if timestamp_key else None
    age_seconds = (market_now() - parsed_timestamp).total_seconds() if parsed_timestamp else None
    return {
        "bid": bid,
        "ask": ask,
        "midpoint": midpoint,
        "last": last,
        "spread_dollars": spread_dollars,
        "spread_percent": spread_percent,
        "quote_timestamp": parsed_timestamp.isoformat() if parsed_timestamp else "",
        "quote_age_seconds": age_seconds if age_seconds is not None else "",
        "selected_price": selected_price,
        "price_source": price_source,
    }


def broker_order_quantity(value):
    price, status = strict_quote_price(value)
    return price if status == "OK" else 0.0 if status == "ZERO" else None


def parse_broker_order_execution(payload):
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except:
            payload = {}
    payload = payload if isinstance(payload, dict) else {}
    order = payload.get("order", payload)
    order = order if isinstance(order, dict) else {}
    status = order.get("status")
    filled_quantity = broker_order_quantity(order.get("exec_quantity"))
    fill_price = valid_price(order.get("avg_fill_price"))
    if fill_price is not None and not math.isfinite(fill_price):
        fill_price = None
    # A canceled/expired order can still carry a real partial fill.
    has_terminal_partial = status in EXIT_ORDER_FAILED_STATUSES and bool(filled_quantity)
    if status not in ["filled", "partially_filled"] and not has_terminal_partial:
        fill_price = None
    timestamp_allowed = status == "filled" or has_terminal_partial
    fill_timestamp = order.get("transaction_date") if fill_price is not None and timestamp_allowed else ""
    return {
        "order_status": status or "",
        "fill_price": fill_price,
        "fill_timestamp": fill_timestamp or "",
        "order_type": order.get("type") or "",
        "requested_price": order.get("price") if order.get("price") not in (None, 0, "0") else "",
        "order_id": str(order.get("id") or ""),
        "option_symbol": order.get("option_symbol") or "",
        "side": order.get("side") or "",
        "tag": order.get("tag") or "",
        "filled_quantity": filled_quantity,
        "remaining_quantity": broker_order_quantity(order.get("remaining_quantity")),
    }


def fetch_broker_order_execution(order_id, initial_response="", retries=4, delay_seconds=0.25):
    execution = parse_broker_order_execution(initial_response)
    if execution.get("fill_price") is not None or not order_id:
        return execution

    for attempt in range(max(1, retries)):
        try:
            response = broker_request("GET",
                "/accounts/{account}/orders/" + str(order_id),
                headers=headers(),
                timeout=3,
            )
            if response.status_code == 200:
                execution = parse_broker_order_execution(response.json())
                if execution.get("fill_price") is not None:
                    return execution
                if execution.get("order_status") in ["rejected", "canceled", "expired", "error"]:
                    return execution
        except Exception as exc:
            print("BROKER EXECUTION LOOKUP ERROR:", exc)
        if attempt < retries - 1:
            time.sleep(delay_seconds)
    return execution


def market_context_spy_snapshot(market_context):
    market_context = market_context or {}
    timestamp = market_context.get("spy_quote_timestamp") or ""
    parsed_timestamp = broker_timestamp(timestamp)
    age_seconds = max(0.0, (market_now() - parsed_timestamp).total_seconds()) if parsed_timestamp else ""
    return {
        "price": valid_price(market_context.get("price")),
        "quote_timestamp": parsed_timestamp.isoformat() if parsed_timestamp else "",
        "quote_age_seconds": age_seconds,
    }


def entry_execution_diagnostics(market_context, contract, order_execution=None):
    spy = market_context_spy_snapshot(market_context)
    option = quote_execution_snapshot(contract)
    order_execution = order_execution or {}
    fill_price = order_execution.get("fill_price")
    slippage = fill_price - option["midpoint"] if fill_price is not None and option["midpoint"] is not None else ""
    return {
        "EntrySpyPrice": spy["price"] if spy["price"] is not None else "",
        "EntrySpyQuoteTimestamp": spy["quote_timestamp"],
        "EntrySpyQuoteAgeSeconds": spy["quote_age_seconds"],
        "EntryOptionBid": option["bid"] if option["bid"] is not None else "",
        "EntryOptionAsk": option["ask"] if option["ask"] is not None else "",
        "EntryOptionMidpoint": option["midpoint"] if option["midpoint"] is not None else "",
        "EntryOptionLast": option["last"] if option["last"] is not None else "",
        "EntryOptionQuoteTimestamp": option["quote_timestamp"],
        "EntryOptionQuoteAgeSeconds": option["quote_age_seconds"],
        "EntrySpreadDollars": option["spread_dollars"] if option["spread_dollars"] is not None else "",
        "EntrySpreadPercent": option["spread_percent"] if option["spread_percent"] is not None else "",
        "EntryRequestedOrderType": "market",
        "EntryRequestedOrderPrice": "",
        "EntryBrokerOrderStatus": order_execution.get("order_status", ""),
        "EntryBrokerFillPrice": fill_price if fill_price is not None else "",
        "EntryBrokerFillTimestamp": order_execution.get("fill_timestamp", ""),
        "EntrySlippageFromMidpoint": slippage,
    }


def market_data_environment():
    try:
        environment = resolve_connection(load_config()).environment
        return "SANDBOX_DELAYED" if environment == "sandbox" else "PRODUCTION"
    except BrokerConnectionError:
        return "UNKNOWN"


def entry_quote_freshness(config, contract, context):
    environment = market_data_environment()
    if not config.get("require_realtime_entry_quotes", False):
        return True, "Freshness guard disabled"
    if environment == "SANDBOX_DELAYED" and config.get("allow_delayed_sandbox_entries", False):
        return True, "Delayed sandbox research override enabled"
    limit = max(1, min(3600, safe_float(config.get("max_entry_quote_age_seconds"), 30)))
    ages = {
        "option": quote_execution_snapshot(contract)["quote_age_seconds"],
        "SPY": market_context_spy_snapshot(context)["quote_age_seconds"],
    }
    for label, age in ages.items():
        if age in (None, "") or not math.isfinite(float(age)) or float(age) < 0:
            return False, f"ENTRY blocked: {label} quote timestamp unavailable/invalid ({environment})"
        if float(age) > limit:
            return False, f"ENTRY blocked: {label} quote age {float(age):.1f}s exceeds {limit:g}s ({environment})"
    return True, "Entry quotes fresh"


def exit_trigger_diagnostics(quote, spy_price, hard_stop_price, trailing_stop_price, effective_stop_price):
    """Record the quote that triggered an option exit. The trigger basis is always the bid."""
    option = quote_execution_snapshot(quote)
    bid_timestamp = broker_timestamp((quote or {}).get("bid_date")) if isinstance(quote, dict) else None
    bid_age = (market_now() - bid_timestamp).total_seconds() if bid_timestamp else ""
    return {
        "ExitTriggerOptionBid": option["bid"] if option["bid"] is not None else "",
        "ExitTriggerOptionAsk": option["ask"] if option["ask"] is not None else "",
        "ExitTriggerOptionMidpoint": option["midpoint"] if option["midpoint"] is not None else "",
        "ExitTriggerOptionLast": option["last"] if option["last"] is not None else "",
        "ExitTriggerQuoteTimestamp": bid_timestamp.isoformat() if bid_timestamp else "",
        "ExitTriggerQuoteAgeSeconds": bid_age,
        "ExitHardStopPrice": hard_stop_price if hard_stop_price is not None else "",
        "ExitTrailingStopPrice": trailing_stop_price if trailing_stop_price is not None else "",
        "ExitEffectiveStopPrice": effective_stop_price if effective_stop_price is not None else "",
        "ExitStopPriceSource": EXIT_PRICE_BASIS if option["bid"] is not None else "NONE",
        "ExitTriggerPrice": option["bid"] if option["bid"] is not None else "",
        "ExitTriggerSpyPrice": spy_price if spy_price is not None else "",
    }


def exit_execution_diagnostics(trigger_diagnostics, pre_exit_quote, spy_exit_price, entry_row, order_execution=None):
    diagnostics = dict(trigger_diagnostics or {})
    pre_exit = quote_execution_snapshot(pre_exit_quote)
    order_execution = order_execution or {}
    fill_price = order_execution.get("fill_price")
    trigger_price = valid_price(diagnostics.get("ExitTriggerPrice"))
    entry_spy_price = valid_price((entry_row or {}).get("EntrySpyPrice"))
    exit_spy_price = valid_price(spy_exit_price)
    spy_change = exit_spy_price - entry_spy_price if exit_spy_price is not None and entry_spy_price is not None else None
    spy_change_percent = (spy_change / entry_spy_price) * 100 if spy_change is not None and entry_spy_price else None
    diagnostics.update({
        "ExitOptionBid": pre_exit["bid"] if pre_exit["bid"] is not None else "",
        "ExitOptionAsk": pre_exit["ask"] if pre_exit["ask"] is not None else "",
        "ExitOptionMidpoint": pre_exit["midpoint"] if pre_exit["midpoint"] is not None else "",
        "ExitOptionLast": pre_exit["last"] if pre_exit["last"] is not None else "",
        "ExitOptionQuoteTimestamp": pre_exit["quote_timestamp"],
        "ExitOptionQuoteAgeSeconds": pre_exit["quote_age_seconds"],
        "ExitRequestedOrderType": "market",
        "ExitRequestedOrderPrice": "",
        "ExitBrokerOrderStatus": order_execution.get("order_status", ""),
        "ExitBrokerFillPrice": fill_price if fill_price is not None else "",
        "ExitBrokerFillTimestamp": order_execution.get("fill_timestamp", ""),
        "ExitSlippageFromTrigger": fill_price - trigger_price if fill_price is not None and trigger_price is not None else "",
        "ExitSpyPrice": exit_spy_price if exit_spy_price is not None else "",
        "SpyChangeDollars": spy_change if spy_change is not None else "",
        "SpyChangePercent": spy_change_percent if spy_change_percent is not None else "",
    })
    return diagnostics


QUOTE_DATA_ERROR_DECISION = "QUOTE DATA ERROR"
EXIT_CODE_HARD_STOP = "HARD_STOP"
EXIT_CODE_TRAILING_STOP = "TRAILING_STOP"
EXIT_CODE_PROFIT_FLOOR_STOP = "PROFIT_FLOOR_STOP"


def strict_quote_price(value):
    """Return (price, status). Status is OK, MISSING, INVALID, NON_FINITE, ZERO or NEGATIVE."""
    if value is None or value == "":
        return None, "MISSING"
    if isinstance(value, bool):
        return None, "INVALID"
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None, "INVALID"
    if not math.isfinite(price):
        return None, "NON_FINITE"
    if price == 0:
        return None, "ZERO"
    if price < 0:
        return None, "NEGATIVE"
    return price, "OK"


def exit_quote_age_limit(config):
    environment = market_data_environment()
    if environment == "SANDBOX_DELAYED":
        key, default = "max_sandbox_exit_quote_age_seconds", DEFAULT_SANDBOX_MAX_EXIT_QUOTE_AGE_SECONDS
    else:
        key, default = "max_exit_quote_age_seconds", DEFAULT_MAX_EXIT_QUOTE_AGE_SECONDS
    limit = safe_float((config or {}).get(key), default)
    if not isinstance(limit, (int, float)) or not math.isfinite(limit) or limit <= 0:
        limit = default
    return float(limit), environment


def get_option_exit_price(quote, config=None):
    """Validated exit price for closing a long option: the current bid, or an explicit error.

    Never falls back to last, ask or entry. get_quote_price stays as it is for underlying
    signals and entries; stop logic must use this helper instead.
    """
    config = config if config is not None else load_config()
    limit, environment = exit_quote_age_limit(config)
    result = {
        "ok": False, "status": "", "message": "", "price": None, "price_source": "NONE",
        "bid": None, "raw_bid": None, "ask": None, "last": None,
        "quote_timestamp": "", "quote_age_seconds": None,
        "max_age_seconds": limit, "market_data_environment": environment,
    }

    def failed(status, detail):
        result.update(status=status, message=f"{status}: {detail}")
        return result

    if not isinstance(quote, dict) or not quote:
        return failed("MISSING_QUOTE", "broker returned no option quote")

    bid, bid_status = strict_quote_price(quote.get("bid"))
    ask, _ = strict_quote_price(quote.get("ask"))
    last, _ = strict_quote_price(quote.get("last"))
    raw_timestamp = quote.get("bid_date")
    timestamp = broker_timestamp(raw_timestamp)
    age = (market_now() - timestamp).total_seconds() if timestamp else None
    result.update(bid=bid, raw_bid=quote.get("bid"), ask=ask, last=last,
                  quote_timestamp=timestamp.isoformat() if timestamp else "",
                  quote_age_seconds=age)

    if bid_status != "OK":
        return failed(f"{bid_status}_BID", f"bid is unusable (raw value {quote.get('bid')!r})")
    if ask is not None and bid > ask + EXIT_PRICE_TOLERANCE:
        return failed("CROSSED_QUOTE", f"bid {bid} is above ask {ask}")
    if timestamp is None:
        status = "MISSING_BID_TIMESTAMP" if raw_timestamp in (None, "") else "INVALID_BID_TIMESTAMP"
        return failed(status, f"bid age cannot be verified (raw bid_date {raw_timestamp!r})")
    if age < -MAX_EXIT_QUOTE_FUTURE_SKEW_SECONDS:
        return failed("FUTURE_BID_TIMESTAMP", f"bid timestamp is {-age:.1f}s in the future")
    if age > limit:
        return failed("STALE_BID", f"bid age {age:.1f}s exceeds {limit:g}s limit ({environment})")

    result.update(ok=True, status="OK", price=bid, price_source=EXIT_PRICE_BASIS,
                  quote_age_seconds=max(age, 0.0))
    return result


def exit_quote_number(value):
    return f"{value:.4f}" if isinstance(value, (int, float)) else "N/A"


def exit_quote_log_line(symbol, exit_quote, stop_price="", exit_reason=""):
    age = exit_quote.get("quote_age_seconds")
    return (
        f"EXIT QUOTE {symbol}: status={exit_quote.get('status')} source={exit_quote.get('price_source')} "
        f"timestamp={exit_quote.get('quote_timestamp') or 'N/A'} "
        f"age={f'{age:.1f}s' if isinstance(age, (int, float)) else 'N/A'} "
        f"bid={exit_quote_number(exit_quote.get('bid'))} ask={exit_quote_number(exit_quote.get('ask'))} "
        f"last={exit_quote_number(exit_quote.get('last'))} stop={exit_quote_number(stop_price)} "
        f"exit_reason={exit_reason or 'NONE'}"
    )


def exit_quote_status_text(exit_quote):
    age = exit_quote.get("quote_age_seconds")
    return f"BID {exit_quote_number(exit_quote.get('bid'))} ({f'{age:.1f}s' if isinstance(age, (int, float)) else 'age N/A'} old)"


def evaluate_option_exit(symbol, entry_price, quote, config):
    """The single stop decision used by every automated exit path."""
    reconcile_exit_state_entry(symbol, entry_price, config)
    exit_quote = get_option_exit_price(quote, config)
    bid = exit_quote["price"]
    hard_stop_percent = float(config["strategy"].get("hard_stop_percent", 20))
    hard_stop_price = entry_price * (1 - hard_stop_percent / 100) if entry_price else None
    stop_values = calculate_stop_state(
        symbol, entry_price, bid, config,
        update_state=exit_quote["ok"],
        price_basis=EXIT_PRICE_BASIS if exit_quote["ok"] else None,
    )
    effective_stop = stop_values.get("effective_trailing_stop")
    stop_armed = bool(stop_values.get("stop_armed"))
    decision = "HOLD"
    exit_code = ""
    stop_price = effective_stop if stop_armed and effective_stop is not None else hard_stop_price

    if not exit_quote["ok"]:
        decision = "DATA_ERROR"
    else:
        clear_exit_quote_error(symbol)
        if hard_stop_price is not None and bid <= hard_stop_price + EXIT_PRICE_TOLERANCE:
            decision, exit_code, stop_price = "SELL", EXIT_CODE_HARD_STOP, hard_stop_price
        elif stop_armed and effective_stop is not None and bid <= effective_stop + EXIT_PRICE_TOLERANCE:
            floor_rules = stop_values.get("stop_control_rule") == "PROFIT FLOOR"
            decision, exit_code, stop_price = "SELL", EXIT_CODE_PROFIT_FLOOR_STOP if floor_rules else EXIT_CODE_TRAILING_STOP, effective_stop

    return {
        "ok": exit_quote["ok"], "decision": decision, "exit_code": exit_code, "bid": bid,
        "exit_quote": exit_quote, "stop_values": stop_values, "hard_stop_price": hard_stop_price,
        "effective_stop": effective_stop, "stop_price": stop_price, "stop_armed": stop_armed,
    }


def clear_exit_quote_error(symbol):
    key = exit_state_key(symbol)
    with BOT_LOCK:
        previous = BOT_STATE["exit_quote_errors"].pop(key, None)
    if previous:
        add_bot_reason(f"EXIT QUOTE RECOVERED {symbol}: valid bid returned after {previous['count']} failed poll(s)")


def record_exit_quote_error(symbol, qty, entry_price, evaluation, label):
    """Surface a quote problem. Places, cancels and changes no orders; the next poll is the retry."""
    exit_quote = evaluation["exit_quote"]
    stop_values = evaluation["stop_values"]
    now_epoch = time.time()
    key = exit_state_key(symbol)
    with BOT_LOCK:
        entry = BOT_STATE["exit_quote_errors"].setdefault(key, {"count": 0, "first_seen": now_epoch})
        entry["count"] += 1
        entry["status"] = exit_quote["status"]
        count, first_seen = entry["count"], entry["first_seen"]
    message = f"{label} QUOTE DATA ERROR {symbol}: {exit_quote['message']}"
    exposure = (
        f"UNRESOLVED EXPOSURE: {qty:g} contract(s) of {symbol} remain open and stops are NOT being evaluated "
        f"({count} failed poll(s) over {now_epoch - first_seen:.0f}s). Entry {exit_quote_number(entry_price)}, "
        f"peak {exit_quote_number(stop_values.get('peak_price'))}, hard stop {exit_quote_number(evaluation['hard_stop_price'])}, "
        f"effective stop {exit_quote_number(evaluation['effective_stop'])}. No order sent or changed; retrying next poll. "
        f"Broker-side orders, if any, are untouched."
    )
    print(exit_quote_log_line(symbol, exit_quote, evaluation["stop_price"], "NONE"))
    print(exposure)
    add_bot_reason(f"{message} | {exposure}")
    set_last_error(f"{message} | {exposure}")
    return message, exposure


def json_safe(value):
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return {}


def exit_state_key(symbol, config=None):
    """Account/environment/contract key for stop state. Unverifiable accounts never share a key."""
    return exit_order_key(exit_broker_scope(config) or EXIT_UNVERIFIED_SCOPE, symbol)


def write_exit_recovery_marker():
    with BOT_LOCK:
        payload = json_safe(BOT_STATE.get("exit_recovery") or {})
    temporary = f"{EXIT_RECOVERY_FILE}.tmp"
    try:
        os.makedirs(os.path.dirname(EXIT_RECOVERY_FILE), exist_ok=True)
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, EXIT_RECOVERY_FILE)
        return True
    except (OSError, TypeError, ValueError) as exc:
        print("EXIT RECOVERY MARKER SAVE FAILED:", exc)
        return False


def require_exit_recovery(reason):
    """Record that saved exit state was lost. Lives in its own file so state saves cannot clear it."""
    message = (f"EXIT RECOVERY REQUIRED: {reason}; armed stops and exit intents NOT restored. "
               f"All new orders are blocked until broker orders and positions are reconciled.")
    with BOT_LOCK:
        BOT_STATE["exit_recovery"] = {
            "required": True, "reason": reason, "detected_epoch": time.time(),
            "detected_at": market_now().isoformat(), "reconciled_scopes": [], "checks": {}, "status": message,
        }
        BOT_STATE["exit_state_error"] = message
    saved = write_exit_recovery_marker()
    print(message)
    add_bot_reason(message)
    set_last_error(message)
    return saved


def load_exit_recovery_marker():
    if not os.path.exists(EXIT_RECOVERY_FILE):
        return
    try:
        with open(EXIT_RECOVERY_FILE, encoding="utf-8") as handle:
            marker = json.load(handle)
        if not isinstance(marker, dict) or not isinstance(marker.get("reconciled_scopes"), list):
            raise ValueError("invalid marker")
    except (OSError, ValueError) as exc:
        require_exit_recovery(f"recovery marker unreadable ({exc})")
        return
    with BOT_LOCK:
        BOT_STATE["exit_recovery"] = marker
    add_bot_reason(f"EXIT RECOVERY marker restored: {marker.get('reason', 'unknown reason')}")


def exit_recovery_block_reason(config=None):
    """Why new orders are blocked on the selected account; empty when they are allowed."""
    with BOT_LOCK:
        recovery = BOT_STATE.get("exit_recovery")
        recovery = dict(recovery) if recovery else None
    if not recovery:
        return ""
    if exit_broker_scope(config) in (recovery.get("reconciled_scopes") or []):
        return ""
    return recovery.get("status") or "EXIT RECOVERY REQUIRED: new orders are blocked"


def update_exit_recovery(scope, count, last_epoch, status):
    with BOT_LOCK:
        recovery = BOT_STATE.get("exit_recovery")
        if not recovery:
            return
        recovery.setdefault("checks", {})[scope] = {"count": count, "last_epoch": last_epoch}
        changed = recovery.get("status") != status
        recovery["status"] = status
    write_exit_recovery_marker()
    if changed:
        print(status)
        add_bot_reason(status)
    set_last_error(status)


@broker_session
def process_exit_recovery(positions):
    """Read-only reconciliation for lost exit state. Returns NONE, BLOCKED or RECONCILED."""
    config = load_config()
    with BOT_LOCK:
        recovery = BOT_STATE.get("exit_recovery")
        recovery = copy.deepcopy(recovery) if recovery else None
    if not recovery:
        return "NONE"
    scope = exit_broker_scope(config)
    if scope is None:
        set_last_error("EXIT RECOVERY REQUIRED: broker account could not be verified; new orders remain blocked")
        return "BLOCKED"
    if scope in recovery.get("reconciled_scopes", []):
        return "NONE"
    check = (recovery.get("checks") or {}).get(scope) or {"count": 0, "last_epoch": 0}
    now_epoch = time.time()
    if now_epoch - check["last_epoch"] < EXIT_RECONCILE_INTERVAL_SECONDS:
        return "BLOCKED"

    def blocked(detail, count=0):
        update_exit_recovery(scope, count, now_epoch, (
            f"EXIT RECOVERY REQUIRED ({recovery.get('reason')}): {detail} New orders remain blocked; monitoring and "
            f"reconciliation continue. ACTION: review open orders and positions for this account in Tradier."
        ))
        return "BLOCKED"

    with BOT_LOCK:
        position_status = str(BOT_STATE.get("last_position_status", ""))
    if not position_status.startswith("OK"):
        return blocked("Broker positions could not be confirmed.")
    orders, error = fetch_broker_orders_for_reconciliation()
    if error:
        return blocked(f"Broker order lookup failed ({error}).")
    working = [order for order in orders
               if order.get("side") in (None, "", "sell_to_close", "buy_to_open") and not order_is_finished(order.get("status"))]
    if working:
        ids = ", ".join(str(order.get("id") or "no id") for order in working[:5])
        return blocked(f"{len(working)} working option order(s) at the broker (ids: {ids}).")
    if now_epoch - recovery.get("detected_epoch", 0) < EXIT_ABSENCE_SETTLE_SECONDS:
        return blocked("Waiting for any recent order to become visible at the broker.")
    count = check["count"] + 1
    if count < EXIT_ABSENCE_CONFIRMATIONS:
        return blocked(f"{count} of {EXIT_ABSENCE_CONFIRMATIONS} required clean broker checks completed.", count)

    notes = rebuild_closed_exits_from_orders(orders, positions, scope)
    status = f"EXIT RECOVERY complete for this account: no working orders in {count} broker checks. Stops restart from entry unless restored."
    if notes:
        status += f" Filled exit order(s) missing from the trade log: {', '.join(notes)}; record them from Tradier."
    with BOT_LOCK:
        current = BOT_STATE.get("exit_recovery")
        if current:
            current.setdefault("reconciled_scopes", []).append(scope)
            current["status"] = status
    if not write_exit_recovery_marker():
        with BOT_LOCK:
            BOT_STATE["exit_recovery"]["reconciled_scopes"].remove(scope)
        return blocked("The reconciliation result could not be saved.")
    print(status)
    add_bot_reason(status)
    return "RECONCILED"


def rebuild_closed_exits_from_orders(orders, positions, scope):
    """Lost state may have held a closed record; tagged fills restore that protection."""
    unlogged = []
    held = {position.get("symbol") for position in positions or []}
    changed = False
    for order in orders:
        tag = str(order.get("tag") or "")
        if not tag.startswith(EXIT_INTENT_TAG_PREFIX) or str(order.get("status") or "").lower() != "filled":
            continue
        try:
            if not exit_fill_already_logged(tag[len(EXIT_INTENT_TAG_PREFIX):]):
                unlogged.append(str(order.get("id") or tag))
        except OSError:
            unlogged.append(str(order.get("id") or tag))
        symbol = order.get("option_symbol")
        if symbol in held:
            stamp = broker_timestamp(order.get("transaction_date") or order.get("create_date"))
            key = exit_order_key(scope, symbol)
            with BOT_LOCK:
                BOT_STATE["exit_closed_awaiting"].setdefault(key, {
                    "key": key, "scope": scope, "symbol": symbol, "intent_id": tag[len(EXIT_INTENT_TAG_PREFIX):],
                    "order_id": str(order.get("id") or ""), "sold_qty": broker_order_quantity(order.get("exec_quantity")) or 0,
                    "closed_epoch": stamp.timestamp() if stamp else time.time(), "position_before": {},
                    "entry_price": None, "flat_checks": 0, "status_message": "Rebuilt during recovery; awaiting position confirmation",
                })
            changed = True
    if changed:
        persist_exit_state()
    return unlogged


def persist_exit_state():
    """Atomically and durably save exit state. Returns True only if it reached disk."""
    with EXIT_STATE_SAVE_LOCK:
        with BOT_LOCK:
            recovery = BOT_STATE.get("exit_recovery")
        if recovery and not os.path.exists(EXIT_RECOVERY_FILE) and not write_exit_recovery_marker():
            # The unreadable state file is the only remaining evidence; do not overwrite it.
            print("EXIT STATE NOT SAVED: recovery marker could not be written")
            return False
        with BOT_LOCK:
            payload = {
                "version": EXIT_STATE_VERSION,
                "saved_at": market_now().isoformat(),
                "positions": {
                    key: {
                        "peak_price": peak,
                        "peak_basis": BOT_STATE["position_peak_basis"].get(key) or LEGACY_PEAK_BASIS,
                        "entry_price": BOT_STATE["position_exit_entry_prices"].get(key),
                        "profit_floor_active": bool(BOT_STATE["position_profit_floor_active"].get(key, False)),
                        "effective_stop": BOT_STATE["position_effective_stops"].get(key),
                    }
                    for key, peak in BOT_STATE["position_peaks"].items()
                },
                "unscoped_positions": BOT_STATE["unscoped_exit_positions"],
                "pending_exit_orders": BOT_STATE["pending_exit_orders"],
                "exit_confirmed_remaining": BOT_STATE["exit_confirmed_remaining"],
                "exit_closed_awaiting": BOT_STATE["exit_closed_awaiting"],
            }
            payload = json_safe(payload)
        temporary = f"{EXIT_STATE_FILE}.tmp"
        try:
            os.makedirs(os.path.dirname(EXIT_STATE_FILE), exist_ok=True)
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, EXIT_STATE_FILE)
            with BOT_LOCK:
                if not BOT_STATE.get("exit_recovery"):
                    BOT_STATE["exit_state_error"] = ""
            return True
        except (OSError, TypeError, ValueError) as exc:
            message = f"EXIT STATE SAVE FAILED: {exc}; armed stops and exit intents would not survive a restart"
            print(message)
            with BOT_LOCK:
                BOT_STATE["exit_state_error"] = message
            set_last_error(message)
            return False


def restore_position_record(key, record, issues):
    if not isinstance(record, dict):
        issues.append(f"position {key} is not a record")
        return
    peak, peak_status = strict_quote_price(record.get("peak_price"))
    if peak_status != "OK":
        issues.append(f"invalid saved peak for {key}: {record.get('peak_price')!r}")
        return
    basis = record.get("peak_basis") if record.get("peak_basis") == EXIT_PRICE_BASIS else LEGACY_PEAK_BASIS
    entry, _ = strict_quote_price(record.get("entry_price"))
    stop, _ = strict_quote_price(record.get("effective_stop"))
    with BOT_LOCK:
        BOT_STATE["position_peaks"][key] = peak
        BOT_STATE["position_peak_basis"][key] = basis
        BOT_STATE["position_profit_floor_active"][key] = bool(record.get("profit_floor_active"))
        if entry is not None:
            BOT_STATE["position_exit_entry_prices"][key] = entry
        if stop is not None:
            BOT_STATE["position_effective_stops"][key] = stop
    add_bot_reason(f"EXIT STATE RESTORED {key}: peak {peak:.4f} basis {basis} effective stop {exit_quote_number(stop)}")


def quarantine_unscoped_position(symbol, record, issues):
    """Pre-scoping stop state belongs to an unknown account, so no account receives it automatically."""
    peak, peak_status = strict_quote_price(record.get("peak_price") if isinstance(record, dict) else None)
    if peak_status != "OK":
        issues.append(f"invalid saved peak for {symbol}")
        return
    stored = dict(record, symbol=symbol, peak_price=peak, migration_id=record.get("migration_id") or uuid.uuid4().hex)
    with BOT_LOCK:
        BOT_STATE["unscoped_exit_positions"][symbol] = stored
    add_bot_reason(f"EXIT STATE MIGRATION REQUIRED {symbol}: saved peak {peak:.4f} has no account; choose an action in Unresolved Exit Orders")


def valid_exit_quantity(value):
    return strict_quote_price(value)[1] == "OK"


def load_exit_state():
    """Restore armed stops and exit intents. Unloadable or invalid state requires broker recovery."""
    load_exit_recovery_marker()
    if not os.path.exists(EXIT_STATE_FILE):
        print("EXIT STATE: no saved state; any open position starts its peak from entry")
        return False
    try:
        with open(EXIT_STATE_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("root is not an object")
        if not isinstance(data.get("version", 1), int) or data.get("version", 1) > EXIT_STATE_VERSION:
            raise ValueError(f"unsupported version {data.get('version')!r}")
    except (OSError, ValueError) as exc:
        # Marker first: if it cannot be written the unreadable file must stay as the only evidence.
        if require_exit_recovery(f"saved exit state unreadable ({exc})"):
            try:
                os.replace(EXIT_STATE_FILE, f"{EXIT_STATE_FILE}.corrupt-{int(time.time())}")
            except OSError as move_error:
                print("EXIT STATE: could not preserve unreadable file:", move_error)
        return False

    issues = []
    for key, record in (data.get("positions") or {}).items():
        if "|" in key:
            restore_position_record(key, record, issues)
        else:
            quarantine_unscoped_position(key, record, issues)
    for symbol, record in (data.get("unscoped_positions") or {}).items():
        quarantine_unscoped_position(symbol, record, issues)

    for saved_key, record in (data.get("pending_exit_orders") or {}).items():
        if not isinstance(record, dict) or not valid_exit_quantity(record.get("qty")) or not isinstance(record.get("submitted_epoch"), (int, float)):
            issues.append(f"invalid exit intent {saved_key}")
            continue
        record.setdefault("symbol", saved_key)
        if not record.get("scope") or not record.get("intent_id"):
            # Saved by the pre-intent format: its account is unknown, so it stays blocked for review.
            record.update(scope=EXIT_UNSCOPED, tag=record.get("tag", ""), intent_id=record.get("intent_id") or uuid.uuid4().hex)
        record["key"] = exit_order_key(record["scope"], record["symbol"])
        with BOT_LOCK:
            BOT_STATE["pending_exit_orders"][record["key"]] = record
        add_bot_reason(f"EXIT STATE RESTORED intent {record['symbol']}: order {record.get('order_id') or 'UNKNOWN'} tag {record.get('tag') or 'NONE'}; reconciling before any new exit")

    for key, value in (data.get("exit_confirmed_remaining") or {}).items():
        if valid_exit_quantity(value):
            with BOT_LOCK:
                BOT_STATE["exit_confirmed_remaining"][key] = value
        else:
            issues.append(f"invalid confirmed remaining quantity for {key}")

    for key, record in (data.get("exit_closed_awaiting") or {}).items():
        if not isinstance(record, dict) or not record.get("symbol") or not isinstance(record.get("closed_epoch"), (int, float)):
            issues.append(f"invalid closed exit record {key}")
            continue
        with BOT_LOCK:
            BOT_STATE["exit_closed_awaiting"][key] = record
        add_bot_reason(f"EXIT STATE RESTORED closed exit {record['symbol']}: awaiting broker position confirmation")

    if issues:
        summary = "; ".join(issues[:5])
        print("EXIT STATE VALIDATION FAILED:", summary)
        add_bot_reason(f"EXIT STATE VALIDATION FAILED: {summary}")
        try:
            shutil.copy2(EXIT_STATE_FILE, f"{EXIT_STATE_FILE}.corrupt-{int(time.time())}")
        except OSError as copy_error:
            print("EXIT STATE: could not preserve invalid file:", copy_error)
        require_exit_recovery(f"saved exit state failed validation ({summary})")
    return True


def clear_position_exit_state(symbol, reason):
    key = exit_state_key(symbol)
    with BOT_LOCK:
        had_state = key in BOT_STATE["position_peaks"] or key in BOT_STATE["exit_confirmed_remaining"]
        BOT_STATE["exit_confirmed_remaining"].pop(key, None)
        for name in ("position_peaks", "position_peak_basis", "position_exit_entry_prices",
                     "position_profit_floor_active", "position_effective_stops", "position_max_profit",
                     "position_max_drawdown", "exit_quote_errors", "exit_retry_after", "exit_state_absent_counts"):
            BOT_STATE[name].pop(key, None)
    if had_state:
        print("EXIT STATE CLEARED:", key, reason)
        persist_exit_state()


def reconcile_exit_state_entry(symbol, entry_price, config=None):
    """A changed cost basis means different fills, so the saved peak describes another position."""
    key = exit_state_key(symbol, config)
    with BOT_LOCK:
        stored = BOT_STATE["position_exit_entry_prices"].get(key)
        peak = BOT_STATE["position_peaks"].get(key)
    if stored is None or abs(stored - entry_price) <= EXIT_ENTRY_MATCH_TOLERANCE:
        return
    message = (f"EXIT STATE RESET {symbol}: broker entry {entry_price:.4f} differs from tracked entry "
               f"{stored:.4f}; discarded peak {exit_quote_number(peak)} from the previous position")
    print(message)
    add_bot_reason(message)
    clear_position_exit_state(symbol, "entry price changed")


def reconcile_exit_state_with_broker(positions):
    """Drop state for positions the broker repeatedly confirms are gone from this account."""
    scope = exit_broker_scope()
    if scope is None:
        return
    prefix = exit_order_key(scope, "")
    with BOT_LOCK:
        confirmed = str(BOT_STATE.get("last_position_status", "")).startswith("OK")
        tracked = [key for key in BOT_STATE["position_peaks"] if key.startswith(prefix)]
        pending = {record.get("symbol") for record in BOT_STATE["pending_exit_orders"].values() if record.get("scope") == scope}
    if not confirmed:
        return
    held = {position.get("symbol") for position in positions or []}
    to_clear = []
    with BOT_LOCK:
        counts = BOT_STATE["exit_state_absent_counts"]
        for key in tracked:
            symbol = key[len(prefix):]
            if symbol in held or symbol in pending:
                counts.pop(key, None)
                continue
            counts[key] = counts.get(key, 0) + 1
            if counts[key] >= EXIT_STATE_ABSENT_CONFIRMATIONS:
                to_clear.append(symbol)
    for symbol in to_clear:
        clear_position_exit_state(symbol, "broker confirms position closed")


def closed_exit_records():
    scope = exit_broker_scope()
    with BOT_LOCK:
        records = [dict(record) for record in BOT_STATE["exit_closed_awaiting"].values()]
    return [record for record in records if scope is None or record.get("scope") == scope]


def get_closed_exit(symbol):
    return next((record for record in closed_exit_records() if record.get("symbol") == symbol), None)


def update_closed_exit(record, persist=True, **changes):
    with BOT_LOCK:
        stored = BOT_STATE["exit_closed_awaiting"].get(record["key"])
        if not stored or all(stored.get(key) == value for key, value in changes.items()):
            return
        stored.update(changes)
    if persist:
        persist_exit_state()


def release_closed_exit(symbol, reason):
    record = get_closed_exit(symbol)
    if not record:
        return
    with BOT_LOCK:
        BOT_STATE["exit_closed_awaiting"].pop(record["key"], None)
    persist_exit_state()
    add_bot_reason(f"CLOSED EXIT RELEASED {symbol}: {reason}")


def position_is_new_after_close(record, position):
    """Evidence that this position opened after the confirmed exit; empty if it may be stale data."""
    acquired = broker_timestamp(position.get("date_acquired"))
    if acquired and acquired.timestamp() > record["closed_epoch"]:
        return f"position acquired {acquired.isoformat()}, after the exit fill"
    before = record.get("position_before") or {}
    quantity = float(position.get("quantity", 0) or 0)
    if before.get("cost_per_contract") is not None and quantity:
        cost = option_entry_price(float(position.get("cost_basis", 0) or 0), quantity)
        if abs(cost - before["cost_per_contract"]) > EXIT_ENTRY_MATCH_TOLERANCE:
            return f"cost per contract changed from {before['cost_per_contract']:.4f} to {cost:.4f}"

    now_epoch = time.time()
    if now_epoch - record.get("last_order_check_epoch", 0) < EXIT_RECONCILE_INTERVAL_SECONDS:
        return ""
    update_closed_exit(record, persist=False, last_order_check_epoch=now_epoch)
    orders, error = fetch_broker_orders_for_reconciliation()
    if error:
        return ""
    for order in orders:
        created = broker_timestamp(order.get("create_date"))
        executed = str(order.get("status") or "").lower() == "filled" or bool(broker_order_quantity(order.get("exec_quantity")))
        if (order.get("option_symbol") == record["symbol"] and order.get("side") == "buy_to_open" and executed
                and created and created.timestamp() > record["closed_epoch"]):
            return f"broker shows buy order {order.get('id')} after the exit fill"
    return ""


def advance_closed_exit(record, positions):
    """Hold a fully sold contract until broker positions catch up. Returns NONE when released."""
    symbol = record["symbol"]
    with BOT_LOCK:
        status = str(BOT_STATE.get("last_position_status", ""))
    held = [position for position in positions or []
            if position.get("symbol") == symbol and float(position.get("quantity", 0) or 0) > 0]
    awaiting = (f"EXIT CLOSED {symbol}: order {record.get('order_id') or 'UNKNOWN'} confirmed filled; "
                f"no further sell until broker positions confirm it.")
    if not held:
        if status.startswith("OK_NO") or (positions is not None and status.startswith("OK")):
            checks = record.get("flat_checks", 0) + 1
            if checks >= EXIT_CLOSED_FLAT_CONFIRMATIONS:
                release_closed_exit(symbol, "broker confirms the position is flat")
                return "NONE"
            update_closed_exit(record, flat_checks=checks, status_message=f"{awaiting} Flat check {checks} of {EXIT_CLOSED_FLAT_CONFIRMATIONS}.")
        else:
            update_closed_exit(record, flat_checks=0, status_message=f"{awaiting} Broker positions could not be confirmed.")
        return "CLOSED_AWAITING"

    update_closed_exit(record, flat_checks=0, status_message=f"{awaiting} Broker still reports the old position.")
    new_position = position_is_new_after_close(record, held[0])
    if new_position:
        release_closed_exit(symbol, f"genuinely new position: {new_position}")
        return "NONE"
    return "CLOSED_AWAITING"


def resolve_unscoped_exit_state(migration_id, action, confirmation):
    """Explicit operator choice for stop state saved before account scoping."""
    def finish(ok, message):
        add_bot_reason(f"EXIT STATE MIGRATION: {message}")
        with BOT_LOCK:
            BOT_STATE["exit_intent_clear_result"] = message
        return ok, message

    config = load_config()
    scope = exit_broker_scope(config)
    if scope is None:
        return finish(False, "NOT MIGRATED: the selected broker account could not be verified.")
    with BOT_LOCK:
        record = next((dict(item) for item in BOT_STATE["unscoped_exit_positions"].values() if migration_id and item.get("migration_id") == migration_id), None)
    if record is None:
        return finish(False, "NOT MIGRATED: that saved stop state no longer exists.")
    symbol = record["symbol"]
    if str(confirmation or "").strip() != symbol:
        return finish(False, f"NOT MIGRATED: type the exact contract symbol {symbol} to confirm.")
    if action not in ("apply", "discard"):
        return finish(False, "NOT MIGRATED: choose apply or discard.")
    key = exit_order_key(scope, symbol)
    with BOT_LOCK:
        if action == "apply" and key in BOT_STATE["position_peaks"]:
            exists = True
        else:
            exists = False
            BOT_STATE["unscoped_exit_positions"].pop(symbol, None)
    if exists:
        return finish(False, f"NOT MIGRATED {symbol}: this account already has its own stop state.")
    if action == "apply":
        restore_position_record(key, dict(record, peak_basis=LEGACY_PEAK_BASIS), [])
    if not persist_exit_state():
        with BOT_LOCK:
            BOT_STATE["unscoped_exit_positions"][symbol] = record
        return finish(False, f"NOT MIGRATED {symbol}: the change could not be saved.")
    return finish(True, f"{symbol}: saved stop state {'applied to the selected account' if action == 'apply' else 'discarded'}.")


def exit_broker_scope(config=None):
    """Identify the actual broker environment and account, without storing the account number."""
    try:
        connection = resolve_connection(config if config is not None else load_config())
    except BrokerConnectionError:
        return None
    return f"{connection.environment}:{hashlib.sha256(connection.account.encode()).hexdigest()[:16]}"


def exit_order_key(scope, symbol):
    return f"{scope}|{symbol}"


def pending_exit_records():
    """Intents for the selected account. If the account cannot be verified, all of them count."""
    scope = exit_broker_scope()
    with BOT_LOCK:
        records = [dict(record) for record in BOT_STATE["pending_exit_orders"].values()]
    return [record for record in records if scope is None or record.get("scope") in (scope, EXIT_UNSCOPED)]


def has_pending_exit_orders():
    return bool(pending_exit_records())


def has_unsettled_exits():
    return bool(pending_exit_records() or closed_exit_records())


@broker_session
def process_all_pending_exits(positions):
    for record in pending_exit_records() + closed_exit_records():
        process_pending_exit(record["symbol"], positions)


def get_pending_exit(symbol):
    """The unresolved exit intent for this symbol on the selected broker account, if any."""
    return next((record for record in pending_exit_records() if record.get("symbol") == symbol), None)


def remove_pending_exit(record):
    with BOT_LOCK:
        BOT_STATE["pending_exit_orders"].pop(record["key"], None)
    return persist_exit_state()


def update_pending_exit(record, **changes):
    with BOT_LOCK:
        stored = BOT_STATE["pending_exit_orders"].get(record["key"])
        if not stored or all(stored.get(key) == value for key, value in changes.items()):
            return
        stored.update(changes)
    persist_exit_state()


def exit_retry_wait_seconds(symbol):
    key = exit_state_key(symbol)
    with BOT_LOCK:
        retry_after = BOT_STATE["exit_retry_after"].get(key, 0)
    return max(0.0, retry_after - time.time())


def block_exit_retry(symbol, config):
    delay = safe_float((config or {}).get("exit_order_retry_seconds"), DEFAULT_EXIT_ORDER_RETRY_SECONDS)
    if not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay <= 0:
        delay = DEFAULT_EXIT_ORDER_RETRY_SECONDS
    key = exit_state_key(symbol, config)
    with BOT_LOCK:
        BOT_STATE["exit_retry_after"][key] = time.time() + delay


def audit_exit_event(action, record, config, **extra):
    """Bot audit row for an exit event; manual sells keep their existing no-audit behavior."""
    if record.get("source") != "BOT":
        return
    fields = dict(record.get("audit_fields") or {})
    fields.update(extra)
    fields["option_symbol"] = record["symbol"]
    fields.setdefault("reason_log", record.get("reasons") or [])
    fields.setdefault("exit_reason", record.get("reason", ""))
    fields.setdefault("order_id", record.get("order_id", ""))
    log_bot_audit(action, "SELL", config.get("symbol", ""), record.get("market_context") or {}, config, **fields)


def mark_exit_unresolved(record, code, detail, absent_checks=0):
    """Broker evidence is missing or ambiguous: keep protection and tell the operator what to check."""
    paused = "AUTOMATED RESUBMISSION PAUSED after operator confirmation. " if record.get("operator_hold") else ""
    message = (
        f"{paused}EXIT UNRESOLVED [{code}] {record['symbol']} qty {record['qty']:g}, tag {record.get('tag') or 'NONE'}: {detail} "
        f"Duplicate exits stay blocked and reconciliation continues. ACTION: in Tradier, check this account's "
        f"orders for a sell_to_close on {record['symbol']} and confirm whether it is working, filled or canceled."
    )
    if record.get("unresolved_code") != code:
        print(message)
        add_bot_reason(message)
    update_pending_exit(record, unresolved_code=code, status_message=message, absent_checks=absent_checks)
    set_last_error(message)
    return "PENDING"


@broker_session
def submit_tracked_exit(symbol, qty, cost_basis, entry_price, label, source, quote, trigger_diagnostics,
                        market_context, reason, reasons, config, audit_fields=None, reference_price=None, position_snapshot=None):
    """Save an exit intent, then send one tagged sell_to_close. Acceptance is not an exit."""
    scope = exit_broker_scope(config)
    if scope is None:
        message = f"{label} NOT SENT {symbol}: broker account/environment could not be verified; position remains open"
        print(message)
        add_bot_reason(message)
        set_last_error(message)
        return "SCOPE_UNAVAILABLE"
    recovery_block = exit_recovery_block_reason(config)
    if recovery_block:
        print(f"{label} NOT SENT {symbol}: {recovery_block}")
        set_last_error(recovery_block)
        return "RECOVERY_REQUIRED"
    if get_pending_exit(symbol):
        print(f"{label} NOT SENT: exit intent already unresolved for", symbol)
        return "PENDING"
    closed = get_closed_exit(symbol)
    if closed and advance_closed_exit(closed, [position_snapshot] if position_snapshot else None) != "NONE":
        print(f"{label} NOT SENT {symbol}: full exit already confirmed; awaiting position confirmation")
        return "AWAITING_POSITION_CONFIRMATION"

    key = exit_order_key(scope, symbol)
    with BOT_LOCK:
        confirmed_remaining = BOT_STATE["exit_confirmed_remaining"].get(key)
    send_qty = min(qty, confirmed_remaining) if confirmed_remaining else qty
    send_qty = int(send_qty) if float(send_qty).is_integer() else send_qty
    intent_id = uuid.uuid4().hex
    now_epoch = time.time()
    record = {
        "key": key,
        "scope": scope,
        "intent_id": intent_id,
        # Tradier documents tag as free text; letters, digits and hyphens are the safe subset.
        "tag": f"{EXIT_INTENT_TAG_PREFIX}{intent_id}",
        "phase": "INTENT_SAVED",
        "order_id": "",
        "order_status": "",
        "symbol": symbol,
        "qty": send_qty,
        "position_qty_at_intent": qty,
        "position_before": {
            "quantity": (position_snapshot or {}).get("quantity"),
            "cost_per_contract": cost_basis / (qty * 100) if qty else None,
            "date_acquired": (position_snapshot or {}).get("date_acquired"),
        },
        "filled_qty": 0,
        "remaining_qty": send_qty,
        "fill_logged": False,
        "absent_checks": 0,
        "last_reconcile_epoch": 0,
        "unresolved_code": "",
        "entry_price": entry_price,
        "cost_basis_per_contract": cost_basis / qty if qty else 0,
        "label": label,
        "source": source,
        "reason": reason,
        "reasons": list(reasons or []),
        "reference_price": reference_price,
        "trigger_diagnostics": json_safe(trigger_diagnostics or {}),
        "pre_exit_quote": json_safe(quote or {}),
        "pre_exit_quote_age_seconds": quote_execution_snapshot(quote)["quote_age_seconds"],
        "market_context": json_safe(market_context or {}),
        "audit_fields": json_safe(audit_fields or {}),
        "submitted_epoch": now_epoch,
        "submitted_at": market_now().isoformat(),
        "intent_market_date": market_now().date().isoformat(),
        "status_message": "EXIT INTENT saved; sending order",
    }

    with BOT_LOCK:
        BOT_STATE["pending_exit_orders"][key] = record
    if not persist_exit_state():
        with BOT_LOCK:
            BOT_STATE["pending_exit_orders"].pop(key, None)
        block_exit_retry(symbol, config)
        message = f"{label} NOT SENT {symbol}: exit intent could not be saved durably; ZERO orders sent; position remains open and stops stay armed"
        print(message)
        add_bot_reason(message)
        set_last_error(message)
        audit_exit_event("SKIP", record, config, skip_reason="exit intent could not be saved; order not sent", order_status="NOT_SENT")
        return "PERSIST_FAILED"

    try:
        ok, order_status, status, text = submit_and_parse_option_order(symbol, send_qty, "sell_to_close", label, tag=record["tag"])
        order_id = extract_order_id(text)
    except Exception as exc:
        ok, order_status, status, text, order_id = None, "SUBMIT_UNKNOWN", None, str(exc), ""

    # A timeout, 5xx or unreadable 200 may still have reached the broker, so only reconciliation can settle it.
    ambiguous = ok is None or (not ok and (status is None or status >= 500 or (status == 200 and order_status != "rejected")))
    if not ok and not ambiguous:
        remove_pending_exit(record)
        block_exit_retry(symbol, config)
        add_bot_reason(f"{label} REJECTED {symbol} ({order_status}): position remains open; stops stay armed; retry after backoff")
        audit_exit_event("SKIP", record, config, skip_reason=f"{label.lower()} rejected or not accepted", order_status=order_status, order_id=order_id)
        return "REJECTED"

    changes = {"order_id": str(order_id or ""), "order_status": order_status or "SUBMIT_UNKNOWN"}
    if order_id:
        changes.update(phase="ACCEPTED", status_message=f"EXIT ORDER {order_id} {order_status}: awaiting confirmed fill")
    else:
        changes.update(phase="SEND_UNCONFIRMED",
                       status_message=f"EXIT SEND UNCONFIRMED ({order_status or status}): duplicate exits blocked; reconciling tag {record['tag']} with broker orders")
    update_pending_exit(record, **changes)
    record.update(changes)
    add_bot_reason(f"{label} {record['status_message']} ({symbol}): {reason}")
    audit_exit_event("EXIT_PENDING", record, config, skip_reason="exit order submitted; not a completed exit", order_status=record["order_status"])
    if not order_id:
        return "PENDING"
    return process_pending_exit(symbol, initial_response=text, retries=4)


@broker_session
def process_pending_exit(symbol, positions=None, initial_response="", retries=1):
    """Advance an intent from broker evidence only. Returns NONE, PENDING, FILLED or FAILED."""
    record = get_pending_exit(symbol)
    if record is None:
        closed = get_closed_exit(symbol)
        return "NONE" if closed is None else advance_closed_exit(closed, positions)
    config = load_config()
    if exit_broker_scope(config) is None:
        return mark_exit_unresolved(record, "ACCOUNT_UNVERIFIED", "The account this exit belongs to cannot be verified.")
    if record.get("scope") == EXIT_UNSCOPED:
        if record.get("operator_hold"):
            return advance_operator_hold(record, config)
        return mark_exit_unresolved(record, "ACCOUNT_UNVERIFIED", "The account this exit belongs to cannot be verified.")
    if not record.get("order_id"):
        return reconcile_unidentified_exit(record, positions, config)
    execution = fetch_broker_order_execution(record["order_id"], initial_response, retries=retries)
    if record.get("operator_hold") and not execution.get("order_status"):
        return advance_operator_hold(record, config)
    return advance_identified_exit(record, execution, config)


def advance_identified_exit(record, execution, config):
    symbol = record["symbol"]
    requested = record["qty"]
    status = str(execution.get("order_status") or "").lower()
    filled = execution.get("filled_quantity")

    if execution.get("option_symbol") not in (None, "", symbol) or execution.get("side") not in (None, "", "sell_to_close"):
        return mark_exit_unresolved(record, "ORDER_MISMATCH", f"Order {record['order_id']} is not a sell_to_close for this contract.")

    if status == "filled":
        if execution.get("fill_price") is None and time.time() - record["submitted_epoch"] < EXIT_FILL_PRICE_WAIT_SECONDS:
            update_pending_exit(record, order_status=status, status_message=f"EXIT ORDER {record['order_id']} filled; waiting for fill price from broker")
            return "PENDING"
        return complete_exit_order(record, execution, filled if filled and filled > 0 else requested, config)

    if status in EXIT_ORDER_FAILED_STATUSES:
        if filled is None and status != "rejected":
            return mark_exit_unresolved(record, "FILL_QUANTITY_MISSING", f"Order {record['order_id']} is {status} but the broker gave no executed quantity.")
        return complete_exit_order(record, execution, filled or 0, config)

    # Working, partially filled, unknown status or failed lookup: nothing here proves the order is finished.
    filled_so_far = filled if filled is not None else record.get("filled_qty", 0)
    remaining = execution.get("remaining_quantity")
    remaining = remaining if remaining is not None else requested - filled_so_far
    state = status or "status unavailable from broker"
    update_pending_exit(
        record,
        order_status=status or record.get("order_status"),
        filled_qty=filled_so_far,
        remaining_qty=remaining,
        unresolved_code="",
        status_message=f"EXIT ORDER {record['order_id']} {state}: filled {filled_so_far:g}/{requested:g}, {remaining:g} still open; awaiting confirmed fill",
    )
    print(f"{record['label']} PENDING {symbol}: order {record['order_id']} {state}; filled {filled_so_far:g}/{requested:g}; no duplicate sent")
    return "PENDING"


def complete_exit_order(record, execution, filled, config):
    """The order is confirmed terminal. Log its fill exactly once, then release protection."""
    symbol = record["symbol"]
    requested = record["qty"]
    status = str(execution.get("order_status") or "").lower()
    if filled > 0 and not record.get("fill_logged"):
        try:
            already_logged = exit_fill_already_logged(record["intent_id"])
        except OSError as exc:
            return mark_exit_unresolved(record, "TRADE_LOG_UNREADABLE", f"Could not check for an existing fill record ({exc}).")
        if not already_logged:
            finalize_exit_fill(record, execution, filled, config)
        update_pending_exit(record, fill_logged=True)

    remaining = max(0, requested - filled)
    fill_stamp = broker_timestamp(execution.get("fill_timestamp"))
    now_epoch = time.time()
    with BOT_LOCK:
        if filled > 0 and remaining > 0:
            BOT_STATE["exit_confirmed_remaining"][record["key"]] = remaining
        elif remaining <= 0:
            BOT_STATE["exit_confirmed_remaining"].pop(record["key"], None)
            BOT_STATE["exit_closed_awaiting"][record["key"]] = {
                "key": record["key"], "scope": record["scope"], "symbol": symbol,
                "intent_id": record.get("intent_id", ""), "order_id": record.get("order_id", ""),
                "sold_qty": filled, "closed_epoch": min(fill_stamp.timestamp(), now_epoch) if fill_stamp else now_epoch,
                "position_before": record.get("position_before") or {}, "flat_checks": 0,
                "status_message": f"EXIT CLOSED {symbol}: confirmed filled; no further sell until broker positions confirm it.",
            }
        BOT_STATE["pending_exit_orders"].pop(record["key"], None)
    persist_exit_state()

    if remaining <= 0:
        clear_position_exit_state(symbol, "exit fill confirmed")
        return "FILLED"
    block_exit_retry(symbol, config)
    add_bot_reason(f"{record['label']} ORDER {record['order_id']} {status} for {symbol}: filled {filled:g}/{requested:g}; "
                   f"{remaining:g} contract(s) confirmed remaining; stops stay armed")
    audit_exit_event("SKIP", record, config, skip_reason=f"exit order {status}; {remaining:g} contract(s) remain open", order_status=status)
    return "FAILED"


def fetch_broker_orders_for_reconciliation():
    """Every order Tradier reports for today's session. Returns (orders, error)."""
    orders = []
    for page in range(1, EXIT_ORDER_LOOKUP_MAX_PAGES + 1):
        try:
            response = broker_request(
                "GET", "/accounts/{account}/orders", headers=headers(), timeout=5,
                params={"includeTags": "true", "limit": EXIT_ORDER_LOOKUP_LIMIT, "page": page},
            )
            if response.status_code != 200:
                return None, f"HTTP {response.status_code}"
            container = response.json()["orders"]
        except Exception as exc:
            return None, type(exc).__name__
        if container in (None, "", "null"):
            return orders, None
        if not isinstance(container, dict) or "order" not in container:
            return None, "unexpected response shape"
        batch = container["order"]
        batch = batch if isinstance(batch, list) else [batch]  # one order arrives as a bare object
        if not all(isinstance(order, dict) for order in batch):
            return None, "unexpected response shape"
        orders.extend(batch)
        if len(batch) < EXIT_ORDER_LOOKUP_LIMIT:
            return orders, None
    return None, "order list too long to read completely"


def order_could_be_this_exit(order, record):
    """Could an order not carrying this intent's tag still be the sell we sent?"""
    symbol = record["symbol"]
    option_symbol = order.get("option_symbol")
    underlying = str(order.get("symbol") or "")
    if option_symbol:
        same_contract = option_symbol == symbol
    else:
        same_contract = underlying == symbol or (bool(underlying) and symbol.startswith(underlying))
    if not same_contract or order.get("side") not in (None, "", "sell_to_close"):
        return False
    status = str(order.get("status") or "").lower()
    executed = broker_order_quantity(order.get("exec_quantity"))
    if status in EXIT_ORDER_FAILED_STATUSES and executed == 0:
        return False  # never executed and can no longer execute
    tag = str(order.get("tag") or "")
    if tag.startswith(EXIT_INTENT_TAG_PREFIX) and (status == "filled" or status in EXIT_ORDER_FAILED_STATUSES):
        return False  # one of our earlier intents, finished and already accounted for
    created = broker_timestamp(order.get("create_date"))
    return created is None or created.timestamp() >= record["submitted_epoch"] - EXIT_ORDER_CLOCK_SKEW_SECONDS


def broker_position_quantity(symbol, positions):
    with BOT_LOCK:
        confirmed = str(BOT_STATE.get("last_position_status", "")).startswith("OK")
    if not confirmed:
        return None
    return sum(float(position.get("quantity", 0) or 0) for position in positions or [] if position.get("symbol") == symbol)


def reconcile_unidentified_exit(record, positions, config):
    """No order id is known. Time and position changes never settle this; broker orders do."""
    symbol = record["symbol"]
    now_epoch = time.time()
    if now_epoch - record.get("last_reconcile_epoch", 0) < EXIT_RECONCILE_INTERVAL_SECONDS:
        return "PENDING"
    update_pending_exit(record, last_reconcile_epoch=now_epoch)

    orders, error = fetch_broker_orders_for_reconciliation()
    if error:
        return mark_exit_unresolved(record, "ORDER_LOOKUP_FAILED", f"Tradier order lookup failed ({error}).")

    tagged = [order for order in orders if str(order.get("tag") or "") == record["tag"]]
    if len(tagged) > 1:
        return mark_exit_unresolved(record, "MULTIPLE_TAGGED_ORDERS", f"{len(tagged)} orders carry this tag; tags are not unique at the broker.")
    if tagged:
        order = tagged[0]
        if not order.get("id"):
            return mark_exit_unresolved(record, "TAGGED_ORDER_WITHOUT_ID", "A tagged order was found but it has no order id.")
        order_id = str(order["id"])
        update_pending_exit(record, order_id=order_id, phase="ACCEPTED", unresolved_code="", absent_checks=0)
        record = dict(record, order_id=order_id, phase="ACCEPTED")
        add_bot_reason(f"{record['label']} reconciled {symbol}: tag {record['tag']} is broker order {order_id}")
        return advance_identified_exit(record, parse_broker_order_execution({"order": order}), config)

    if record.get("operator_hold"):
        return evaluate_operator_hold(record, orders, config)

    possible = [order for order in orders if order_could_be_this_exit(order, record)]
    if possible:
        ids = ", ".join(str(order.get("id") or "no id") for order in possible[:5])
        return mark_exit_unresolved(record, "POSSIBLE_MATCHING_ORDERS", f"{len(possible)} order(s) on this contract lack this intent's tag (ids: {ids}).")

    if market_now().date().isoformat() != record.get("intent_market_date"):
        return mark_exit_unresolved(record, "OUTSIDE_ORDER_LIST_WINDOW", "Tradier's order list covers only today's session, so it cannot show this earlier intent.")

    held = broker_position_quantity(symbol, positions)
    if held is None:
        return mark_exit_unresolved(record, "POSITION_UNKNOWN", "The broker position could not be confirmed.")
    if held < record["position_qty_at_intent"]:
        return mark_exit_unresolved(record, "POSITION_CHANGED_NO_ORDER", f"Position fell from {record['position_qty_at_intent']:g} to {held:g} but no matching order was found.")

    age = now_epoch - record["submitted_epoch"]
    checks = record.get("absent_checks", 0) + 1 if age >= EXIT_ABSENCE_SETTLE_SECONDS else 0
    if checks < EXIT_ABSENCE_CONFIRMATIONS:
        return mark_exit_unresolved(record, "NO_ORDER_FOUND_YET", f"No matching order in {checks} of {EXIT_ABSENCE_CONFIRMATIONS} required complete broker lookups.", absent_checks=checks)

    add_bot_reason(f"{record['label']} intent {record['tag']} for {symbol}: {checks} complete broker order lists show no such order and the position is unchanged; treated as never received")
    audit_exit_event("SKIP", record, config, skip_reason="exit intent confirmed absent from broker orders", order_status="NOT_FOUND")
    remove_pending_exit(record)
    block_exit_retry(symbol, config)
    return "FAILED"


def order_is_finished(status):
    status = str(status or "").lower()
    return status == "filled" or status in EXIT_ORDER_FAILED_STATUSES


def live_related_orders(orders, record):
    """Orders that may still be working for this intent, or could be mistaken for it."""
    tag = record.get("tag")
    order_id = str(record.get("order_id") or "")
    related = [order for order in orders
               if (tag and str(order.get("tag") or "") == tag)
               or (order_id and str(order.get("id") or "") == order_id)
               or order_could_be_this_exit(order, record)]
    return [order for order in related if not order_is_finished(order.get("status"))]


def manual_clear_evidence(record):
    """Returns (VERIFIED|UNVERIFIED|BLOCKED, detail) from current broker data."""
    code = record.get("unresolved_code")
    self_resolving = "The broker shows this order finished; the bot will complete it automatically on the next poll."
    order_id = record.get("order_id")
    if order_id and record.get("scope") != EXIT_UNSCOPED:
        status = str(fetch_broker_order_execution(order_id, retries=1).get("order_status") or "").lower()
        if status and code == "ORDER_MISMATCH":
            return "VERIFIED", "broker order does not match this exit"
        if status and order_is_finished(status):
            return ("VERIFIED", f"broker order is {status}") if code else ("BLOCKED", self_resolving)
        if status:
            return "BLOCKED", f"Broker reports order {order_id} is {status}. Cancel it in Tradier first; the bot will then finish this automatically."

    orders, error = fetch_broker_orders_for_reconciliation()
    if error:
        return "UNVERIFIED", f"order lookup failed ({error})"
    live = live_related_orders(orders, record)
    if live:
        ids = ", ".join(str(order.get("id") or "no id") for order in live[:5])
        return "BLOCKED", f"Broker shows a working sell on this contract (order {ids}). Cancel it in Tradier first or wait for it to finish."
    tagged = [order for order in orders if record.get("tag") and str(order.get("tag") or "") == record.get("tag")]
    if not order_id and len(tagged) == 1 and code != "ORDER_MISMATCH":
        return "BLOCKED", self_resolving
    return "VERIFIED", "complete broker order list shows no working order"


def release_exit_hold(record, config, detail):
    message = f"{record['label']} operator hold released for {record['symbol']}: {detail}. Automated exits may resume after the retry delay."
    print(message)
    add_bot_reason(message)
    audit_exit_event("SKIP", record, config, skip_reason="operator hold released by broker evidence", order_status="NOT_WORKING")
    remove_pending_exit(record)
    block_exit_retry(record["symbol"], config)
    return "FAILED"


def evaluate_operator_hold(record, orders, config):
    """Only complete broker data showing nothing working may end an operator hold."""
    live = live_related_orders(orders, record)
    if live:
        ids = ", ".join(str(order.get("id") or "no id") for order in live[:5])
        return mark_exit_unresolved(record, "HOLD_WORKING_ORDER", f"Broker shows working order(s): {ids}.")
    checks = record.get("absent_checks", 0) + 1
    if checks < EXIT_ABSENCE_CONFIRMATIONS:
        return mark_exit_unresolved(record, "HOLD_AWAITING_EVIDENCE", f"{checks} of {EXIT_ABSENCE_CONFIRMATIONS} required complete broker checks show no working order.", absent_checks=checks)
    return release_exit_hold(record, config, f"{checks} complete broker order lists show no working order")


def advance_operator_hold(record, config):
    now_epoch = time.time()
    if now_epoch - record.get("last_reconcile_epoch", 0) < EXIT_RECONCILE_INTERVAL_SECONDS:
        return "PENDING"
    update_pending_exit(record, last_reconcile_epoch=now_epoch)
    orders, error = fetch_broker_orders_for_reconciliation()
    if error:
        return mark_exit_unresolved(record, "ORDER_LOOKUP_FAILED", f"Tradier order lookup failed ({error}).")
    return evaluate_operator_hold(record, orders, config)


@broker_session
def clear_stuck_exit_intent(intent_id, confirmation):
    """Operator attestation about a stuck intent. Sends and cancels nothing.

    Protection is removed only if the broker can show nothing is working; otherwise the
    attestation is recorded and automated resubmission stays paused.
    """
    def finish(ok, message):
        print("EXIT INTENT MANUAL CLEAR:", message)
        add_bot_reason(f"EXIT INTENT MANUAL CLEAR: {message}")
        with BOT_LOCK:
            BOT_STATE["exit_intent_clear_result"] = message
        return ok, message

    config = load_config()
    if exit_broker_scope(config) is None:
        return finish(False, "NOT CLEARED: the selected broker account could not be verified.")
    record = next((item for item in pending_exit_records() if intent_id and item.get("intent_id") == intent_id), None)
    if record is None:
        return finish(False, "NOT CLEARED: that exit intent is no longer open on the selected account.")
    symbol = record["symbol"]
    if str(confirmation or "").strip() != symbol:
        return finish(False, f"NOT CLEARED: type the exact contract symbol {symbol} to confirm.")
    if time.time() - record.get("submitted_epoch", 0) < EXIT_ABSENCE_SETTLE_SECONDS:
        return finish(False, f"NOT CLEARED {symbol}: this intent is under {EXIT_ABSENCE_SETTLE_SECONDS}s old and its order may not be visible at the broker yet.")
    verdict, detail = manual_clear_evidence(record)
    if verdict == "BLOCKED":
        return finish(False, f"NOT CLEARED {symbol}: {detail}")

    if verdict == "UNVERIFIED":
        changes = {"operator_hold": True, "operator_hold_at": market_now().isoformat(), "absent_checks": 0}
        update_pending_exit(record, **changes)
        mark_exit_unresolved(dict(record, **changes), "OPERATOR_HOLD", f"Your confirmation is recorded, but the broker could not verify this order ({detail}).")
        audit_exit_event("SKIP", record, config, skip_reason="operator confirmed exit intent; broker unverifiable, automated resubmission paused", order_status=record.get("order_status", ""))
        return finish(False, f"HOLD {symbol}: your confirmation is recorded, but the broker cannot verify the order ({detail}). AUTOMATED RESUBMISSION REMAINS PAUSED until reconciliation confirms nothing is working.")

    with BOT_LOCK:
        BOT_STATE["pending_exit_orders"].pop(record["key"], None)
    if not persist_exit_state():
        with BOT_LOCK:
            BOT_STATE["pending_exit_orders"][record["key"]] = record
        return finish(False, f"NOT CLEARED {symbol}: the change could not be saved, so the intent would return after a restart.")

    block_exit_retry(symbol, config)
    try:
        log_bot_audit(
            "MANUAL_EXIT_CLEAR", "SELL", config.get("symbol", ""), record.get("market_context") or {}, config,
            option_symbol=symbol, order_id=record.get("order_id", ""), order_status=record.get("order_status", ""),
            skip_reason=f"operator cleared unresolved exit intent {record.get('tag') or 'NONE'} ({record.get('unresolved_code') or 'no code'}); broker: {detail}",
            exit_reason=record.get("reason", ""),
        )
    except OSError as exc:
        print("EXIT INTENT MANUAL CLEAR: audit row not written:", exc)
    return finish(True, (
        f"CLEARED {symbol} (tag {record.get('tag') or 'NONE'}): duplicate protection removed on your confirmation and broker evidence ({detail}). "
        f"No order was sent or canceled. The bot may exit any remaining position after the retry delay if a stop is hit. "
        f"Any fills from this intent are NOT in the bot's trade log; record them from Tradier."
    ))


def render_pending_exit_panel():
    records = pending_exit_records()
    closed = closed_exit_records()
    with BOT_LOCK:
        result = BOT_STATE.get("exit_intent_clear_result") or ""
        unscoped = [dict(item) for item in BOT_STATE["unscoped_exit_positions"].values()]
    recovery = exit_recovery_block_reason()
    if not records and not result and not closed and not unscoped and not recovery:
        return ""
    rows = ""
    for record in records:
        symbol = escape_html(record.get("symbol", ""))
        if record.get("operator_hold"):
            action = "<div><strong>AUTOMATED RESUBMISSION PAUSED</strong>: your confirmation is recorded; waiting for broker verification.</div>"
        else:
            action = f"""<form method="post" action="/resolve-exit-intent">
<input type="hidden" name="intent_id" value="{escape_html(record.get("intent_id", ""))}">
<label>I checked Tradier and no order for this exit is working. Type {symbol} to clear:
<input type="text" name="confirm_symbol" required autocomplete="off"></label>
<button type="submit">Clear exit intent</button>
</form>"""
        rows += f"""
<div class="item">
<div class="label">{symbol} qty {escape_html(record.get("qty", ""))}</div>
<div>{escape_html(record.get("status_message") or "Pending")}</div>
<div>Order ID: {escape_html(record.get("order_id") or "UNKNOWN")} · Tag: {escape_html(record.get("tag") or "NONE")}</div>
{action}
</div>"""
    for record in closed:
        rows += f"""
<div class="item">
<div class="label">{escape_html(record.get("symbol", ""))}: closed, awaiting position confirmation</div>
<div>{escape_html(record.get("status_message") or "Awaiting broker position data")}</div>
</div>"""
    for record in unscoped:
        symbol = escape_html(record.get("symbol", ""))
        rows += f"""
<div class="item">
<div class="label">{symbol}: saved stop state has no account</div>
<div>Peak {fmt_money(record.get("peak_price"))}. It was saved before stops were tracked per account and is not being used.</div>
<form method="post" action="/resolve-unscoped-exit-state">
<input type="hidden" name="migration_id" value="{escape_html(record.get("migration_id", ""))}">
<label>Type {symbol} to confirm: <input type="text" name="confirm_symbol" required autocomplete="off"></label>
<button type="submit" name="action" value="apply">Apply to selected account</button>
<button type="submit" name="action" value="discard">Discard</button>
</form>
</div>"""
    recovery_notice = f"<p><strong>{escape_html(recovery)}</strong></p>" if recovery else ""
    notice = f"<p>{escape_html(result)}</p>" if result else ""
    return f"""<div class="card">
<h2>Unresolved Exit Orders</h2>
<p>Exits for these contracts are blocked to prevent a duplicate sell. Reload this page for the latest status.</p>
{recovery_notice}{notice}{rows}
</div>

"""


def finalize_exit_fill(record, execution, filled_qty, config):
    """Log a completed (or partially completed) exit once. Trigger and fill stay separate."""
    symbol = record["symbol"]
    label = record["label"]
    filled_qty = int(filled_qty) if float(filled_qty).is_integer() else filled_qty
    fill_price = execution.get("fill_price")
    trigger_diagnostics = record.get("trigger_diagnostics") or {}
    trigger_price = valid_price(trigger_diagnostics.get("ExitTriggerPrice"))
    reference = next((price for price in (fill_price, trigger_price, record.get("reference_price")) if price is not None), None)
    if reference is None:
        message = (f"{label} COMPLETED AT BROKER {symbol} qty {filled_qty:g} but no fill or quote price is available; "
                   f"trade row NOT written. Reconcile order {record.get('order_id') or 'UNKNOWN'} manually.")
        print(message)
        add_bot_reason(message)
        set_last_error(message)
        return

    entry_price = record["entry_price"]
    filled_cost = record["cost_basis_per_contract"] * filled_qty
    pnl = option_pnl(reference, filled_cost, filled_qty)
    pnl_percent = ((reference - entry_price) / entry_price) * 100 if entry_price else 0
    market_context = dict(record.get("market_context") or {})
    entry_row = find_last_buy(symbol)
    complete = filled_qty >= record["qty"]
    # This row covers only the contracts that filled, so its fill is fully realized.
    row_execution = dict(execution, order_status="filled") if fill_price is not None else execution
    if not complete:
        market_context["decision_reasons"] = list(market_context.get("decision_reasons") or []) + [
            f"Partial exit: {filled_qty:g} of {record['qty']:g} filled; order ended {execution.get('order_status')}"
        ]
    execution_diagnostics = exit_execution_diagnostics(
        trigger_diagnostics, record.get("pre_exit_quote") or {}, market_context.get("price"), entry_row, row_execution
    )
    # The snapshot recomputes age at fill time; report the age when the order was sent.
    execution_diagnostics["ExitOptionQuoteAgeSeconds"] = record.get("pre_exit_quote_age_seconds", "")
    execution_diagnostics["ExitFillKey"] = record.get("intent_id", "")
    log_price = reference if record.get("source") != "BOT" else (trigger_price if trigger_price is not None else reference)
    trade_row = log_trade(
        "SELL", symbol, filled_qty, log_price, pnl, source=record.get("source", "HUMAN"),
        market_context=market_context, execution_diagnostics=execution_diagnostics,
    )
    if trade_row.get("PnLSource") == "BROKER_FILLS":
        pnl = trade_row["PnL"]
        pnl_percent = trade_row["PnLPercent"]
    with BOT_LOCK:
        max_profit = BOT_STATE["position_max_profit"].get(record["key"], pnl)
        max_drawdown = BOT_STATE["position_max_drawdown"].get(record["key"], pnl)
    audit_exit_event(
        "SELL", dict(record, market_context=market_context), config,
        exit_grade=trade_row.get("ExitGrade", ""), pnl=pnl, pnl_percent=pnl_percent,
        order_status=execution.get("order_status", ""), max_unrealized_profit=max_profit,
        max_drawdown=max_drawdown, hold_time=trade_row.get("HoldTime", ""),
        spy_price_at_exit=market_context.get("price", ""), market_state_at_exit=market_context.get("market_state", ""),
    )
    if record.get("source") == "BOT":
        update_last_trade_review({
            "entry_reason": entry_row.get("GradeReason", "") if entry_row else "",
            "exit_reason": record.get("reason", ""),
            "entry_grade": entry_row.get("EntryGrade", "") if entry_row else "",
            "exit_grade": trade_row.get("ExitGrade", ""),
            "final_pl": pnl,
            "max_profit": max_profit,
            "max_drawdown": max_drawdown,
            "hold_time": trade_row.get("HoldTime", ""),
            "human_or_bot": "BOT"
        })
    fill_text = f"broker fill {fill_price:.4f}" if fill_price is not None else "broker fill price UNAVAILABLE"
    trigger_text = f"bid trigger {trigger_price:.4f}" if trigger_price is not None else "no bid trigger"
    add_bot_reason(f"{label} {'FILLED' if complete else 'PARTIAL FILL'} {symbol} qty {filled_qty:g}/{record['qty']:g}: {fill_text}; {trigger_text}; {record.get('reason', '')}")


def default_pending_entry():
    return {
        "active": False,
        "id": "",
        "timestamp": "",
        "underlying": "",
        "direction": "",
        "decision": "",
        "option_symbol": "",
        "signal_generated": "",
        "momentum_status": "WAITING",
        "breakout_status": "PASS",
        "current_breakout_candle": 0,
        "required_breakout_candles": 0,
        "breakout_level": None,
        "starting_option_price": None,
        "confirmation_price_source": "",
        "required_confirmation_percent": 0,
        "confirmation_price": None,
        "current_option_price": None,
        "current_momentum_gain_percent": 0,
        "current_pre_confirmation_drawdown_percent": 0,
        "maximum_allowed_drawdown_percent": 0,
        "momentum_timeout_seconds": 0,
        "elapsed_time_seconds": 0,
        "retry_cooldown_remaining_seconds": 0,
        "final_cancellation_trigger": "",
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


def pending_history_record(pending, final_status=None, cancellation_reason=None, buy_submitted=False):
    status = final_status or pending.get("status", "WAITING FOR MOMENTUM")
    now_epoch = time.time()
    return {
        "id": pending.get("id", ""),
        "timestamp": pending.get("timestamp", ""),
        "direction": pending.get("direction", ""),
        "underlying": pending.get("underlying", ""),
        "option_symbol": pending.get("option_symbol", ""),
        "signal_generated": pending.get("signal_generated", pending.get("decision", "")),
        "momentum_status": pending.get("momentum_status", "WAITING"),
        "breakout_status": pending.get("breakout_status", "PASS"),
        "current_breakout_candle": pending.get("current_breakout_candle", 0),
        "required_breakout_candles": pending.get("required_breakout_candles", 0),
        "breakout_level": pending.get("breakout_level"),
        "starting_option_price": pending.get("starting_option_price"),
        "current_option_price": pending.get("current_option_price"),
        "highest_option_price": pending.get("highest_option_price"),
        "lowest_option_price": pending.get("lowest_option_price"),
        "confirmation_price": pending.get("confirmation_price"),
        "confirmation_price_source": pending.get("confirmation_price_source", ""),
        "current_momentum_gain_percent": pending.get("current_momentum_gain_percent", 0),
        "current_pre_confirmation_drawdown_percent": pending.get("current_pre_confirmation_drawdown_percent", 0),
        "momentum_confirmed": bool(pending.get("momentum_confirmed", pending.get("momentum_status") == "PASS")),
        "breakout_confirmed": bool(pending.get("breakout_confirmed", pending.get("breakout_status") == "PASS")),
        "maximum_allowed_drawdown_percent": pending.get("maximum_allowed_drawdown_percent", 0),
        "momentum_timeout_seconds": pending.get("momentum_timeout_seconds", 0),
        "elapsed_time_seconds": pending.get("elapsed_time_seconds", 0),
        "retry_cooldown_remaining_seconds": pending.get("retry_cooldown_remaining_seconds", 0),
        "final_cancellation_trigger": pending.get("final_cancellation_trigger", ""),
        "updated_at": pending.get("updated_at", ""),
        "updated_epoch": now_epoch,
        "completed_epoch": now_epoch if status in ["CANCELLED", "BUY SUBMITTED", "CONFIRMED"] else None,
        "final_status": status,
        "cancellation_reason": cancellation_reason or pending.get("reason", ""),
        "buy_submitted": bool(buy_submitted)
    }


def load_json_history(path):
    try:
        if not os.path.exists(path):
            return []
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except:
        return []


def write_json_history(path, rows):
    with open(path, "w") as f:
        json.dump(list(rows or []), f, indent=2)


def load_pending_history_rows(limit=None, visible=True):
    if visible and os.path.exists(PENDING_ENTRY_HISTORY_VISIBLE_FILE):
        rows = load_json_history(PENDING_ENTRY_HISTORY_VISIBLE_FILE)
    else:
        rows = load_json_history(PENDING_ENTRY_HISTORY_FILE)
    return rows[-limit:] if limit else rows


def write_pending_history_rows(rows, visible=True):
    path = PENDING_ENTRY_HISTORY_VISIBLE_FILE if visible else PENDING_ENTRY_HISTORY_FILE
    write_json_history(path, rows)


def is_test_pending_history_record(row):
    if not isinstance(row, dict):
        return False
    symbols = [
        row.get("option_symbol"),
        row.get("symbol"),
        row.get("contract", {}).get("symbol") if isinstance(row.get("contract"), dict) else None,
    ]
    if row.get("test_type") or row.get("is_test_record") is True:
        return True
    return any(str(symbol or "").upper().startswith("SPYTEST") for symbol in symbols)


def cleanup_test_pending_history_records():
    cleaned = {}
    for path in [
        PENDING_ENTRY_HISTORY_FILE,
        PENDING_ENTRY_HISTORY_VISIBLE_FILE,
        PENDING_ENTRY_HISTORY_BACKUP_FILE,
    ]:
        rows = load_json_history(path)
        if not rows:
            cleaned[path] = 0
            continue
        kept_rows = [row for row in rows if not is_test_pending_history_record(row)]
        removed_count = len(rows) - len(kept_rows)
        if removed_count:
            write_json_history(path, kept_rows)
        cleaned[path] = removed_count
    return cleaned


def upsert_history_record(rows, record):
    updated_rows = list(rows or [])
    existing_index = next((index for index, item in enumerate(updated_rows) if item.get("id") == record.get("id")), None)
    if existing_index is None:
        updated_rows.append(record)
    else:
        updated_rows[existing_index].update(record)
    return updated_rows


def upsert_pending_history(pending, final_status=None, cancellation_reason=None, buy_submitted=False):
    record = pending_history_record(pending, final_status, cancellation_reason, buy_submitted)
    permanent_history = upsert_history_record(load_pending_history_rows(visible=False), record)
    write_pending_history_rows(permanent_history, visible=False)

    visible_history = upsert_history_record(load_pending_history_rows(visible=True), record)
    write_pending_history_rows(visible_history, visible=True)

    with BOT_LOCK:
        BOT_STATE["pending_entry_history"] = visible_history
    return record


def pending_retry_cooldown_remaining(config, direction, option_symbol):
    cooldown_seconds = clamp_int(config.get("pending_entry_retry_cooldown_seconds", 60), 0, 600, 60)
    if cooldown_seconds <= 0:
        return 0

    rows = load_pending_history_rows(visible=False)
    now_epoch = time.time()
    for row in reversed(rows):
        if row.get("direction") != direction or row.get("option_symbol") != option_symbol:
            continue
        if row.get("final_status") not in ["CANCELLED"]:
            continue

        timestamp = row.get("completed_epoch") or row.get("updated_epoch") or row.get("cancelled_epoch")
        if timestamp is None:
            continue

        remaining = cooldown_seconds - int(now_epoch - float(timestamp))
        return max(0, remaining)

    return 0


def equivalent_pending_entry_active(direction, option_symbol):
    pending = get_pending_entry()
    return (
        pending.get("active")
        and pending.get("direction") == direction
        and pending.get("option_symbol") == option_symbol
    )


def create_pending_entry(config, decision, side, contract, contracts, start_price, market_context, price_source=""):
    momentum_enabled = bool(config.get("option_momentum_confirmation_enabled", True))
    breakout_enabled = bool(config.get("two_candle_or_confirmation_enabled", True))
    momentum_percent = float(config.get("option_momentum_percent", 1.0)) if momentum_enabled else 0
    timeout_seconds = int(config.get("confirmation_timeout_seconds", 60))
    max_drawdown_percent = max(0.0, min(50.0, safe_float(config.get("pre_confirmation_max_drawdown_percent", 5.0), 5.0)))
    started_epoch = time.time()
    timestamp = market_now().strftime("%H:%M:%S")
    pending_id = f"{market_now().strftime('%Y%m%d%H%M%S%f')}-{contract.get('symbol', '')}"
    confirmation_price = start_price * (1 + momentum_percent / 100) if momentum_enabled else start_price
    opposite_side = "PUT" if side == "CALL" else "CALL" if side == "PUT" else ""
    reset_opening_range_confirmation(opposite_side)
    breakout = initialize_opening_range_breakout_confirmation(config, side) if breakout_enabled else {
        "status": "PASS",
        "count": 0,
        "required": 0,
        "level": None,
        "reason": "two-candle opening-range confirmation disabled"
    }
    breakout_status = breakout.get("status", "WAITING")
    momentum_status = "WAITING" if momentum_enabled else "PASS"
    status = "WAITING FOR MOMENTUM" if momentum_enabled else "WAITING FOR BREAKOUT"
    if momentum_status == "PASS" and breakout_status == "PASS":
        status = "CONFIRMED"
    pending = {
        "active": True,
        "id": pending_id,
        "timestamp": timestamp,
        "underlying": config.get("symbol", ""),
        "direction": side,
        "decision": decision,
        "option_symbol": contract.get("symbol", ""),
        "signal_generated": decision,
        "momentum_status": momentum_status,
        "breakout_status": breakout_status,
        "current_breakout_candle": breakout.get("count", 0),
        "required_breakout_candles": breakout.get("required", 0),
        "breakout_level": breakout.get("level"),
        "starting_option_price": start_price,
        "confirmation_price_source": price_source,
        "required_confirmation_percent": momentum_percent,
        "confirmation_price": confirmation_price,
        "current_option_price": start_price,
        "highest_option_price": start_price,
        "lowest_option_price": start_price,
        "current_momentum_gain_percent": 0,
        "current_pre_confirmation_drawdown_percent": 0,
        "momentum_confirmed": momentum_status == "PASS",
        "breakout_confirmed": breakout_status == "PASS",
        "maximum_allowed_drawdown_percent": max_drawdown_percent,
        "momentum_timeout_seconds": timeout_seconds,
        "elapsed_time_seconds": 0,
        "retry_cooldown_remaining_seconds": 0,
        "final_cancellation_trigger": "",
        "time_remaining_seconds": timeout_seconds,
        "status": status,
        "reason": breakout.get("reason", ""),
        "started_epoch": started_epoch,
        "updated_at": market_now().isoformat(),
        "expires_epoch": started_epoch + timeout_seconds,
        "contracts": contracts,
        "contract": dict(contract),
        "market_context": dict(market_context)
    }
    set_pending_entry(pending)
    upsert_pending_history(pending, final_status="WAITING")
    add_bot_reason(
        f"PENDING BUY {side}: {contract.get('symbol', '')} start {start_price:.2f}, "
        f"confirm {confirmation_price:.2f}, breakout {breakout_status} "
        f"{breakout.get('count', 0)}/{breakout.get('required', 0)}, timeout {timeout_seconds}s"
    )
    log_bot_audit(
        "PENDING ENTRY",
        decision,
        config.get("symbol", ""),
        market_context,
        config,
        option_symbol=contract.get("symbol", ""),
        current_price=start_price,
        skip_reason="waiting for entry confirmation"
    )


def update_pending_confirmation_calculations(pending, current_price, current_price_source, breakout):
    pending["current_option_price"] = current_price
    pending["confirmation_price_source"] = current_price_source
    pending["elapsed_time_seconds"] = int(time.time() - float(pending.get("started_epoch") or time.time()))
    pending["updated_at"] = market_now().isoformat()
    pending["breakout_status"] = breakout.get("status", "WAITING")
    pending["current_breakout_candle"] = breakout.get("count", 0)
    pending["required_breakout_candles"] = breakout.get("required", 0)
    pending["breakout_level"] = breakout.get("level")
    pending["breakout_confirmed"] = pending["breakout_status"] == "PASS"
    refresh_pending_time_remaining(pending)

    start_price = safe_float(pending.get("starting_option_price"))
    if current_price is None or not start_price:
        pending["momentum_confirmed"] = pending.get("momentum_status") == "PASS"
        return pending

    current_price = float(current_price)
    previous_high = pending.get("highest_option_price")
    previous_low = pending.get("lowest_option_price")
    highest_price = max(
        current_price,
        safe_float(previous_high, current_price) if previous_high not in [None, ""] else current_price,
        start_price,
    )
    lowest_price = min(
        current_price,
        safe_float(previous_low, current_price) if previous_low not in [None, ""] else current_price,
        start_price,
    )
    gain_percent = ((current_price - start_price) / start_price) * 100
    drawdown_percent = max(0, ((start_price - lowest_price) / start_price) * 100)

    pending["highest_option_price"] = highest_price
    pending["lowest_option_price"] = lowest_price
    pending["current_momentum_gain_percent"] = gain_percent
    pending["current_pre_confirmation_drawdown_percent"] = drawdown_percent
    pending["momentum_confirmed"] = pending.get("momentum_status") == "PASS"
    return pending


def pending_entry_snapshot():
    pending = get_pending_entry()
    return refresh_pending_time_remaining(pending)


def pending_entry_history_snapshot(limit=None):
    history = load_pending_history_rows(visible=True)
    return history[-limit:] if limit else history


def clear_pending_entry_history():
    current_history = load_pending_history_rows(visible=True)
    write_json_history(PENDING_ENTRY_HISTORY_BACKUP_FILE, current_history)
    write_pending_history_rows([], visible=True)
    with BOT_LOCK:
        BOT_STATE["pending_entry_history_backup"] = current_history
        BOT_STATE["pending_entry_history"] = []


def restore_pending_entry_history():
    backup_history = load_json_history(PENDING_ENTRY_HISTORY_BACKUP_FILE)
    write_pending_history_rows(backup_history, visible=True)
    with BOT_LOCK:
        BOT_STATE["pending_entry_history_backup"] = backup_history
        BOT_STATE["pending_entry_history"] = backup_history
    return True


def initialize_pending_entry_history():
    cleanup_test_pending_history_records()
    with BOT_LOCK:
        BOT_STATE["pending_entry_history"] = load_pending_history_rows(visible=True)


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
            # Peaks are deliberately kept: wiping them here would silently loosen the
            # armed stop of a position held overnight. They clear on confirmed exit.
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
        r = broker_request("GET",
            "/markets/history",
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
        r = broker_request("GET",
            "/markets/timesales",
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


def completed_intraday_bars(symbol, now=None):
    now = now or market_now()
    return [
        bar for bar in get_intraday_bars(symbol)
        if bar.get("time") and bar["time"] + timedelta(minutes=1) <= now
    ]


def default_opening_range_confirmation_state():
    return {
        "count": 0,
        "status": "WAITING",
        "level": None,
        "last_processed_candle": "",
        "reason": ""
    }


def reset_opening_range_confirmation(direction):
    direction = str(direction or "").upper()
    if direction not in ["CALL", "PUT"]:
        return
    with BOT_LOCK:
        BOT_STATE["opening_range_confirmation"][direction] = default_opening_range_confirmation_state()


def initialize_opening_range_breakout_confirmation(config, direction):
    direction = str(direction or "").upper()
    required = clamp_int(config.get("required_breakout_candles", 2), 1, 10, 2)
    if not config.get("two_candle_or_confirmation_enabled", True):
        return {
            "status": "PASS",
            "count": 0,
            "required": 0,
            "level": None,
            "last_processed_candle": "",
            "reason": "two-candle opening-range confirmation disabled"
        }

    now = market_now()
    today = now.date()
    symbol = config.get("symbol", "SPY")
    opening_range_start = datetime_time(9, 30)
    opening_range_end = parse_clock(config.get("decision_time", "09:35"))
    bars = completed_intraday_bars(symbol, now)
    opening_bars = [
        bar for bar in bars
        if bar["time"].date() == today and opening_range_start <= bar["time"].time() < opening_range_end
    ]
    opening_high, opening_low = high_low_from_bars(opening_bars)
    level = opening_high if direction == "CALL" else opening_low if direction == "PUT" else None
    confirmation_bars = [
        bar for bar in bars
        if bar["time"].date() == today
        and opening_range_end <= bar["time"].time() <= datetime_time(16, 0)
    ]
    last_processed = confirmation_bars[-1]["time"].strftime("%Y-%m-%d %H:%M:%S") if confirmation_bars else ""
    state = {
        "count": 0,
        "status": "WAITING",
        "level": level,
        "last_processed_candle": last_processed,
        "reason": "waiting for completed breakout candle after signal"
    }
    if level is None:
        state["reason"] = "opening range candles unavailable"

    with BOT_LOCK:
        BOT_STATE["opening_range_confirmation"][direction] = dict(state)
    return {
        "status": state["status"],
        "count": state["count"],
        "required": required,
        "level": state["level"],
        "last_processed_candle": state["last_processed_candle"],
        "reason": state["reason"]
    }


def update_opening_range_breakout_confirmation(config, direction):
    direction = str(direction or "").upper()
    required = clamp_int(config.get("required_breakout_candles", 2), 1, 10, 2)
    if direction not in ["CALL", "PUT"]:
        return {
            "status": "FAIL",
            "count": 0,
            "required": required,
            "level": None,
            "reason": "invalid breakout direction"
        }

    if not config.get("two_candle_or_confirmation_enabled", True):
        return {
            "status": "PASS",
            "count": required,
            "required": required,
            "level": None,
            "reason": "two-candle opening-range confirmation disabled"
        }

    now = market_now()
    today = now.date()
    symbol = config.get("symbol", "SPY")
    opening_range_start = datetime_time(9, 30)
    opening_range_end = parse_clock(config.get("decision_time", "09:35"))

    with BOT_LOCK:
        state = dict(BOT_STATE.get("opening_range_confirmation", {}).get(direction) or default_opening_range_confirmation_state())

    if state.get("status") == "PASS":
        return {
            "status": "PASS",
            "count": int(state.get("count") or required),
            "required": required,
            "level": state.get("level"),
            "last_processed_candle": state.get("last_processed_candle", ""),
            "reason": state.get("reason", "opening-range breakout confirmed")
        }

    if now.time() < opening_range_end:
        state.update({
            "count": 0,
            "status": "WAITING",
            "level": None,
            "reason": "opening range is not complete"
        })
        with BOT_LOCK:
            BOT_STATE["opening_range_confirmation"][direction] = dict(state)
        return {
            "status": state["status"],
            "count": state["count"],
            "required": required,
            "level": state["level"],
            "reason": state["reason"]
        }

    bars = completed_intraday_bars(symbol, now)
    opening_bars = [
        bar for bar in bars
        if bar["time"].date() == today and opening_range_start <= bar["time"].time() < opening_range_end
    ]
    opening_high, opening_low = high_low_from_bars(opening_bars)
    level = opening_high if direction == "CALL" else opening_low

    if level is None:
        state.update({
            "count": 0,
            "status": "WAITING",
            "level": None,
            "reason": "opening range candles unavailable"
        })
        with BOT_LOCK:
            BOT_STATE["opening_range_confirmation"][direction] = dict(state)
        return {
            "status": state["status"],
            "count": state["count"],
            "required": required,
            "level": state["level"],
            "reason": state["reason"]
        }

    confirmation_bars = [
        bar for bar in bars
        if bar["time"].date() == today
        and opening_range_end <= bar["time"].time() <= datetime_time(16, 0)
    ]
    last_processed = state.get("last_processed_candle") or ""
    new_bars = [
        bar for bar in confirmation_bars
        if bar["time"].strftime("%Y-%m-%d %H:%M:%S") > last_processed
    ]

    state["level"] = level
    state["required"] = required
    for bar in new_bars:
        candle_id = bar["time"].strftime("%Y-%m-%d %H:%M:%S")
        close_price = float(bar.get("close") or 0)
        closes_beyond_level = close_price > level if direction == "CALL" else close_price < level

        if closes_beyond_level:
            state["count"] = min(required, int(state.get("count") or 0) + 1)
            state["status"] = "PASS" if state["count"] >= required else "WAITING"
            state["reason"] = (
                f"{direction} breakout candle {state['count']}/{required}: "
                f"{close_price:.2f} {'>' if direction == 'CALL' else '<'} {level:.2f}"
            )
        else:
            state["count"] = 0
            state["status"] = "FAIL"
            state["reason"] = f"{direction} breakout reset: candle closed inside opening range"

        state["last_processed_candle"] = candle_id

    if not new_bars and state.get("status") != "PASS":
        state["status"] = "WAITING"
        state["reason"] = state.get("reason") or "waiting for completed breakout candle"

    with BOT_LOCK:
        BOT_STATE["opening_range_confirmation"][direction] = dict(state)

    return {
        "status": state.get("status", "WAITING"),
        "count": int(state.get("count") or 0),
        "required": required,
        "level": state.get("level"),
        "last_processed_candle": state.get("last_processed_candle", ""),
        "reason": state.get("reason", "")
    }


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
    completed_close_bars = [
        bar for bar in regular_bars
        if (
            opening_range_end <= bar["time"].time() <= datetime_time(16, 0)
            and bar["time"] + timedelta(minutes=1) <= now
        )
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
        "last_updated": now.strftime("%Y-%m-%d %H:%M:%S"),
        "completed_closes": tuple(bar["close"] for bar in completed_close_bars)
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
        "ema_value": None,
        "ema_slope": None,
        "ma_value": None,
        "ma_slope": None,
        "macd_value": None,
        "macd_signal_value": None,
        "macd_histogram": None,
        "macd_histogram_slope": None,
        "vwap_state": "NEUTRAL",
        "volume_state": "NEUTRAL",
        "opening_direction": "NEUTRAL",
        "tick_statistics": {"green_ticks": 0, "red_ticks": 0, "green_percent": 0, "red_percent": 0},
        "levels": levels,
        "market_structure_source": "Tradier daily/intraday bars",
        "market_date": market_now().date().isoformat(),
        "market_structure_last_updated": "",
        "completed_closes": (),
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


def score_direction(bullish_score, bearish_score):
    if bullish_score > bearish_score:
        return "CALL"
    if bearish_score > bullish_score:
        return "PUT"
    return "NONE"


def direction_safety_check(decision, market_context):
    bullish_score = int(market_context.get("bullish_score") or 0)
    bearish_score = int(market_context.get("bearish_score") or 0)
    confidence = abs(bullish_score - bearish_score)
    dominance_percent = calculate_dominance_percent(bullish_score, bearish_score)
    selected_direction = "CALL" if decision == "BUY CALL" else "PUT" if decision == "BUY PUT" else "NONE"
    winning_direction = score_direction(bullish_score, bearish_score)
    passed = (
        (selected_direction == "CALL" and bullish_score > bearish_score)
        or (selected_direction == "PUT" and bearish_score > bullish_score)
    )
    return {
        "passed": passed,
        "selected_direction": selected_direction,
        "winning_direction": winning_direction,
        "bullish_score": bullish_score,
        "bearish_score": bearish_score,
        "confidence": confidence,
        "dominance_percent": dominance_percent
    }


def log_direction_safety_result(prefix, decision, market_context):
    result = direction_safety_check(decision, market_context)
    message = (
        f"{prefix}: bullish={result['bullish_score']} bearish={result['bearish_score']} "
        f"confidence={result['confidence']} dominance={result['dominance_percent']:.1f}% "
        f"selected={result['selected_direction']} winner={result['winning_direction']} "
        f"result={'PASS' if result['passed'] else 'FAIL'}"
    )
    add_bot_reason(message)
    print(message)
    return result


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
    previous_ema_fast = exponential_average(prices[:-1], s.get("ema_fast", 1)) if len(prices) > 1 else None
    previous_ma_fast = moving_average(prices[:-1], s.get("ma_fast", 5)) if len(prices) > 1 else None
    ema_slope = ema_fast - previous_ema_fast if ema_fast is not None and previous_ema_fast is not None else None
    ma_slope = ma_fast - previous_ma_fast if ma_fast is not None and previous_ma_fast is not None else None

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
    macd_value = None
    macd_signal_value = None
    macd_histogram = None
    macd_histogram_slope = None
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
        position_is_option = len(position_symbol) > 6
        if position_is_option:
            position_exit_quote = get_option_exit_price(position_quote, config)
            position_price = position_exit_quote["price"]
            if position_price is None:
                current_pl = None
                reasons.append(position_exit_quote["message"])
        else:
            position_price = get_quote_price(position_quote)
        if position_price is not None and qty:
            entry_price = option_entry_price(cost_basis, qty) if position_is_option else cost_basis / qty
            multiplier = 100 if position_is_option else 1
            current_pl = (position_price - entry_price) * qty * multiplier
            position_key = exit_state_key(position_symbol, config)
            with BOT_LOCK:
                max_profit = max(BOT_STATE["position_max_profit"].get(position_key, current_pl), current_pl)
                max_drawdown = min(BOT_STATE["position_max_drawdown"].get(position_key, current_pl), current_pl)
                BOT_STATE["position_max_profit"][position_key] = max_profit
                BOT_STATE["position_max_drawdown"][position_key] = max_drawdown
            # Read-only: decide_surfer_action/fast_exit_poll own the peak updates.
            stop_values = calculate_stop_state(position_symbol, entry_price, position_price, config)
            trailing_stop_price = stop_values.get("effective_trailing_stop")
            distance_to_trailing_stop = position_price - trailing_stop_price if trailing_stop_price is not None else None
            if current_pl > 0:
                reasons.append("Current P/L supports holding")

    minimum_signals = int(e.get("minimum_signals", 3))
    minimum_confidence = int(config.get("minimum_confidence", 2))
    minimum_dominance_percent = int(config.get("minimum_dominance_percent", 60))
    confidence = abs(bullish_score - bearish_score)
    dominance_percent = calculate_dominance_percent(bullish_score, bearish_score)
    market_state = "NEUTRAL"

    if (
        bullish_score > bearish_score
        and bullish_score >= minimum_signals
        and confidence >= minimum_confidence
        and dominance_percent >= minimum_dominance_percent
    ):
        market_state = "BULLISH"
        current_signal = "CALL"
    elif (
        bearish_score > bullish_score
        and bearish_score >= minimum_signals
        and confidence >= minimum_confidence
        and dominance_percent >= minimum_dominance_percent
    ):
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
        "ema_value": ema_fast,
        "ema_slope": ema_slope,
        "ma_value": ma_fast,
        "ma_slope": ma_slope,
        "macd_value": macd_value,
        "macd_signal_value": macd_signal_value,
        "macd_histogram": macd_histogram,
        "macd_histogram_slope": macd_histogram_slope,
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
        "completed_closes": structure.get("completed_closes", ()),
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
    original_market_context = market_context if isinstance(market_context, dict) else None
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
        if len(symbol) <= 6:
            # try_surfer_exit only ever closes option contracts.
            return "HOLD", ["Non-option position: stop evaluation not applicable"] + market_context.get("reasons", [])

        quote = get_market_quote(symbol)
        entry_price = option_entry_price(cost_basis, qty) if qty else 0
        evaluation = evaluate_option_exit(symbol, entry_price, quote, config)

        if not evaluation["ok"]:
            message, exposure = record_exit_quote_error(symbol, qty, entry_price, evaluation, "SURFER EXIT")
            return QUOTE_DATA_ERROR_DECISION, [message, exposure]

        current_price = evaluation["bid"]
        stop_values = evaluation["stop_values"]
        hard_stop_price = evaluation["hard_stop_price"]
        trailing_stop_price = evaluation["effective_stop"]
        stop_control_rule = stop_values.get("stop_control_rule", "HARD STOP")
        pnl_percent = ((current_price - entry_price) / entry_price) * 100 if entry_price else 0
        quote_line = exit_quote_log_line(symbol, evaluation["exit_quote"], evaluation["stop_price"], evaluation["exit_code"])
        print(quote_line)

        if evaluation["decision"] == "SELL":
            trigger_snapshot = exit_trigger_diagnostics(
                quote,
                market_context.get("price"),
                hard_stop_price,
                stop_values.get("percentage_trailing_stop"),
                trailing_stop_price,
            )
            market_context["exit_trigger_diagnostics"] = trigger_snapshot
            if original_market_context is not None:
                original_market_context["exit_trigger_diagnostics"] = trigger_snapshot
            if evaluation["exit_code"] == EXIT_CODE_HARD_STOP:
                return "SELL", ["Hard stop hit", f"P/L {pnl_percent:.2f}%", quote_line]
            return "SELL", ["Trailing stop hit", f"Bid {current_price:.2f} <= effective stop {trailing_stop_price:.2f}", f"Rule: {stop_control_rule}", quote_line]

        trailing_reason = "Trailing stop not hit" if stop_values.get("stop_armed") else "Trailing stop inactive"
        return "HOLD", ["Hard stop not hit", trailing_reason] + market_context.get("reasons", [])

    if (
        market_context.get("bullish_score", 0) > market_context.get("bearish_score", 0)
        and market_context.get("bullish_score", 0) >= minimum_signals
        and confidence >= minimum_confidence
        and dominance_percent >= minimum_dominance_percent
    ):
        return "BUY CALL", market_context.get("reasons", [])
    if (
        market_context.get("bearish_score", 0) > market_context.get("bullish_score", 0)
        and market_context.get("bearish_score", 0) >= minimum_signals
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


def ensure_tournament_runtime_states(profiles):
    global TOURNAMENT_RUNTIME_STATES, TOURNAMENT_RUNTIME_STATES_LOADED
    if not TOURNAMENT_RUNTIME_STATES_LOADED:
        existing = load_tournament_state(TOURNAMENT_STATE_FILE, profiles)
        TOURNAMENT_RUNTIME_STATES_LOADED = True
    else:
        existing = TOURNAMENT_RUNTIME_STATES if isinstance(TOURNAMENT_RUNTIME_STATES, dict) else {}
    missing_profiles = {
        profile_id: profile
        for profile_id, profile in profiles.items()
        if profile_id not in existing
    }
    if missing_profiles:
        existing.update(create_all_initial_states(missing_profiles))
    TOURNAMENT_RUNTIME_STATES = {
        profile_id: existing[profile_id]
        for profile_id in profiles
    }
    return TOURNAMENT_RUNTIME_STATES


def mark_tournament_dirty():
    global TOURNAMENT_STATE_DIRTY
    TOURNAMENT_STATE_DIRTY = True


def mark_tournament_saved():
    global TOURNAMENT_STATE_DIRTY
    TOURNAMENT_STATE_DIRTY = False
    now_iso = market_now().isoformat()
    TOURNAMENT_HEALTH["tournament_last_state_save"] = now_iso
    TOURNAMENT_HEALTH["state_save_failed"] = False
    with BOT_LOCK:
        BOT_STATE["tournament_health"] = dict(TOURNAMENT_HEALTH)


def save_tournament_state_safe(states):
    try:
        save_tournament_state(TOURNAMENT_STATE_FILE, states)
        mark_tournament_saved()
        return True
    except Exception as error:
        TOURNAMENT_HEALTH["state_save_failed"] = True
        TOURNAMENT_HEALTH["tournament_last_error"] = str(error)
        with BOT_LOCK:
            BOT_STATE["tournament_health"] = dict(TOURNAMENT_HEALTH)
        print("TOURNAMENT STATE SAVE ERROR:", error)
        return False


def seconds_since_iso(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=MARKET_TZ)
        return max(0, int((market_now() - parsed.astimezone(MARKET_TZ)).total_seconds()))
    except Exception:
        return None


def tournament_health_snapshot():
    with TOURNAMENT_LOCK:
        profiles = build_tournament_profiles(load_config())
        states = ensure_tournament_runtime_states(profiles)
        open_positions = len(get_unique_tournament_option_symbols(states))
    entry_age = seconds_since_iso(TOURNAMENT_HEALTH.get("tournament_last_entry_cycle"))
    exit_age = seconds_since_iso(TOURNAMENT_HEALTH.get("tournament_last_exit_poll"))
    quote_age = seconds_since_iso(TOURNAMENT_HEALTH.get("tournament_last_successful_quote"))
    save_age = seconds_since_iso(TOURNAMENT_HEALTH.get("tournament_last_state_save"))
    exit_alive = bool(TOURNAMENT_EXIT_THREAD and TOURNAMENT_EXIT_THREAD.is_alive())
    entry_alive = bool(BOT_STATE.get("thread_alive")) or TOURNAMENT_HEALTH.get("tournament_entry_loop_alive")
    recovery_complete = bool(TOURNAMENT_HEALTH.get("tournament_recovery_complete"))
    last_error = TOURNAMENT_HEALTH.get("tournament_last_error")
    status = "HEALTHY"
    warnings = []
    if not recovery_complete:
        status = "FAILED"
        warnings.append("recovery incomplete")
    if not exit_alive:
        status = "FAILED"
        warnings.append("exit monitor is not alive")
    if TOURNAMENT_HEALTH.get("state_save_failed"):
        status = "FAILED"
        warnings.append("state save failed")
    if quote_age is not None and quote_age > 30:
        status = "DEGRADED" if status == "HEALTHY" else status
        warnings.append("quote is stale")
    if TOURNAMENT_HEALTH.get("state_source") == "BACKUP" or TOURNAMENT_HEALTH.get("trades_source") == "BACKUP":
        status = "DEGRADED" if status == "HEALTHY" else status
        warnings.append("backup recovery used")
    if exit_age is not None and exit_age > 30:
        status = "DEGRADED" if status == "HEALTHY" else status
        warnings.append("exit loop delayed")
    if last_error and last_error not in {"None", ""}:
        status = "DEGRADED" if status == "HEALTHY" else status

    return {
        "overall_status": status,
        "profile_count": len(profiles),
        "open_position_count": open_positions,
        "entry_loop_alive": bool(entry_alive),
        "exit_loop_alive": exit_alive,
        "seconds_since_last_entry_cycle": entry_age,
        "seconds_since_last_exit_poll": exit_age,
        "seconds_since_last_successful_quote": quote_age,
        "state_save_age": save_age,
        "recovery_status": TOURNAMENT_HEALTH.get("tournament_recovery_status"),
        "recovery_complete": recovery_complete,
        "last_error": last_error or "None",
        "warnings": warnings,
        **dict(TOURNAMENT_HEALTH),
    }


def initialize_tournament_recovery():
    global TOURNAMENT_RUNTIME_STATES, TOURNAMENT_RUNTIME_STATES_LOADED
    config = load_config()
    profiles = build_tournament_profiles(config)
    with TOURNAMENT_LOCK:
        states = load_tournament_state(TOURNAMENT_STATE_FILE, profiles)
        trades = list_tournament_trades(path=TOURNAMENT_TRADES_FILE)
        states, trades, summary = reconcile_tournament_state(profiles, states, trades)
        daily_reset_changed = apply_daily_resets(profiles, states, time.time())
        TOURNAMENT_RUNTIME_STATES = states
        TOURNAMENT_RUNTIME_STATES_LOADED = True
        if summary.get("changed") or daily_reset_changed:
            tournament_trades_module.save_tournament_trades(trades, TOURNAMENT_TRADES_FILE)
            save_tournament_state_safe(states)
        positions = serialize_tournament_positions(states)
        state_by_profile = refresh_tournament_state_snapshot(states)

    TOURNAMENT_HEALTH["tournament_recovery_complete"] = True
    TOURNAMENT_HEALTH["tournament_recovery_status"] = "COMPLETE"
    TOURNAMENT_HEALTH["state_source"] = tournament_state_module.LAST_STATE_LOAD_SOURCE
    TOURNAMENT_HEALTH["trades_source"] = tournament_trades_module.LAST_TRADES_LOAD_SOURCE
    with BOT_LOCK:
        BOT_STATE["tournament_virtual_positions"] = positions
        BOT_STATE["tournament_state_by_profile"] = state_by_profile
        BOT_STATE["tournament_health"] = dict(TOURNAMENT_HEALTH)

    print("TOURNAMENT RECOVERY COMPLETE")
    print("profiles:", summary.get("profiles", 0))
    print("open_positions:", summary.get("open_positions", 0))
    print("recovered_positions:", summary.get("recovered_positions", 0))
    print("cancelled_invalid:", summary.get("cancelled_invalid", 0))
    print("duplicates_cleaned:", summary.get("duplicates_cleaned", 0))
    print("state_source:", TOURNAMENT_HEALTH["state_source"])
    return summary


def add_snapshot_option_fields(signal, call_contract=None, put_contract=None):
    signal = dict(signal or {})
    diagnostics = list(signal.get("option_diagnostics") or [])

    def add_contract(prefix, contract, required_direction):
        if not contract:
            return
        option_symbol = contract.get("symbol")
        if option_symbol_direction(option_symbol) != required_direction:
            diagnostics.append(f"INVALID_{prefix.upper()}_OPTION_SYMBOL")
            return
        bid = safe_float(contract.get("bid"), None)
        ask = safe_float(contract.get("ask"), None)
        last = safe_float(contract.get("last"), None)
        midpoint = ((bid + ask) / 2) if bid is not None and ask is not None and bid > 0 and ask > 0 else None
        signal[f"{prefix}_option_symbol"] = option_symbol
        signal[f"{prefix}_option_bid"] = bid
        signal[f"{prefix}_option_ask"] = ask
        signal[f"{prefix}_option_last"] = last
        signal[f"{prefix}_option_midpoint"] = midpoint

    add_contract("call", call_contract, "CALL")
    add_contract("put", put_contract, "PUT")
    if diagnostics:
        signal["option_diagnostics"] = diagnostics
    if call_contract or put_contract:
        signal["option_quote_timestamp"] = market_now().isoformat()
    return signal


def locked_quote_row_from_quote(quote):
    if not isinstance(quote, dict):
        return None
    bid = safe_float(quote.get("bid"), None)
    ask = safe_float(quote.get("ask"), None)
    last = safe_float(quote.get("last"), None)
    midpoint = ((bid + ask) / 2) if bid is not None and ask is not None and bid > 0 and ask > 0 else None
    return {
        "symbol": quote.get("symbol"),
        "bid": bid,
        "ask": ask,
        "last": last,
        "midpoint": midpoint,
        "timestamp": quote.get("_fetched_at"),
    }


def enrich_snapshot_with_locked_candidate_quotes(snapshot, states):
    symbols = sorted({
        state.momentum_candidate.option_symbol
        for state in states.values()
        if state.momentum_candidate
        and state.momentum_candidate.status in {"TRACKING", "CONFIRMED"}
        and state.momentum_candidate.option_symbol
    })
    if not symbols:
        return snapshot

    locked_quotes = dict(snapshot.locked_option_quotes or {})
    for symbol in symbols:
        if symbol in locked_quotes:
            continue
        quote = get_market_quote(symbol)
        row = locked_quote_row_from_quote(quote)
        if row:
            locked_quotes[symbol] = row
    return replace(snapshot, locked_option_quotes=locked_quotes)


def snapshot_for_confirmed_candidate_entry(snapshot, state):
    candidate = state.momentum_candidate
    if not candidate or candidate.status != "CONFIRMED":
        return snapshot
    quote = (snapshot.locked_option_quotes or {}).get(candidate.option_symbol)
    if not isinstance(quote, dict):
        return snapshot
    if candidate.direction == "CALL":
        return replace(
            snapshot,
            call_option_symbol=candidate.option_symbol,
            call_option_bid=safe_float(quote.get("bid"), None),
            call_option_ask=safe_float(quote.get("ask"), None),
            call_option_last=safe_float(quote.get("last"), None),
            call_option_midpoint=safe_float(quote.get("midpoint"), None),
        )
    if candidate.direction == "PUT":
        return replace(
            snapshot,
            put_option_symbol=candidate.option_symbol,
            put_option_bid=safe_float(quote.get("bid"), None),
            put_option_ask=safe_float(quote.get("ask"), None),
            put_option_last=safe_float(quote.get("last"), None),
            put_option_midpoint=safe_float(quote.get("midpoint"), None),
        )
    return snapshot


def print_tournament_decisions(decisions):
    print("TOURNAMENT DECISIONS")
    for profile_id in ["BOT_A_BASELINE", "BOT_B_MOMENTUM", "BOT_C_TWO_CANDLE_OR", "BOT_D_COMBINED"]:
        decision = decisions.get(profile_id)
        if not decision:
            continue
        print(f"{profile_id}:")
        print("accepted:", decision.accepted)
        print("status:", decision.status)
        print("direction:", decision.direction)
        print("reason:", decision.rejection_reason)


def serialize_tournament_positions(states):
    rows = []
    now_epoch = time.time()
    for profile_id, state in states.items():
        position = state.virtual_position
        if position and position.status == "OPEN":
            row = asdict(position)
            row.update(stop_snapshot(position))
            row["position_source"] = tournament_position_source(position)
            cooldown_until = state.virtual_entry_cooldown_until_epoch
            row["virtual_trades_today"] = state.virtual_trades_today
            row["cooldown_remaining_seconds"] = max(0, int(cooldown_until - now_epoch)) if cooldown_until else 0
            row["last_entry_fingerprint"] = state.last_entry_fingerprint
            rows.append(row)
    return rows


def tournament_position_source(position):
    if not position:
        return "NONE"
    return "TEST" if is_synthetic_proof_position(position) else "REAL"


def tournament_option_money_snapshot(snapshot, profile, decision):
    direction = decision.direction or decision.final_direction or decision.preliminary_direction
    option_premium = None
    if direction == "CALL":
        option_premium = snapshot.call_option_ask if snapshot.call_option_ask is not None else snapshot.option_ask
    elif direction == "PUT":
        option_premium = snapshot.put_option_ask if snapshot.put_option_ask is not None else snapshot.option_ask
    contracts = int((profile.config or {}).get("contracts", 1) or 1)
    total_position_cost = float(option_premium) * 100 * contracts if option_premium is not None else None
    return {
        "option_premium": option_premium,
        "contracts": contracts,
        "total_position_cost": total_position_cost,
        "maximum_position_cost_dollars": float((profile.config or {}).get("maximum_position_cost_dollars", 0) or 0),
        "call_contract": snapshot.call_option_symbol,
        "put_contract": snapshot.put_option_symbol,
        "candidate_contract_type": option_symbol_direction(decision.momentum_candidate_option_symbol),
        "decision_direction": direction,
        "contract_direction_match": (
            option_symbol_matches_direction(decision.momentum_candidate_option_symbol, direction)
            if decision.momentum_candidate_option_symbol and direction in {"CALL", "PUT"}
            else None
        ),
        "contract_direction_mismatch_reason": (
            "OPTION_DIRECTION_MISMATCH"
            if decision.momentum_candidate_option_symbol
            and direction in {"CALL", "PUT"}
            and not option_symbol_matches_direction(decision.momentum_candidate_option_symbol, direction)
            else None
        ),
        "option_diagnostics": list(snapshot.option_diagnostics or ()),
    }


def tournament_pipeline_counters(state):
    counters = state.pipeline_counters if isinstance(state.pipeline_counters, dict) else {}
    defaults = {
        "candidates_started": 0,
        "momentum_confirmed": 0,
        "or_confirmed": 0,
        "decisions_accepted": 0,
        "entries_attempted": 0,
        "entries_opened": 0,
        "entry_block_reasons": {},
    }
    for key, value in defaults.items():
        if key not in counters or (key == "entry_block_reasons" and not isinstance(counters.get(key), dict)):
            counters[key] = dict(value) if isinstance(value, dict) else value
    state.pipeline_counters = counters
    return counters


def increment_tournament_counter(state, key, amount=1):
    counters = tournament_pipeline_counters(state)
    counters[key] = int(counters.get(key, 0) or 0) + amount


def increment_tournament_block_reason(state, reason):
    counters = tournament_pipeline_counters(state)
    reasons = counters.setdefault("entry_block_reasons", {})
    reason = str(reason or "UNKNOWN")
    reasons[reason] = int(reasons.get(reason, 0) or 0) + 1


def record_tournament_pipeline_counters(states, before_candidates, decisions):
    for profile_id, decision in decisions.items():
        state = states[profile_id]
        before = before_candidates.get(profile_id)
        after = state.momentum_candidate
        if before is None and after is not None:
            increment_tournament_counter(state, "candidates_started")
        if decision.momentum_required and decision.momentum_passed:
            increment_tournament_counter(state, "momentum_confirmed")
        if decision.or_confirmation_required and decision.two_candle_or_passed:
            increment_tournament_counter(state, "or_confirmed")
        if decision.accepted:
            increment_tournament_counter(state, "decisions_accepted")


def clear_stale_confirmed_candidate_after_entry_attempt(state, decision):
    candidate = getattr(state, "momentum_candidate", None)
    if not candidate or candidate.status != "CONFIRMED":
        return False
    if decision.entry_status not in {"BLOCKED", "OPENED"} and not decision.rejection_reason:
        return False
    state.momentum_candidate = None
    return True


def synthetic_tournament_signal(profile, option_price, *, direction="CALL", signal_label="TEST_PIPELINE"):
    config = profile.config or {}
    contracts = int(config.get("contracts", 1) or 1)
    maximum_cost = float(config.get("maximum_position_cost_dollars", 150.0) or 150.0)
    option_price = min(float(option_price), max(0.01, maximum_cost / (100 * max(1, contracts))))
    option_price = round(option_price, 4)
    bullish_score = 10 if direction == "CALL" else 0
    bearish_score = 10 if direction == "PUT" else 0
    current_price = 501.0 if direction == "CALL" else 489.0
    completed_closes = (500.0, 501.0) if direction == "CALL" else (489.0, 488.0)
    return {
        "price": current_price,
        "bullish_score": bullish_score,
        "bearish_score": bearish_score,
        "confidence": 10,
        "dominance_percent": 100.0,
        "market_state": "BULLISH" if direction == "CALL" else "BEARISH",
        "current_signal": signal_label,
        "ema_state": "BULLISH" if direction == "CALL" else "BEARISH",
        "macd_state": "BULLISH" if direction == "CALL" else "BEARISH",
        "vwap_state": "BULLISH" if direction == "CALL" else "BEARISH",
        "volume_state": "BULLISH" if direction == "CALL" else "BEARISH",
        "levels": {
            "opening_range_high": 499.0,
            "opening_range_low": 490.0,
        },
        "completed_closes": completed_closes,
        "call_option_symbol": "SPY260101C00500000",
        "call_option_bid": option_price,
        "call_option_ask": option_price,
        "call_option_last": option_price,
        "call_option_midpoint": option_price,
        "put_option_symbol": "SPY260101P00500000",
        "put_option_bid": option_price,
        "put_option_ask": option_price,
        "put_option_last": option_price,
        "put_option_midpoint": option_price,
        "option_quote_timestamp": market_now().isoformat(),
    }


def run_profile_pipeline_proof(profile, direction="CALL"):
    profile = copy.deepcopy(profile)
    required_percent = float((profile.config or {}).get("option_momentum_percent", 1.0) or 1.0)
    contracts = int((profile.config or {}).get("contracts", 1) or 1)
    maximum_cost = float((profile.config or {}).get("maximum_position_cost_dollars", 150.0) or 150.0)
    maximum_premium = max(0.01, maximum_cost / (100 * max(1, contracts)))
    start_price = min(1.0, maximum_premium * 0.75)
    confirmation_price = start_price * (1 + ((required_percent + 0.25) / 100))
    start_signal = synthetic_tournament_signal(profile, start_price, direction=direction)
    confirm_signal = synthetic_tournament_signal(profile, confirmation_price, direction=direction)
    start_snapshot = build_market_snapshot_from_signal(start_signal, "SPY", market_now().isoformat())
    confirm_snapshot = build_market_snapshot_from_signal(confirm_signal, "SPY", market_now().isoformat())
    proof_state = copy.deepcopy(create_all_initial_states({profile.profile_id: profile})[profile.profile_id])
    simulated_trades = []
    proof_epoch = 1000.0
    result = {
        "profile_id": profile.profile_id,
        "profile_name": profile.display_name,
        "requested_direction": direction,
        "decision_direction": None,
        "candidate_symbol": None,
        "candidate_type": None,
        "contract_direction_match": None,
        "preliminary_direction": None,
        "candidate_created": False,
        "momentum_confirmed": False,
        "or_confirmed": False,
        "decision_accepted": False,
        "entry_attempted": False,
        "entry_opened": False,
        "would_open": False,
        "simulated_trade": None,
        "status": "NOT_STARTED",
        "block_reason": None,
        "trade_id": None,
    }

    initial_decision = evaluate_profile(profile, proof_state, start_snapshot)
    result["preliminary_direction"] = initial_decision.preliminary_direction or initial_decision.direction
    initial_decisions = {profile.profile_id: initial_decision}
    apply_tournament_momentum({profile.profile_id: profile}, {profile.profile_id: proof_state}, initial_decisions, start_snapshot, proof_epoch)
    result["candidate_created"] = proof_state.momentum_candidate is not None
    if not result["candidate_created"]:
        result["status"] = "BLOCKED"
        result["block_reason"] = initial_decision.entry_block_reason or initial_decision.rejection_reason or initial_decision.momentum_block_reason
        return result

    confirmation_decision = evaluate_profile(profile, proof_state, confirm_snapshot)
    confirmed_decisions = apply_tournament_momentum(
        {profile.profile_id: profile},
        {profile.profile_id: proof_state},
        {profile.profile_id: confirmation_decision},
        confirm_snapshot,
        proof_epoch + 1,
    )
    final_decision = confirmed_decisions[profile.profile_id]
    result["decision_direction"] = final_decision.direction
    result["candidate_symbol"] = final_decision.momentum_candidate_option_symbol
    result["candidate_type"] = option_symbol_direction(final_decision.momentum_candidate_option_symbol)
    result["contract_direction_match"] = option_symbol_matches_direction(final_decision.momentum_candidate_option_symbol, final_decision.direction)
    result["momentum_confirmed"] = bool(final_decision.momentum_passed)
    result["or_confirmed"] = (not final_decision.or_confirmation_required) or bool(final_decision.two_candle_or_passed)
    result["decision_accepted"] = bool(final_decision.accepted)
    result["block_reason"] = final_decision.entry_block_reason or final_decision.rejection_reason or final_decision.momentum_block_reason
    if not final_decision.accepted:
        result["status"] = "BLOCKED"
        return result

    result["entry_attempted"] = True
    entry_snapshot = snapshot_for_confirmed_candidate_entry(confirm_snapshot, proof_state)
    def proof_trade_writer(trade):
        simulated_trades.append(copy.deepcopy(trade))
        return trade

    trade = try_open_virtual_position(
        profile,
        proof_state,
        final_decision,
        entry_snapshot,
        proof_epoch + 1,
        trade_writer=proof_trade_writer,
        is_test_position=True,
        test_type=TEST_TYPE_PIPELINE_PROOF,
    )
    result["entry_opened"] = trade is not None
    result["would_open"] = trade is not None
    result["status"] = "POSITION_OPENED" if trade else "ENTRY_BLOCKED"
    result["block_reason"] = final_decision.entry_block_reason or final_decision.rejection_reason
    result["trade_id"] = trade.trade_id if trade else None
    result["simulated_trade"] = asdict(trade) if trade else None
    proof_state.virtual_position = None
    proof_state.momentum_candidate = None
    return result


def run_tournament_pipeline_proof(config):
    profiles = build_tournament_profiles(config, TOURNAMENT_PROFILES_FILE)
    results = {}
    for profile_id in ("BOT_B_MOMENTUM", "BOT_D_COMBINED"):
        if profile_id not in profiles:
            results[profile_id] = {"profile_id": profile_id, "status": "BLOCKED", "block_reason": "PROFILE_MISSING"}
            continue
        results[profile_id] = {
            "CALL": run_profile_pipeline_proof(profiles[profile_id], "CALL"),
            "PUT": run_profile_pipeline_proof(profiles[profile_id], "PUT"),
        }
    return results


def evaluate_tournament_decisions(config, signal, call_contract=None, put_contract=None):
    if not scanner_market_is_open():
        TOURNAMENT_HEALTH["tournament_entry_loop_alive"] = True
        with BOT_LOCK:
            BOT_STATE["tournament_health"] = dict(TOURNAMENT_HEALTH)
        return None, {}
    profiles = build_tournament_profiles(config)
    signal = add_snapshot_option_fields(signal, call_contract, put_contract)
    snapshot = build_market_snapshot_from_signal(
        signal,
        config.get("symbol", "SPY"),
        market_now().isoformat(),
    )
    with TOURNAMENT_LOCK:
        states = ensure_tournament_runtime_states(profiles)
        daily_reset_changed = apply_daily_resets(profiles, states, time.time())
        snapshot = enrich_snapshot_with_locked_candidate_quotes(snapshot, states)
        counters_before = json.dumps({
            profile_id: tournament_pipeline_counters(state)
            for profile_id, state in states.items()
        }, sort_keys=True)
        decisions = evaluate_all_profiles(profiles, states, snapshot)
        opened_trades = []
        now_epoch = time.time()
        before_candidates = {
            profile_id: state.momentum_candidate
            for profile_id, state in states.items()
        }
        decisions = apply_tournament_momentum(profiles, states, decisions, snapshot, now_epoch)
        record_tournament_pipeline_counters(states, before_candidates, decisions)
        candidate_cleanup_changed = False
        for profile_id, profile in profiles.items():
            if decisions[profile_id].accepted:
                increment_tournament_counter(states[profile_id], "entries_attempted")
            entry_snapshot = snapshot_for_confirmed_candidate_entry(snapshot, states[profile_id])
            trade = try_open_virtual_position(
                profile,
                states[profile_id],
                decisions[profile_id],
                entry_snapshot,
                now_epoch,
            )
            if trade:
                increment_tournament_counter(states[profile_id], "entries_opened")
                opened_trades.append(trade)
                print("TOURNAMENT VIRTUAL ENTRY")
                print("profile:", trade.profile_id)
                print("trade_id:", trade.trade_id)
                print("direction:", trade.direction)
                print("option_symbol:", trade.option_symbol)
                print("entry_price:", trade.entry_price)
                print("contracts:", trade.contracts)
                print("entry_cost:", trade.entry_cost)
            elif decisions[profile_id].entry_block_reason:
                increment_tournament_block_reason(states[profile_id], decisions[profile_id].entry_block_reason)
            if clear_stale_confirmed_candidate_after_entry_attempt(states[profile_id], decisions[profile_id]):
                candidate_cleanup_changed = True
        counters_after = json.dumps({
            profile_id: tournament_pipeline_counters(state)
            for profile_id, state in states.items()
        }, sort_keys=True)
        if opened_trades or daily_reset_changed or candidate_cleanup_changed or counters_after != counters_before:
            save_tournament_state_safe(states)
        state_by_profile = {
            profile_id: {
                "virtual_position": dict(asdict(state.virtual_position), **stop_snapshot(state.virtual_position)) if state.virtual_position else None,
                "momentum_candidate": asdict(state.momentum_candidate) if state.momentum_candidate else None,
                "virtual_trades_today": state.virtual_trades_today,
                "cooldown_remaining_seconds": max(0, int(state.virtual_entry_cooldown_until_epoch - now_epoch)) if state.virtual_entry_cooldown_until_epoch else 0,
                "last_entry_fingerprint": state.last_entry_fingerprint,
                "pipeline_counters": tournament_pipeline_counters(state),
                "candidate_transitions": list(state.candidate_transitions or [])[-20:],
            }
            for profile_id, state in states.items()
        }
        positions = serialize_tournament_positions(states)
        decisions_payload = {
            profile_id: dict(
                asdict(decision),
                **tournament_option_money_snapshot(snapshot, profiles[profile_id], decision),
            )
            for profile_id, decision in decisions.items()
        }
        decisions_evaluated = sum(state.decisions_evaluated for state in states.values())
        decisions_evaluated_by_profile = {
            profile_id: state.decisions_evaluated
            for profile_id, state in states.items()
        }
    with BOT_LOCK:
        BOT_STATE["tournament_last_snapshot"] = asdict(snapshot)
        BOT_STATE["tournament_decisions"] = decisions_payload
        BOT_STATE["tournament_decisions_evaluated"] = decisions_evaluated
        BOT_STATE["tournament_decisions_evaluated_by_profile"] = decisions_evaluated_by_profile
        BOT_STATE["tournament_virtual_positions"] = positions
        BOT_STATE["tournament_state_by_profile"] = state_by_profile
        TOURNAMENT_HEALTH["tournament_entry_loop_alive"] = True
        TOURNAMENT_HEALTH["tournament_last_entry_cycle"] = market_now().isoformat()
        BOT_STATE["tournament_health"] = dict(TOURNAMENT_HEALTH)
    print_tournament_decisions(decisions)
    return snapshot, decisions


def refresh_tournament_state_snapshot(states):
    now_epoch = time.time()
    return {
        profile_id: {
            "virtual_position": dict(
                asdict(state.virtual_position),
                **stop_snapshot(state.virtual_position),
                position_source=tournament_position_source(state.virtual_position),
            ) if state.virtual_position else None,
            "position_source": tournament_position_source(state.virtual_position),
            "momentum_candidate": asdict(state.momentum_candidate) if state.momentum_candidate else None,
            "virtual_trades_today": state.virtual_trades_today,
            "cooldown_remaining_seconds": max(0, int(state.virtual_entry_cooldown_until_epoch - now_epoch)) if state.virtual_entry_cooldown_until_epoch else 0,
            "last_entry_fingerprint": state.last_entry_fingerprint,
            "pipeline_counters": tournament_pipeline_counters(state),
            "candidate_transitions": list(state.candidate_transitions or [])[-20:],
        }
        for profile_id, state in states.items()
    }


def get_unique_tournament_option_symbols(states):
    return sorted({
        state.virtual_position.option_symbol
        for state in states.values()
        if state.virtual_position and state.virtual_position.status == "OPEN" and state.virtual_position.option_symbol
    })


def process_tournament_virtual_exit_poll():
    config = load_config()
    profiles = build_tournament_profiles(config)
    now_iso = market_now().isoformat()
    TOURNAMENT_HEALTH["tournament_last_exit_poll"] = now_iso
    TOURNAMENT_HEALTH["tournament_exit_loop_alive"] = True
    if not scanner_market_is_open():
        with BOT_LOCK:
            BOT_STATE["tournament_health"] = dict(TOURNAMENT_HEALTH)
        return False
    with TOURNAMENT_LOCK:
        states = ensure_tournament_runtime_states(profiles)
        daily_reset_changed = apply_daily_resets(profiles, states, time.time())
        symbols = get_unique_tournament_option_symbols(states)
        if daily_reset_changed:
            save_tournament_state_safe(states)

    if not symbols:
        with BOT_LOCK:
            BOT_STATE["tournament_health"] = dict(TOURNAMENT_HEALTH)
        return

    option_quotes = {
        symbol: get_market_quote(symbol)
        for symbol in symbols
    }
    now_epoch = time.time()
    with TOURNAMENT_LOCK:
        closed_trades = process_virtual_exits(profiles, states, option_quotes, now_epoch)
        save_tournament_state_safe(states)
        positions = serialize_tournament_positions(states)
        state_by_profile = refresh_tournament_state_snapshot(states)

    with BOT_LOCK:
        BOT_STATE["tournament_virtual_positions"] = positions
        BOT_STATE["tournament_state_by_profile"] = state_by_profile
        BOT_STATE["tournament_health"] = dict(TOURNAMENT_HEALTH)

    for trade in closed_trades:
        print("TOURNAMENT VIRTUAL EXIT")
        print("profile:", trade.profile_id)
        print("trade_id:", trade.trade_id)
        print("option_symbol:", trade.option_symbol)
        print("entry_price:", trade.entry_price)
        print("exit_price:", trade.exit_price)
        print("exit_reason:", trade.exit_reason)
        print("pnl_dollars:", trade.pnl_dollars)
        print("pnl_percent:", trade.pnl_percent)
    return True


def tournament_virtual_exit_loop():
    traceback_logged = False
    while not TOURNAMENT_HEALTH.get("stopping"):
        try:
            if not scanner_market_is_open():
                TOURNAMENT_HEALTH["tournament_exit_loop_alive"] = True
                with BOT_LOCK:
                    BOT_STATE["tournament_health"] = dict(TOURNAMENT_HEALTH)
                interval_ms = MARKET_CLOSED_SLEEP_SECONDS * 1000
            else:
                process_tournament_virtual_exit_poll()
                config = load_config()
                interval_ms = max(500, int(config.get("strategy", {}).get("exit_poll_interval_ms", 1000) or 1000))
            traceback_logged = False
        except Exception as error:
            print("TOURNAMENT EXIT ERROR:", error)
            TOURNAMENT_HEALTH["tournament_last_error"] = str(error)
            TOURNAMENT_HEALTH["tournament_exit_loop_alive"] = False
            with BOT_LOCK:
                BOT_STATE["tournament_health"] = dict(TOURNAMENT_HEALTH)
            if not traceback_logged:
                traceback.print_exc()
                traceback_logged = True
            interval_ms = 1000
        time.sleep(interval_ms / 1000)


def tournament_watchdog_loop():
    global TOURNAMENT_EXIT_THREAD
    while not TOURNAMENT_HEALTH.get("stopping"):
        try:
            if TOURNAMENT_EXIT_THREAD is None or not TOURNAMENT_EXIT_THREAD.is_alive():
                TOURNAMENT_HEALTH["tournament_exit_loop_alive"] = False
                start_tournament_exit_monitor()
            if TOURNAMENT_STATE_DIRTY and seconds_since_iso(TOURNAMENT_HEALTH.get("tournament_last_state_save")) not in (None,) and seconds_since_iso(TOURNAMENT_HEALTH.get("tournament_last_state_save")) >= 30:
                profiles = build_tournament_profiles(load_config())
                with TOURNAMENT_LOCK:
                    states = ensure_tournament_runtime_states(profiles)
                    save_tournament_state_safe(states)
            with BOT_LOCK:
                BOT_STATE["tournament_health"] = dict(TOURNAMENT_HEALTH)
        except Exception as error:
            TOURNAMENT_HEALTH["tournament_last_error"] = str(error)
            traceback.print_exc()
        time.sleep(10)


def start_tournament_exit_monitor():
    global TOURNAMENT_EXIT_THREAD
    if TOURNAMENT_EXIT_THREAD and TOURNAMENT_EXIT_THREAD.is_alive():
        return TOURNAMENT_EXIT_THREAD
    TOURNAMENT_EXIT_THREAD = threading.Thread(target=tournament_virtual_exit_loop, daemon=True)
    TOURNAMENT_EXIT_THREAD.start()
    return TOURNAMENT_EXIT_THREAD


def start_tournament_watchdog():
    global TOURNAMENT_WATCHDOG_THREAD
    if TOURNAMENT_WATCHDOG_THREAD and TOURNAMENT_WATCHDOG_THREAD.is_alive():
        return TOURNAMENT_WATCHDOG_THREAD
    TOURNAMENT_WATCHDOG_THREAD = threading.Thread(target=tournament_watchdog_loop, daemon=True)
    TOURNAMENT_WATCHDOG_THREAD.start()
    return TOURNAMENT_WATCHDOG_THREAD


def shutdown_tournament_monitors():
    TOURNAMENT_HEALTH["stopping"] = True
    try:
        profiles = build_tournament_profiles(load_config())
        with TOURNAMENT_LOCK:
            states = ensure_tournament_runtime_states(profiles)
            save_tournament_state_safe(states)
    except Exception as error:
        print("TOURNAMENT SHUTDOWN SAVE ERROR:", error)


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


def entry_funds_check(config, contract, contracts, reference_price):
    """Final gate before a bot buy: estimated cost must fit both the remaining
    bot budget and the broker-reported buying power (fetched fresh)."""
    prices = [p for p in (valid_price(reference_price), valid_price(contract.get("ask"))) if p is not None]
    if not prices:
        return False, "funds check: no option price"
    cost = max(prices) * 100 * int(contracts)
    _, spent_today = sync_trade_limits_from_file(config)
    try:
        account = BROKER.account(resolve_connection(config), force=True)
    except BrokerConnectionError:
        return False, "funds check: broker connection unavailable"
    if not account.get("validated"):
        return False, "funds check: broker balances not verified"
    limit, binding = entry_funds_limit(config.get("bot_budget", 100), spent_today,
                                      account.get("balances", {}).get("entry_buying_power"))
    if limit is None:
        return False, f"funds check: {binding}"
    if cost > limit:
        return False, f"funds check: cost {cost:.2f} > {limit:.2f} allowed by {binding}"
    return True, ""


@broker_session
def execute_entry_buy(config, decision, side, contract, contracts, reference_price, market_context, label="ENTRY"):
    current_config = load_config()
    if (not current_config.get("bot_enabled") or
            config.get("broker_selection_version", 0) != current_config.get("broker_selection_version", 0)):
        add_bot_reason("ENTRY skipped: bot disabled or account selection changed")
        return False
    if not current_config.get("auto_entries_enabled"):
        add_bot_reason(f"{label} skipped: new automatic entries are disabled")
        log_bot_audit("SKIP", decision, config.get("symbol", ""), market_context, config,
                      option_symbol=contract.get("symbol", ""), skip_reason="automatic entries disabled")
        return False
    try:
        if not BROKER.account(resolve_connection(current_config))["validated"]:
            add_bot_reason("ENTRY skipped: selected broker account not validated")
            return False
    except BrokerConnectionError:
        add_bot_reason("ENTRY skipped: broker connection unavailable")
        return False
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

    safety = log_direction_safety_result(f"{label} ORDER SAFETY", decision, market_context)
    if not safety["passed"]:
        add_bot_reason("SIGNAL DIRECTION MISMATCH: order submission cancelled")
        log_bot_audit(
            "SKIP",
            decision,
            config.get("symbol", ""),
            market_context,
            config,
            option_symbol=contract.get("symbol", ""),
            skip_reason=(
                "SIGNAL DIRECTION MISMATCH "
                f"bullish={safety['bullish_score']} bearish={safety['bearish_score']} "
                f"confidence={safety['confidence']} dominance={safety['dominance_percent']:.1f}% "
                f"selected={safety['selected_direction']} winner={safety['winning_direction']}"
            )
        )
        return False

    entry_diagnostics = entry_execution_diagnostics(market_context, contract)
    fresh, freshness_reason = entry_quote_freshness(config, contract, market_context)
    if not fresh:
        add_bot_reason(freshness_reason)
        log_bot_audit("SKIP", decision, config.get("symbol", ""), market_context, config,
                      option_symbol=contract.get("symbol", ""), skip_reason=freshness_reason)
        return False
    funds_ok, funds_reason = entry_funds_check(current_config, contract, contracts, reference_price)
    if not funds_ok:
        add_bot_reason(f"{label} skipped: {funds_reason}")
        log_bot_audit("SKIP", decision, config.get("symbol", ""), market_context, config,
                      option_symbol=contract.get("symbol", ""), skip_reason=funds_reason)
        return False
    ok, order_status, status, text = submit_and_parse_option_order(
        contract["symbol"],
        contracts,
        "buy_to_open",
        label
    )
    order_id = extract_order_id(text)

    if ok:
        order_execution = fetch_broker_order_execution(order_id, text)
        fill_price = order_execution.get("fill_price")
        midpoint = valid_price(entry_diagnostics.get("EntryOptionMidpoint"))
        entry_diagnostics.update({
            "EntryBrokerOrderStatus": order_execution.get("order_status", ""),
            "EntryBrokerFillPrice": fill_price if fill_price is not None else "",
            "EntryBrokerFillTimestamp": order_execution.get("fill_timestamp", ""),
            "EntrySlippageFromMidpoint": fill_price - midpoint if fill_price is not None and midpoint is not None else "",
        })
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
            estimated_entry_price=estimated_entry_price,
            execution_diagnostics=entry_diagnostics
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
        clear_position_exit_state(contract["symbol"], "new bot entry")
        release_closed_exit(contract["symbol"], "new bot entry")
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
    if not config.get("auto_entries_enabled"):
        add_bot_reason("SIGNAL no entry: new automatic entries are disabled")
        log_bot_audit("SKIP", market_context.get("decision", "DO NOTHING"), config.get("symbol", ""), market_context, config, skip_reason="automatic entries disabled")
        return
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
    maximum_position_cost_dollars = float(config.get("maximum_position_cost_dollars", 100))
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
    print("maximum_position_cost_dollars:", maximum_position_cost_dollars)
    print("bot_budget:", bot_budget)
    print("spent_today:", spent_today)

    if real_cost > maximum_position_cost_dollars:
        add_bot_reason(f"BUDGET CHECK skipped {side}: cost {real_cost:.2f} > maximum_position_cost_dollars {maximum_position_cost_dollars:.2f}")
        log_bot_audit("SKIP", decision, config.get("symbol", ""), market_context, config, option_symbol=contract.get("symbol", ""), skip_reason="maximum position cost exceeded")
        return

    if real_cost > bot_budget or spent_today + real_cost > bot_budget:
        add_bot_reason(f"BUDGET CHECK skipped {side}: cost {real_cost:.2f} exceeds budget")
        log_bot_audit("SKIP", decision, config.get("symbol", ""), market_context, config, option_symbol=contract.get("symbol", ""), skip_reason="budget exceeded")
        return

    if config.get("option_momentum_confirmation_enabled", True) or config.get("two_candle_or_confirmation_enabled", True):
        option_symbol = contract.get("symbol", "")
        if equivalent_pending_entry_active(side, option_symbol):
            add_bot_reason(f"PENDING BUY reused: active {side} setup already exists for {option_symbol}")
            log_bot_audit("SKIP", decision, config.get("symbol", ""), market_context, config, option_symbol=option_symbol, skip_reason="equivalent pending entry already active")
            return

        retry_remaining = pending_retry_cooldown_remaining(config, side, option_symbol)
        if retry_remaining > 0:
            add_bot_reason(f"Pending retry blocked: cooldown active, {retry_remaining}s remaining")
            log_bot_audit("SKIP", decision, config.get("symbol", ""), market_context, config, option_symbol=option_symbol, skip_reason=f"pending retry cooldown active {retry_remaining}s remaining")
            return

        confirmation_start_price, confirmation_price_source = get_option_confirmation_price(contract)
        if confirmation_start_price is None:
            add_bot_reason(f"PENDING BUY skipped {side}: no confirmation price")
            log_bot_audit("SKIP", decision, config.get("symbol", ""), market_context, config, option_symbol=option_symbol, skip_reason="no option confirmation price")
            return

        safety = log_direction_safety_result("PENDING ENTRY SAFETY", decision, market_context)
        if not safety["passed"]:
            add_bot_reason("SIGNAL DIRECTION MISMATCH: pending entry cancelled")
            log_bot_audit(
                "SKIP",
                decision,
                config.get("symbol", ""),
                market_context,
                config,
                option_symbol=option_symbol,
                skip_reason=(
                    "SIGNAL DIRECTION MISMATCH "
                    f"bullish={safety['bullish_score']} bearish={safety['bearish_score']} "
                    f"confidence={safety['confidence']} dominance={safety['dominance_percent']:.1f}% "
                    f"selected={safety['selected_direction']} winner={safety['winning_direction']}"
                )
            )
            return

        create_pending_entry(config, decision, side, contract, contracts, confirmation_start_price, market_context, confirmation_price_source)
        return

    execute_entry_buy(config, decision, side, contract, contracts, ask, market_context, label="ENTRY")


@broker_session
def process_pending_entry(config):
    pending = get_pending_entry()
    if not pending.get("active"):
        return False
    if not scanner_market_is_open():
        with BOT_LOCK:
            BOT_STATE["last_action"] = "Market closed — scanner sleeping"
        return False

    decision = pending.get("decision", "DO NOTHING")
    side = pending.get("direction", "")
    option_symbol = pending.get("option_symbol", "")
    contract = pending.get("contract") or {"symbol": option_symbol}
    contracts = int(pending.get("contracts") or config.get("contracts", 1))
    market_context = pending.get("market_context") or current_market_context_snapshot()
    start_price = safe_float(pending.get("starting_option_price"))
    confirmation_price = safe_float(pending.get("confirmation_price"))
    momentum_enabled = bool(config.get("option_momentum_confirmation_enabled", True))
    breakout_enabled = bool(config.get("two_candle_or_confirmation_enabled", True))

    if not config.get("bot_enabled") or not config.get("auto_entries_enabled"):
        why = "bot disabled" if not config.get("bot_enabled") else "automatic entries disabled"
        pending["active"] = False
        pending["status"] = "CANCELLED"
        pending["reason"] = why
        pending["final_cancellation_trigger"] = why
        pending["momentum_status"] = "FAIL"
        refresh_pending_time_remaining(pending)
        set_pending_entry(pending)
        record_shadow_gate_state_from_pending(config, pending)
        upsert_pending_history(pending, final_status="CANCELLED", cancellation_reason=pending["reason"])
        add_bot_reason(f"PENDING BUY cancelled: {why}")
        log_bot_audit("SKIP", decision, config.get("symbol", ""), market_context, config, option_symbol=option_symbol, skip_reason=f"pending entry cancelled: {why}")
        return True

    if not option_market_is_open():
        pending["active"] = False
        pending["status"] = "CANCELLED"
        pending["reason"] = "options market closed"
        pending["final_cancellation_trigger"] = "market closed"
        pending["momentum_status"] = "FAIL"
        refresh_pending_time_remaining(pending)
        set_pending_entry(pending)
        record_shadow_gate_state_from_pending(config, pending)
        upsert_pending_history(pending, final_status="CANCELLED", cancellation_reason=pending["reason"])
        add_bot_reason("PENDING BUY cancelled: options market closed")
        log_bot_audit("SKIP", decision, config.get("symbol", ""), market_context, config, option_symbol=option_symbol, skip_reason="pending entry cancelled: options market closed")
        return True

    quote = get_market_quote(option_symbol)
    current_price, current_price_source = get_option_confirmation_price(quote)
    pending["momentum_timeout_seconds"] = int(pending.get("momentum_timeout_seconds") or config.get("confirmation_timeout_seconds", 60))
    pending["maximum_allowed_drawdown_percent"] = max(0.0, min(50.0, safe_float(
        pending.get("maximum_allowed_drawdown_percent", config.get("pre_confirmation_max_drawdown_percent", 5.0)),
        5.0
    )))
    breakout = update_opening_range_breakout_confirmation(config, side) if breakout_enabled else {
        "status": "PASS",
        "count": 0,
        "required": 0,
        "level": None,
        "reason": "two-candle opening-range confirmation disabled"
    }
    pending = update_pending_confirmation_calculations(pending, current_price, current_price_source, breakout)

    if current_price is None:
        if time.time() >= float(pending.get("expires_epoch") or 0):
            pending["active"] = False
            pending["status"] = "CANCELLED"
            pending["reason"] = "momentum confirmation timeout expired"
            pending["final_cancellation_trigger"] = "momentum confirmation timeout expired"
            if pending.get("momentum_status") != "PASS":
                pending["momentum_status"] = "FAIL"
                pending["momentum_confirmed"] = False
            if pending.get("breakout_status") != "PASS":
                pending["breakout_status"] = "FAIL"
                pending["breakout_confirmed"] = False
            set_pending_entry(pending)
            record_shadow_gate_state_from_pending(config, pending)
            upsert_pending_history(pending, final_status="CANCELLED", cancellation_reason=pending["reason"])
            add_bot_reason("PENDING BUY cancelled: momentum confirmation timeout expired with no option quote")
            log_bot_audit("SKIP", decision, config.get("symbol", ""), market_context, config, option_symbol=option_symbol, current_price=current_price, skip_reason="pending entry cancelled: momentum confirmation timeout expired")
            return True
        pending["status"] = "WAITING FOR MOMENTUM" if pending.get("momentum_status") != "PASS" else "WAITING FOR BREAKOUT"
        pending["reason"] = "option quote unavailable"
        set_pending_entry(pending)
        record_shadow_gate_state_from_pending(config, pending)
        upsert_pending_history(pending, final_status="WAITING")
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
    print("highest_option_price:", pending.get("highest_option_price"))
    print("lowest_option_price:", pending.get("lowest_option_price"))
    print("confirmation_price_source:", pending.get("confirmation_price_source"))
    print("current_momentum_gain_percent:", pending.get("current_momentum_gain_percent"))
    print("current_pre_confirmation_drawdown_percent:", pending.get("current_pre_confirmation_drawdown_percent"))
    print("maximum_allowed_drawdown_percent:", pending.get("maximum_allowed_drawdown_percent"))
    print("breakout_status:", pending.get("breakout_status"))
    print("current_breakout_candle:", pending.get("current_breakout_candle"))
    print("required_breakout_candles:", pending.get("required_breakout_candles"))
    print("breakout_level:", pending.get("breakout_level"))
    print("time_remaining_seconds:", pending.get("time_remaining_seconds"))

    max_drawdown_percent = safe_float(pending.get("maximum_allowed_drawdown_percent"), 5.0)
    if (
        momentum_enabled
        and pending.get("momentum_status") != "PASS"
        and max_drawdown_percent > 0
        and pending.get("current_pre_confirmation_drawdown_percent", 0) >= max_drawdown_percent
    ):
        pending["active"] = False
        pending["status"] = "CANCELLED"
        pending["reason"] = "maximum pre-confirmation drawdown hit"
        pending["final_cancellation_trigger"] = "maximum pre-confirmation drawdown hit"
        pending["momentum_status"] = "FAIL"
        set_pending_entry(pending)
        record_shadow_gate_state_from_pending(config, pending)
        upsert_pending_history(pending, final_status="CANCELLED", cancellation_reason=pending["reason"])
        add_bot_reason(
            f"PENDING BUY cancelled: drawdown {pending.get('current_pre_confirmation_drawdown_percent', 0):.2f}% "
            f">= max {max_drawdown_percent:.2f}%"
        )
        log_bot_audit("SKIP", decision, config.get("symbol", ""), market_context, config, option_symbol=option_symbol, current_price=current_price, skip_reason="pending entry cancelled: maximum pre-confirmation drawdown hit")
        return True

    if not momentum_enabled:
        pending["momentum_status"] = "PASS"
    elif current_price >= confirmation_price:
        pending["momentum_status"] = "PASS"
        print("MOMENTUM CONFIRMED")
        print("Starting Price:", start_price)
        print("Current Price:", current_price)
        print("Gain %:", pending.get("current_momentum_gain_percent"))
        print("Required %:", pending.get("required_confirmation_percent"))
        print("Target Price:", confirmation_price)

    momentum_passed = pending.get("momentum_status") == "PASS"
    pending["momentum_confirmed"] = momentum_passed
    breakout_passed = pending.get("breakout_status") == "PASS"
    pending["breakout_confirmed"] = breakout_passed

    if momentum_passed and breakout_passed:
        pending["status"] = "CONFIRMED"
        pending["reason"] = "option momentum and opening-range breakout confirmed"
        pending["active"] = False
        set_pending_entry(pending)
        record_shadow_gate_state_from_pending(config, pending)
        add_bot_reason(
            f"PENDING BUY confirmed: momentum {current_price:.2f} >= {confirmation_price:.2f}; "
            f"breakout {pending.get('current_breakout_candle')}/{pending.get('required_breakout_candles')}"
        )
        entry_contract = dict(contract)
        if isinstance(quote, dict):
            entry_contract.update(quote)
            entry_contract["symbol"] = option_symbol
        buy_submitted = execute_entry_buy(config, decision, side, entry_contract, contracts, current_price, market_context, label="MOMENTUM ENTRY")
        upsert_pending_history(
            pending,
            final_status="BUY SUBMITTED" if buy_submitted else "CONFIRMED",
            buy_submitted=buy_submitted
        )
        return True

    if time.time() >= float(pending.get("expires_epoch") or 0):
        pending["active"] = False
        pending["status"] = "CANCELLED"
        if momentum_passed and not breakout_passed:
            pending["reason"] = "breakout confirmation failed"
            pending["final_cancellation_trigger"] = "breakout confirmation failed"
        else:
            pending["reason"] = "momentum confirmation timeout expired"
            pending["final_cancellation_trigger"] = "momentum confirmation timeout expired"
        if not momentum_passed:
            pending["momentum_status"] = "FAIL"
            pending["momentum_confirmed"] = False
        if not breakout_passed:
            pending["breakout_status"] = "FAIL"
            pending["breakout_confirmed"] = False
        set_pending_entry(pending)
        record_shadow_gate_state_from_pending(config, pending)
        upsert_pending_history(pending, final_status="CANCELLED", cancellation_reason=pending["reason"])
        add_bot_reason(
            f"PENDING BUY cancelled: {pending['reason']} "
            f"(gain {pending.get('current_momentum_gain_percent', 0):.2f}% / required {safe_float(pending.get('required_confirmation_percent'), 0):.2f}%)"
        )
        log_bot_audit("SKIP", decision, config.get("symbol", ""), market_context, config, option_symbol=option_symbol, current_price=current_price, skip_reason=f"pending entry cancelled: {pending['reason']}")
        return True

    if not momentum_passed:
        pending["status"] = "WAITING FOR MOMENTUM"
        pending["reason"] = "waiting for option price confirmation"
        if pending.get("momentum_status") != "PASS":
            pending["momentum_status"] = "WAITING"
    elif not breakout_passed:
        pending["status"] = "WAITING FOR BREAKOUT"
        pending["reason"] = breakout.get("reason") or "waiting for two completed opening-range breakout candles"
    set_pending_entry(pending)
    record_shadow_gate_state_from_pending(config, pending)
    upsert_pending_history(pending, final_status="WAITING")
    return True


@broker_session
def try_surfer_exit(config, positions, market_context):
    if any(position.get("_broker_selection_version", 0) != load_config().get("broker_selection_version", 0) for position in (positions or [])):
        return False
    if not positions:
        log_bot_audit("DO NOTHING", market_context.get("decision", "DO NOTHING"), config.get("symbol", ""), market_context, config, skip_reason="no open position")
        return False
    if not scanner_market_is_open():
        return False

    for pos in positions:
        if process_pending_exit(pos.get("symbol", ""), positions) != "NONE":
            return True

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
        trigger_diagnostics = dict(market_context.get("exit_trigger_diagnostics") or {})

        exit_reasons = market_context.get("decision_reasons", [])
        reason = "; ".join(exit_reasons)
        print("EXIT REASON:", reason)

        if not option_market_is_open():
            add_bot_reason("EXIT skipped: options market is closed")
            log_bot_audit("SKIP", decision, config.get("symbol", ""), market_context, config, option_symbol=symbol, skip_reason="options market closed", exit_reason=reason)
            return True

        if exit_retry_wait_seconds(symbol) > 0:
            add_bot_reason(f"EXIT waiting before retry after rejected order: {symbol}")
            return True

        submit_tracked_exit(
            symbol, qty, cost_basis, entry_price, "EXIT", "BOT", quote, trigger_diagnostics,
            market_context, reason, exit_reasons, config,
            audit_fields={
                "entry_price": entry_price,
                "cost_basis": cost_basis,
                "current_price": trigger_diagnostics.get("ExitTriggerPrice", ""),
            },
            position_snapshot=pos,
        )
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
    sell_trigger_reason="",
    stop_values=None
):
    stop_values = stop_values or {}
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
        "trailing_drawdown_percent": trailing_drawdown,
        "profit_floor_enabled": stop_values.get("profit_floor_enabled", ""),
        "locked_profit_amount": stop_values.get("locked_profit_amount", ""),
        "locked_profit_dollars": stop_values.get("locked_profit_dollars", ""),
        "required_premium_movement": stop_values.get("required_premium_movement", ""),
        "profit_floor_price": stop_values.get("profit_floor_price", ""),
        "profit_floor_activated": stop_values.get("profit_floor_activated", ""),
        "percentage_trailing_stop": stop_values.get("percentage_trailing_stop", ""),
        "effective_trailing_stop": stop_values.get("effective_trailing_stop", trailing_stop_price),
        "stop_control_rule": stop_values.get("stop_control_rule", "")
    }


@broker_session
def fast_exit_poll(config, positions):
    if any(position.get("_broker_selection_version", 0) != load_config().get("broker_selection_version", 0) for position in (positions or [])):
        return False
    tick_started_at = time.perf_counter()
    set_last_error(None)
    if not positions:
        record_tick_finished(tick_started_at)
        return False
    if not scanner_market_is_open():
        with BOT_LOCK:
            BOT_STATE["thread_alive"] = True
            BOT_STATE["last_action"] = "Market closed — scanner sleeping"
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

    if process_pending_exit(symbol, positions) != "NONE":
        record_tick_finished(tick_started_at)
        return True

    qty = int(float(pos.get("quantity", 1)))
    cost_basis = float(pos.get("cost_basis", 0) or 0)
    entry_price = option_entry_price(cost_basis, qty)
    quote = get_market_quote(symbol)
    hard_stop_percent = float(config["strategy"].get("hard_stop_percent", 20))
    trailing_stop_percent = float(config["strategy"].get("trailing_stop_percent", 15))
    evaluation = evaluate_option_exit(symbol, entry_price, quote, config)

    if not evaluation["ok"]:
        message, exposure = record_exit_quote_error(symbol, qty, entry_price, evaluation, "FAST EXIT")
        reasons = [message, exposure, exit_quote_log_line(symbol, evaluation["exit_quote"], evaluation["stop_price"], "NONE")]
        market_context = current_market_context_snapshot()
        market_context.update({
            "decision": QUOTE_DATA_ERROR_DECISION,
            "decision_reasons": reasons,
            "current_pl": None,
            "distance_to_trailing_stop": None,
            "exit_trigger_diagnostics": {},
        })
        with BOT_LOCK:
            BOT_STATE["market_context"] = dict(market_context)
            BOT_STATE["last_action"] = f"FAST EXIT {QUOTE_DATA_ERROR_DECISION}: {message}"
            BOT_STATE["thread_alive"] = True
        log_bot_audit(
            "SKIP",
            QUOTE_DATA_ERROR_DECISION,
            config.get("symbol", ""),
            market_context,
            config,
            option_symbol=symbol,
            entry_price=entry_price,
            cost_basis=cost_basis,
            reason_log=reasons,
            skip_reason="option quote data error",
            no_sell_reason=f"{message} {exposure}",
        )
        record_tick_finished(tick_started_at)
        return False

    current_price = evaluation["bid"]
    pnl = option_pnl(current_price, cost_basis, qty)
    pnl_percent = ((current_price - entry_price) / entry_price) * 100 if entry_price else 0

    position_key = exit_state_key(symbol, config)
    with BOT_LOCK:
        BOT_STATE["position_max_profit"][position_key] = max(BOT_STATE["position_max_profit"].get(position_key, pnl), pnl)
        BOT_STATE["position_max_drawdown"][position_key] = min(BOT_STATE["position_max_drawdown"].get(position_key, pnl), pnl)
        BOT_STATE["thread_alive"] = True

    hard_stop_price = evaluation["hard_stop_price"]
    stop_values = evaluation["stop_values"]
    peak = stop_values.get("peak_price", entry_price)
    percentage_trailing_stop = stop_values.get("percentage_trailing_stop")
    trailing_stop_price = stop_values.get("effective_trailing_stop")
    trailing_drawdown = ((peak - current_price) / peak) * 100 if peak else 0
    distance_to_trailing_stop = current_price - trailing_stop_price
    stop_armed = bool(stop_values.get("stop_armed"))
    stop_control_rule = stop_values.get("stop_control_rule", "HARD STOP")
    no_sell_reason = ""
    sell_trigger_reason = ""
    decision = "HOLD"
    reasons = [
        "Fast exit poll",
        f"Entry {entry_price:.2f}",
        f"Current {current_price:.2f}",
        f"Peak {peak:.2f}",
        f"Hard stop price {hard_stop_price:.2f}",
        f"Percentage trailing stop {percentage_trailing_stop:.2f}",
        f"Effective trailing stop {trailing_stop_price:.2f}",
        f"Stop control rule {stop_control_rule}",
        f"Drawdown {trailing_drawdown:.2f}%"
    ]

    quote_line = exit_quote_log_line(symbol, evaluation["exit_quote"], evaluation["stop_price"], evaluation["exit_code"])
    if evaluation["exit_code"] == EXIT_CODE_HARD_STOP:
        decision = "SELL"
        sell_trigger_reason = f"Hard stop hit: bid {current_price:.2f} <= hard stop {hard_stop_price:.2f}; P/L at bid {pnl_percent:.2f}%"
        reasons = ["Hard stop hit", f"Bid {current_price:.2f} <= hard stop {hard_stop_price:.2f}", quote_line] + reasons
    elif evaluation["decision"] == "SELL":
        decision = "SELL"
        sell_trigger_reason = f"{evaluation['exit_code']}: bid {current_price:.2f} <= effective stop {trailing_stop_price:.2f} ({stop_control_rule})"
        reasons = ["Trailing stop hit", f"Bid {current_price:.2f} <= effective stop {trailing_stop_price:.2f}", f"Rule: {stop_control_rule}", quote_line] + reasons
    else:
        if stop_armed:
            no_sell_reason = f"Hard stop not hit; trailing stop not hit; stop_armed={stop_armed}; current {current_price:.2f} > effective stop {trailing_stop_price:.2f}; rule={stop_control_rule}"
            reasons = [
                "Trailing stop not hit.",
                f"Current {current_price:.2f} > effective stop {trailing_stop_price:.2f}.",
                f"Rule: {stop_control_rule}."
            ] + reasons
        else:
            no_sell_reason = f"Hard stop not hit; trailing stop inactive; stop_armed={stop_armed}; effective_stop {trailing_stop_price:.2f} <= entry {entry_price:.2f}"
            reasons = [
                "Trailing stop inactive.",
                f"Effective stop {trailing_stop_price:.2f} <= entry {entry_price:.2f}.",
                f"Rule: {stop_control_rule}."
            ] + reasons

    market_context = current_market_context_snapshot()
    trigger_diagnostics = {}
    if decision == "SELL":
        trigger_diagnostics = exit_trigger_diagnostics(
            quote,
            market_context.get("price"),
            hard_stop_price,
            percentage_trailing_stop,
            trailing_stop_price,
        )
    market_context.update({
        "decision": decision,
        "decision_reasons": reasons,
        "current_pl": pnl,
        "distance_to_trailing_stop": distance_to_trailing_stop,
        "exit_trigger_diagnostics": trigger_diagnostics,
    })

    with BOT_LOCK:
        BOT_STATE["market_context"] = dict(market_context)
        BOT_STATE["last_action"] = f"FAST EXIT {decision}: {reasons[0]}"

    print("FAST EXIT POLL")
    print("symbol:", symbol)
    print(quote_line)
    print("entry_price:", entry_price)
    print("current_option_price:", current_price)
    print("highest_option_price_since_entry:", peak)
    print("hard_stop_price:", hard_stop_price)
    print("trailing_stop_percent:", trailing_stop_percent)
    print("profit_floor_enabled:", stop_values.get("profit_floor_enabled"))
    print("locked_profit_dollars:", stop_values.get("locked_profit_dollars"))
    print("required_premium_movement:", stop_values.get("required_premium_movement"))
    print("profit_floor_price:", stop_values.get("profit_floor_price"))
    print("profit_floor_activated:", stop_values.get("profit_floor_activated"))
    print("percentage_trailing_stop:", percentage_trailing_stop)
    print("calculated_stop_price:", trailing_stop_price)
    print("effective_trailing_stop:", trailing_stop_price)
    print("stop_control_rule:", stop_control_rule)
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
                no_sell_reason=no_sell_reason,
                stop_values=stop_values
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
                sell_trigger_reason=sell_trigger_reason,
                stop_values=stop_values
            )
        )
        record_tick_finished(tick_started_at)
        return True

    if exit_retry_wait_seconds(symbol) > 0:
        add_bot_reason(f"FAST EXIT waiting before retry after rejected order: {symbol}")
        record_tick_finished(tick_started_at)
        return True

    submit_tracked_exit(
        symbol, qty, cost_basis, entry_price, "FAST EXIT", "BOT", quote, trigger_diagnostics,
        market_context, reason, reasons, config,
        audit_fields=fast_exit_audit_fields(
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
            sell_trigger_reason=sell_trigger_reason,
            stop_values=stop_values
        ),
        position_snapshot=pos,
    )
    record_tick_finished(tick_started_at)
    return True


@broker_session
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

    if not scanner_market_is_open():
        with BOT_LOCK:
            BOT_STATE["thread_alive"] = True
            BOT_STATE["last_action"] = "Market closed — scanner sleeping"
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
    spy_quote_diagnostics = quote_execution_snapshot(quote)
    signal["spy_quote_timestamp"] = spy_quote_diagnostics.get("quote_timestamp", "")
    signal["spy_quote_age_seconds"] = spy_quote_diagnostics.get("quote_age_seconds", "")
    update_bot_signal_state(signal, call_cost, put_cost)
    try:
        update_shadow_research(signal, call, put, price)
    except Exception as error:
        print("SHADOW RESEARCH ERROR:", error)
    try:
        evaluate_tournament_decisions(config, signal, call, put)
    except Exception as error:
        print("TOURNAMENT ERROR:", error)
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
            if has_unsettled_exits():
                # Runs regardless of market hours or open positions so a fill is never lost.
                process_all_pending_exits(get_position())
            if exit_recovery_block_reason(config):
                # Read-only: reconciles lost exit state; order submission stays blocked until it passes.
                process_exit_recovery(get_position())
            if not scanner_market_is_open():
                idle_started_at = time.perf_counter()
                with BOT_LOCK:
                    BOT_STATE["thread_alive"] = True
                    BOT_STATE["last_action"] = "Market closed — scanner sleeping"
                record_tick_finished(idle_started_at)
                time.sleep(MARKET_CLOSED_SLEEP_SECONDS)
                continue
            positions = get_position()
            reconcile_exit_state_with_broker(positions)
            if positions:
                active_pending = get_pending_entry()
                if active_pending.get("active"):
                    active_pending["active"] = False
                    active_pending["status"] = "CANCELLED"
                    active_pending["reason"] = "broker position exists"
                    active_pending["momentum_status"] = "FAIL"
                    upsert_pending_history(active_pending, final_status="CANCELLED", cancellation_reason=active_pending["reason"])
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
                if has_unsettled_exits():
                    # Exit not yet confirmed: never start a new entry on top of it.
                    time.sleep(exit_poll_interval_ms / 1000)
                elif get_pending_entry().get("active"):
                    process_pending_entry(config)
                    time.sleep(exit_poll_interval_ms / 1000)
                else:
                    surfer_bot_tick()
                    time.sleep(max(1, interval))
        except Exception as exc:
            set_last_error(exc)
            add_bot_reason(f"BOT ERROR {exc}")
            time.sleep(10)


@broker_session
def sell_all_positions():
    recovery_block = exit_recovery_block_reason()
    if recovery_block:
        add_bot_reason(f"SELL BLOCKED: {recovery_block}")
        return False, recovery_block
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
            if get_pending_exit(symbol):
                results.append(f"{symbol}: exit order already pending; no duplicate sent")
                continue
            exit_quote = get_option_exit_price(quote)
            reference_price = exit_quote["price"] or strict_quote_price((quote or {}).get("last"))[0]
            outcome = submit_tracked_exit(
                symbol, qty, cost_basis, entry_price, "SELL", "HUMAN", quote, {},
                current_market_context_snapshot(), "Manual sell", ["Manual sell"], load_config(),
                reference_price=reference_price,
                position_snapshot=pos,
            )
            results.append(f"{symbol}: {outcome}")
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

            r = broker_request("POST",
                "/accounts/{account}/orders",
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
    if not scanner_market_is_open():
        return jsonify({"dates": [], "market_open": False, "status": "MARKET_CLOSED"})
    return jsonify({"dates": get_expirations(symbol)})


@app.route("/api/orders")
def api_orders():
    r = broker_request("GET",
        "/accounts/{account}/orders",
        headers=headers()
    )
    return r.text


@app.route("/api/bot-state")
def api_bot_state():
    config = load_config()
    sync_trade_limits_from_file(config)
    pending_history_limit = history_limit(config, "pending_entry_limit")
    bot_trades_limit = history_limit(config, "bot_trades_limit")
    trade_history_limit = history_limit(config, "trade_history_limit")
    bot_audit_limit = history_limit(config, "bot_audit_limit")
    tournament_health = tournament_health_snapshot()

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
            "pending_entry_history": pending_entry_history_snapshot(pending_history_limit),
            "tournament_decisions": dict(BOT_STATE["tournament_decisions"]),
            "tournament_last_snapshot": dict(BOT_STATE["tournament_last_snapshot"]),
            "tournament_decisions_evaluated": BOT_STATE["tournament_decisions_evaluated"],
            "tournament_decisions_evaluated_by_profile": dict(BOT_STATE["tournament_decisions_evaluated_by_profile"]),
            "tournament_profile_settings": load_tournament_profile_settings(TOURNAMENT_PROFILES_FILE),
            "tournament_virtual_positions": list(BOT_STATE["tournament_virtual_positions"]),
            "tournament_state_by_profile": dict(BOT_STATE["tournament_state_by_profile"]),
            "tournament_health": tournament_health,
            "shadow_research": dict(BOT_STATE.get("shadow_research") or shadow_summary(
                SHADOW_CANDIDATES_FILE,
                today=market_now().date().isoformat(),
                limit=20,
            )),
            "history_limits": {
                "pending_entry_limit": pending_history_limit,
                "bot_trades_limit": bot_trades_limit,
                "trade_history_limit": trade_history_limit,
                "bot_audit_limit": bot_audit_limit
            },
            "config_strategy": dict(config.get("strategy", {}))
        })


@app.route("/api/developer-diagnostics")
def api_developer_diagnostics():
    config = load_config()
    positions = get_position() if scanner_market_is_open() else None
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
    if not scanner_market_is_open():
        return jsonify({"positions": [], "market_open": False, "status": "MARKET_CLOSED"})
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
    if not scanner_market_is_open():
        return jsonify({"quote": None, "market_open": False, "status": "MARKET_CLOSED", "symbol": symbol})
    quote = get_market_quote(symbol)
    return jsonify({"quote": quote, "market_open": True, "symbol": symbol,
                    "market_data_environment": market_data_environment(),
                    "quote_age_seconds": quote_execution_snapshot(quote)["quote_age_seconds"]})


@app.route("/api/market-structure")
def api_market_structure():
    market_open = scanner_market_is_open()
    quote = get_market_quote(load_config()["symbol"]) if market_open else None

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
        "reasons": market_context.get("decision_reasons") or market_context.get("reasons", []),
        "market_open": market_open,
        "status": "OPEN" if market_open else "MARKET_CLOSED"
    })


@app.route("/api/chain")
def api_chain():
    config = load_config()
    symbol = request.args.get("symbol", config["symbol"]).upper()
    expiration = request.args.get("expiration")
    option_type = request.args.get("option_type", "call")

    if not expiration:
        return jsonify({"options": []})

    if not scanner_market_is_open():
        return jsonify({"options": [], "market_open": False, "status": "MARKET_CLOSED"})

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
@broker_session
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
@broker_session
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
@broker_session
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


@app.route("/resolve-unscoped-exit-state", methods=["POST"])
@broker_session
def resolve_unscoped_exit_state_route():
    resolve_unscoped_exit_state(request.form.get("migration_id", ""), request.form.get("action", ""), request.form.get("confirm_symbol", ""))
    return redirect("/")


@app.route("/resolve-exit-intent", methods=["POST"])
def resolve_exit_intent():
    clear_stuck_exit_intent(request.form.get("intent_id", ""), request.form.get("confirm_symbol", ""))
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


@app.route("/clear-pending-entry-history", methods=["POST"])
def clear_pending_entry_history_route():
    clear_pending_entry_history()
    return redirect("/")


@app.route("/restore-pending-entry-history", methods=["POST"])
def restore_pending_entry_history_route():
    restore_pending_entry_history()
    return redirect("/")


@app.route("/clear-bot-audit-view", methods=["POST"])
def clear_bot_audit_view_route():
    clear_bot_audit_view()
    return redirect("/")


@app.route("/restore-bot-audit-view", methods=["POST"])
def restore_bot_audit_view_route():
    restore_bot_audit_view()
    return redirect("/")


@app.route("/api/tournament/settings", methods=["GET"])
def api_tournament_settings():
    return jsonify(tournament_settings_response())


@app.route("/api/tournament/settings", methods=["POST"])
def api_save_tournament_settings():
    try:
        raw_settings = parse_tournament_settings_payload()
        normalized, errors = normalize_profile_settings(raw_settings, strict=False)
        if errors:
            return tournament_api_or_redirect({"ok": False, "errors": errors}, 400)
        saved = save_tournament_profile_settings(normalized, TOURNAMENT_PROFILES_FILE)
        return tournament_api_or_redirect({"ok": True, **tournament_settings_response(saved)})
    except Exception as error:
        return tournament_api_or_redirect({"ok": False, "errors": [str(error)]}, 400)


@app.route("/api/tournament/settings/reset", methods=["POST"])
def api_reset_tournament_settings():
    settings = reset_tournament_profile_settings(TOURNAMENT_PROFILES_FILE)
    return tournament_api_or_redirect({"ok": True, **tournament_settings_response(settings)})


@app.route("/api/tournament/settings/copy", methods=["POST"])
def api_copy_tournament_settings():
    source_profile_id = request.form.get("source_profile_id")
    copy_mode = request.form.get("copy_mode", "SHARED_ONLY")
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        source_profile_id = payload.get("source_profile_id", source_profile_id)
        copy_mode = payload.get("copy_mode", copy_mode)
    try:
        settings = copy_tournament_profile_settings(source_profile_id, copy_mode, TOURNAMENT_PROFILES_FILE)
        return tournament_api_or_redirect({"ok": True, **tournament_settings_response(settings)})
    except Exception as error:
        return tournament_api_or_redirect({"ok": False, "errors": [str(error)]}, 400)


@app.route("/api/tournament/trades", methods=["GET"])
def api_tournament_trades():
    profile_id = request.args.get("profile_id") or None
    status = request.args.get("status") or None
    limit_value = request.args.get("limit")
    limit = None
    if limit_value not in (None, ""):
        try:
            limit = int(limit_value)
        except:
            return jsonify({"ok": False, "errors": ["limit must be an integer"]}), 400
    try:
        trades = list_tournament_trades(profile_id=profile_id, status=status, limit=limit, path=TOURNAMENT_TRADES_FILE)
        return jsonify(tournament_trades_response(trades))
    except Exception as error:
        return jsonify({"ok": False, "errors": [str(error)]}), 400


@app.route("/api/tournament/trades/<trade_id>", methods=["GET"])
def api_tournament_trade_detail(trade_id):
    trade = get_tournament_trade(trade_id, TOURNAMENT_TRADES_FILE)
    if not trade:
        return jsonify({"ok": False, "errors": ["trade not found"]}), 404
    return jsonify({"ok": True, "trade": tournament_trade_to_dict(trade)})


@app.route("/api/tournament/trades/test-record", methods=["POST"])
def api_create_test_tournament_trade():
    payload = request.get_json(silent=True) if request.is_json else request.form
    payload = payload or {}
    try:
        profile_id = payload.get("profile_id")
        with BOT_LOCK:
            latest_decision = dict(BOT_STATE.get("tournament_decisions", {}).get(profile_id, {}))
        direction = latest_decision.get("final_direction") or latest_decision.get("direction")
        if direction not in {"CALL", "PUT"}:
            return jsonify({"ok": False, "errors": ["latest profile decision has no automatic CALL/PUT direction"]}), 400
        entry_price = safe_float(payload.get("entry_price", 1.0), 1.0)
        trade = create_synthetic_tournament_trade(
            profile_id=profile_id,
            direction=direction,
            entry_price=entry_price,
            option_symbol=payload.get("option_symbol") or None,
            symbol=payload.get("symbol") or load_config().get("symbol", "SPY"),
        )
        append_tournament_trade(trade, TOURNAMENT_TRADES_FILE)
        return jsonify({"ok": True, "trade": tournament_trade_to_dict(trade)})
    except Exception as error:
        return jsonify({"ok": False, "errors": [str(error)]}), 400


@app.route("/api/tournament/trades/test-close", methods=["POST"])
def api_close_latest_test_tournament_trade():
    payload = request.get_json(silent=True) if request.is_json else request.form
    payload = payload or {}
    open_test_trades = [
        trade for trade in list_tournament_trades(status="OPEN", path=TOURNAMENT_TRADES_FILE)
        if trade.signal == "TEST_RECORD"
    ]
    if not open_test_trades:
        return jsonify({"ok": False, "errors": ["no open synthetic tournament trade"]}), 404

    trade = open_test_trades[0]
    exit_price = safe_float(payload.get("exit_price", trade.entry_price + 0.01), trade.entry_price + 0.01)
    exit_value = exit_price * 100 * trade.contracts
    pnl_dollars = exit_value - trade.entry_cost
    pnl_percent = (pnl_dollars / trade.entry_cost * 100) if trade.entry_cost else 0
    now_text = market_now().isoformat()
    updated = update_tournament_trade(
        trade.trade_id,
        {
            "status": "CLOSED",
            "exit_time": now_text,
            "exit_epoch": time.time(),
            "exit_price": exit_price,
            "exit_price_source": "SYNTHETIC",
            "exit_value": exit_value,
            "exit_reason": "TEST_RECORD_CLOSE",
            "pnl_dollars": pnl_dollars,
            "pnl_percent": pnl_percent,
            "peak_price": max(trade.peak_price or trade.entry_price, exit_price),
            "lowest_price": min(trade.lowest_price or trade.entry_price, exit_price),
            "max_profit_dollars": max(0, pnl_dollars),
            "max_drawdown_dollars": min(0, pnl_dollars),
            "updated_at": now_text,
        },
        TOURNAMENT_TRADES_FILE,
    )
    return jsonify({"ok": True, "trade": tournament_trade_to_dict(updated)})


@app.route("/api/tournament/pipeline-proof/run", methods=["POST"])
def api_run_tournament_pipeline_proof():
    try:
        results = run_tournament_pipeline_proof(load_config())
        ok = all(
            bool(row.get("would_open"))
            for profile_results in results.values()
            for row in (profile_results.values() if isinstance(profile_results, dict) else [profile_results])
        )
        return jsonify({"ok": ok, "results": results}), 200 if ok else 400
    except Exception as error:
        return jsonify({"ok": False, "errors": [str(error)]}), 400


@app.route("/api/tournament/pipeline-proof/delete", methods=["POST"])
def api_delete_tournament_pipeline_proof_trades():
    try:
        trades = list_tournament_trades(path=TOURNAMENT_TRADES_FILE)
        kept = [trade for trade in trades if trade.signal != "TEST_PIPELINE"]
        deleted = len(trades) - len(kept)
        save_tournament_trades(kept, TOURNAMENT_TRADES_FILE)
        return jsonify({"ok": True, "deleted": deleted})
    except Exception as error:
        return jsonify({"ok": False, "errors": [str(error)]}), 400


@app.route("/api/tournament/positions", methods=["GET"])
def api_tournament_positions():
    with BOT_LOCK:
        positions = list(BOT_STATE.get("tournament_virtual_positions", []))
    return jsonify({
        "ok": True,
        "count": len(positions),
        "positions": positions,
    })


@app.route("/api/tournament/health", methods=["GET"])
def api_tournament_health():
    return jsonify({"ok": True, **tournament_health_snapshot()})


@app.route("/api/tournament/recovery/run", methods=["POST"])
def api_tournament_recovery_run():
    with TOURNAMENT_LOCK:
        summary = initialize_tournament_recovery()
    return jsonify({"ok": True, "summary": summary, "health": tournament_health_snapshot()})


@app.route("/api/tournament/state/save", methods=["POST"])
def api_tournament_state_save():
    config = load_config()
    profiles = build_tournament_profiles(config)
    with TOURNAMENT_LOCK:
        states = ensure_tournament_runtime_states(profiles)
        saved = save_tournament_state_safe(states)
    return jsonify({"ok": saved, "last_save": TOURNAMENT_HEALTH.get("tournament_last_state_save")})


@app.route("/api/tournament/positions/test-price", methods=["POST"])
def api_tournament_position_test_price():
    payload = request.get_json(silent=True) if request.is_json else request.form
    payload = payload or {}
    profile_id = payload.get("profile_id")
    bid_price = safe_float(payload.get("bid_price"), None)
    if not profile_id or not valid_bid(bid_price):
        return jsonify({"ok": False, "errors": ["profile_id and valid bid_price are required"]}), 400

    config = load_config()
    profiles = build_tournament_profiles(config)
    with TOURNAMENT_LOCK:
        states = ensure_tournament_runtime_states(profiles)
        if profile_id not in states or not states[profile_id].virtual_position:
            return jsonify({"ok": False, "errors": ["open virtual position not found"]}), 404

        state = states[profile_id]
        position = update_virtual_position_price(state.virtual_position, bid_price, time.time())
        state.virtual_position = position
        exit_reason = evaluate_virtual_exit(position, bid_price, time.time())
        if exit_reason:
            trade = process_virtual_exits(
                {profile_id: profiles[profile_id]},
                {profile_id: state},
                {position.option_symbol: {"bid": bid_price, "_fetched_at": market_now().isoformat()}},
                time.time(),
            )[0]
            save_tournament_state_safe(states)
            positions = serialize_tournament_positions(states)
            state_by_profile = refresh_tournament_state_snapshot(states)
            with BOT_LOCK:
                BOT_STATE["tournament_virtual_positions"] = positions
                BOT_STATE["tournament_state_by_profile"] = state_by_profile
            return jsonify({"ok": True, "closed": True, "trade": tournament_trade_to_dict(trade)})

        save_tournament_state_safe(states)
        position_payload = dict(asdict(position), **stop_snapshot(position))
        positions = serialize_tournament_positions(states)
        state_by_profile = refresh_tournament_state_snapshot(states)
        with BOT_LOCK:
            BOT_STATE["tournament_virtual_positions"] = positions
            BOT_STATE["tournament_state_by_profile"] = state_by_profile
        return jsonify({"ok": True, "closed": False, "position": position_payload})


@app.route("/save-settings", methods=["POST"])
@broker_session
def save_settings():
    config = load_config()
    previous_environment = config.get("broker_environment", legacy_environment())
    selected_environment = request.form.get("mode", previous_environment)
    if selected_environment not in ("sandbox", "live"):
        return "Select sandbox or live", 400
    switching = selected_environment != previous_environment
    if switching:
        try:
            BROKER.assert_switch_safe(resolve_connection(config))
            resolve_connection(dict(config, broker_environment=selected_environment))
        except BrokerConnectionError as error:
            return str(error), 400
    anomaly_threshold = safe_float(request.form.get("execution_anomaly_threshold_percent", config.get("execution_anomaly_threshold_percent", 15.0)), None)
    if anomaly_threshold is None or not math.isfinite(anomaly_threshold) or anomaly_threshold < 0:
        return "Execution anomaly threshold must be a finite non-negative percentage", 400
    config["execution_anomaly_threshold_percent"] = anomaly_threshold
    freshness_limit = safe_float(request.form.get("max_entry_quote_age_seconds", 30), None)
    if freshness_limit is None or not math.isfinite(freshness_limit) or not 1 <= freshness_limit <= 3600:
        return "Maximum entry quote age must be between 1 and 3600 seconds", 400
    config["require_realtime_entry_quotes"] = request.form.get("require_realtime_entry_quotes") == "on"
    config["allow_delayed_sandbox_entries"] = request.form.get("allow_delayed_sandbox_entries") == "on"
    config["max_entry_quote_age_seconds"] = freshness_limit

    symbol_choice = request.form.get("symbol_choice", "SPY")
    custom_symbol = request.form.get("custom_symbol", "").strip().upper()

    config["symbol"] = custom_symbol if custom_symbol else symbol_choice
    config["mode"] = selected_environment
    config["broker_environment"] = selected_environment
    config["asset_type"] = request.form.get("asset_type", "option")
    config["contracts"] = int(request.form.get("contracts", 1))
    config["bot_enabled"] = request.form.get("bot_enabled") == "on"
    config["auto_entries_enabled"] = request.form.get("auto_entries_enabled") == "on"
    config["strategy_mode"] = request.form.get("strategy_mode", "SURFER")
    config["decision_time"] = request.form.get("decision_time", "09:35")
    config["bot_budget"] = float(request.form.get("bot_budget", 100))
    config["human_daily_trading_budget"] = float(request.form.get("human_daily_trading_budget", 500))
    config["bot_starting_account_balance"] = float(request.form.get("bot_starting_account_balance", 500))
    config["human_starting_account_balance"] = float(request.form.get("human_starting_account_balance", 500))
    config["maximum_position_cost_dollars"] = max(0.0, safe_float(request.form.get("maximum_position_cost_dollars", 100), 100))
    config["max_open_contracts"] = clamp_int(request.form.get("max_open_contracts", 1), 1, 5, 1)
    config["contract_selection_mode"] = request.form.get("contract_selection_mode", "strict_atm")
    config["option_momentum_confirmation_enabled"] = request.form.get("option_momentum_confirmation_enabled") == "on"
    config["option_momentum_percent"] = max(0.1, min(20.0, safe_float(request.form.get("option_momentum_percent", 1.0), 1.0)))
    config["confirmation_timeout_seconds"] = clamp_int(request.form.get("confirmation_timeout_seconds", 60), 1, 300, 60)
    config["pre_confirmation_max_drawdown_percent"] = max(0.0, min(50.0, safe_float(request.form.get("pre_confirmation_max_drawdown_percent", 5.0), 5.0)))
    config["pending_entry_retry_cooldown_seconds"] = clamp_int(request.form.get("pending_entry_retry_cooldown_seconds", 60), 0, 600, 60)
    config["two_candle_or_confirmation_enabled"] = request.form.get("two_candle_or_confirmation_enabled") == "on"
    config["required_breakout_candles"] = clamp_int(request.form.get("required_breakout_candles", 2), 1, 10, 2)
    config["minimum_confidence"] = clamp_int(request.form.get("minimum_confidence", 2), 1, 10, 2)
    config["minimum_dominance_percent"] = clamp_int(request.form.get("minimum_dominance_percent", 60), 50, 100, 60)
    history = config.setdefault("history", {})
    history["use_global_limit"] = request.form.get("use_global_history_limit") == "on"
    history["global_limit"] = clamp_int(request.form.get("global_history_limit", DEFAULT_HISTORY_LIMIT), 1, 500, DEFAULT_HISTORY_LIMIT)
    history["pending_entry_limit"] = clamp_int(request.form.get("pending_entry_history_limit", history["global_limit"]), 1, 500, history["global_limit"])
    history["bot_trades_limit"] = clamp_int(request.form.get("bot_trades_limit", history["global_limit"]), 1, 500, history["global_limit"])
    history["trade_history_limit"] = clamp_int(request.form.get("trade_history_limit", history["global_limit"]), 1, 500, history["global_limit"])
    history["bot_audit_limit"] = clamp_int(request.form.get("bot_audit_limit", history["global_limit"]), 1, 500, history["global_limit"])

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
    s["enable_profit_floor_trailing_stop"] = request.form.get("enable_profit_floor_trailing_stop") == "on"
    s["locked_profit_dollars"] = max(0.0, safe_float(request.form.get("locked_profit_dollars", 0.0), 0.0))
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

    if switching:
        config["bot_enabled"] = False
        config["auto_entries_enabled"] = False
        config["broker_selection_version"] = config.get("broker_selection_version", 0) + 1
        clear_pending_entry("CANCELLED", "broker environment changed")
        with BOT_LOCK:
            BOT_STATE["samples"] = []
            BOT_STATE["market_context"] = {}
            BOT_STATE["historical_levels"] = {}
            BOT_STATE["historical_levels_day"] = None
            BOT_STATE["historical_levels_symbol"] = None
            BOT_STATE["current_signal"] = "NONE"
    elif config.get("bot_enabled"):
        try:
            if not BROKER.account(resolve_connection(config))["validated"]:
                return "Validate the selected account before enabling the bot", 400
        except BrokerConnectionError:
            return "Matching account credentials are required before enabling the bot", 400
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


def fmt_adverse_percent(value):
    if value in (None, ""):
        return "N/A"
    return f"{-abs(safe_float(value)):.2f}%"


def fmt_seconds_or_na(value):
    if value in (None, ""):
        return "N/A"
    return f"{escape_html(value)}s"


def fmt_seconds_value(value):
    if value in (None, ""):
        return "N/A"
    return f"{safe_float(value):.2f}s"


def fmt_signed_money_value(value):
    if value in (None, ""):
        return "N/A"
    number = safe_float(value)
    return f"{number:+.2f}"


def enrich_trade_rows(rows, execution_threshold=None):
    if execution_threshold is None:
        execution_threshold = load_config().get("execution_anomaly_threshold_percent", 15.0)
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
            display = broker_realized_values(display, buy_row)
            display = execution_quality_values(display, execution_threshold)
            display["Entry"] = buy_row.get("Price", "") if buy_row else ""
            if buy_row:
                display["EntryPriceSource"] = buy_row.get("EntryPriceSource", "")
                display["EstimatedEntryPrice"] = buy_row.get("EstimatedEntryPrice", "")
                if not display.get("EntryGrade"):
                    display["EntryGrade"] = buy_row.get("EntryGrade", "")
            display["Exit"] = price
            if display.get("PnLSource") == "BROKER_FILLS":
                display["Entry"] = display["EntryBrokerFillPrice"]
                display["Exit"] = display["ExitBrokerFillPrice"]
                if row.get("PnLSource") != "BROKER_FILLS":
                    exit_grade, score, reason, _ = grade_exit_trade(symbol, row.get("Qty"), display["Exit"], display["PnL"], {
                        "broker_realized_pnl_percent": display["PnLPercent"],
                        "decision": "SELL",
                        "decision_reasons": (row.get("ExitReason") or "").splitlines(),
                    })
                    entry_grade = display.get("EntryGrade")
                    overall = grade_from_score((grade_score(entry_grade) + grade_score(exit_grade)) / 2) if entry_grade else exit_grade
                    display.update(ExitGrade=exit_grade, OverallGrade=overall, BotGrade=overall,
                                   Grade=overall, TradeScore=score, GradeReason=reason)
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
    enriched_rows = enrich_trade_rows(source_rows, config.get("execution_anomaly_threshold_percent", 15.0))
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
        "trade_list": trade_list,
        "execution_performance": execution_performance_summary(enriched_rows)
    }


def get_trade_performance():
    config = load_config()
    rows = get_recent_trades(limit=None)
    return {
        "BOT": summarize_ledger(rows, "BOT", config),
        "HUMAN": summarize_ledger(rows, "HUMAN", config)
    }


def reporting_trade_statistics(values):
    values = [value for value in values if value is not None and math.isfinite(value)]
    winners = [value for value in values if value > 0]
    losers = [value for value in values if value < 0]
    return {
        "Trades": len(values), "Wins": len(winners), "Losses": len(losers),
        "Win Rate": f"{len(winners) / len(values) * 100 if values else 0:.2f}%",
        "Total Realized P/L": fmt_money(sum(values)),
        "Average Winner": fmt_trade_price(sum(winners) / len(winners) if winners else None),
        "Average Loser": fmt_trade_price(sum(losers) / len(losers) if losers else None),
        "Largest Winner": fmt_trade_price(max(winners) if winners else None),
        "Largest Loser": fmt_trade_price(min(losers) if losers else None),
    }


def render_reporting_metrics(metrics):
    return '<div class="kv-grid">' + ''.join(
        f'<span>{escape_html(key)}</span><strong>{escape_html(value) if value != 0 else "0"}</strong>'
        for key, value in metrics.items()) + '</div>'


def render_human_bot_comparison(rows, config):
    cards = []
    for source, title in (("HUMAN", "HUMAN"), ("BOT", "ORIGINAL BOT")):
        completed = [row for row in enrich_trade_rows(rows, config.get("execution_anomaly_threshold_percent", 15))
                     if trade_source(row) == source and row.get("Action") == "SELL"]
        metrics = reporting_trade_statistics([safe_float(row.get("PnL"), None) for row in completed])
        legacy = sum(row.get("PnLSource") != "BROKER_FILLS" for row in completed)
        note = f'Broker fills used where available. Quote / legacy results: {legacy}.' if source == "BOT" else 'Recorded completed human trades.'
        cards.append(f'<article class="performance-panel {source.lower()}"><h3>{title}</h3>{render_reporting_metrics(metrics)}<p>{note}</p></article>')
    return '<div class="performance-grid">' + ''.join(cards) + '</div>'


def render_strategy_research_cards(trades, settings, states):
    names = ("BOT A — BASELINE", "BOT B — MOMENTUM", "BOT C — BREAKOUT", "BOT D — COMBINED")
    cards = []
    for profile_id, title in zip(PROFILE_ORDER, names):
        profile = settings.get(profile_id, {})
        momentum = bool(profile.get("option_momentum_confirmation_enabled"))
        breakout = bool(profile.get("two_candle_or_confirmation_enabled"))
        differences = []
        if momentum:
            differences.append('option momentum confirmation')
        if breakout:
            differences.append('opening-range candle confirmation')
        description = 'Baseline direction strategy' + (' + ' + ' + '.join(differences) if differences else '; no extra confirmation filters')
        profile_trades = [row for row in trades if row.get("profile_id") == profile_id
                          and not row.get("is_test_position") and not row.get("test_type")
                          and not str(row.get("option_symbol", "")).startswith("SPYTEST")
                          and row.get("signal") not in ("TEST_PIPELINE", "TEST_RECORD")
                          and row.get("entry_price_source") != "SYNTHETIC"]
        completed = [row for row in profile_trades if row.get("status") == "CLOSED"]
        metrics = reporting_trade_statistics([safe_float(row.get("pnl_dollars"), None) for row in completed])
        metrics["Net P/L (Virtual)"] = metrics.pop("Total Realized P/L")
        position = (states.get(profile_id) or {}).get("virtual_position") or {}
        if not position:
            position = next((row for row in profile_trades if row.get("status") == "OPEN"), {})
        position_text = 'None'
        if position and position.get("status", "OPEN") == "OPEN":
            if position.get("is_test_position") or position.get("test_type") or str(position.get("option_symbol", "")).startswith("SPYTEST"):
                position_text = 'Test position (excluded from performance)'
            else:
                position_text = f'{position.get("direction", "")} | {position.get("option_symbol", "N/A")} | Qty: {position.get("contracts", "N/A")} | Entry: {fmt_trade_price(position.get("entry_price"))}'
        cards.append(f'''<article class="tournament-decision-card">
<h3>{title}</h3><p><strong>{description}</strong></p>
<p>Momentum: {"ON" if momentum else "OFF"} · Opening-range confirmation: {"ON" if breakout else "OFF"}<br>
Enabled: {"YES" if profile.get("enabled", True) else "NO"}</p>
{render_reporting_metrics(metrics)}
<div class="final-decision-section"><strong>Current Open Position</strong><p>{escape_html(position_text)}</p></div>
</article>''')
    return '<div class="tournament-decision-grid">' + ''.join(cards) + '</div>'


@app.route("/api/strategy-research-display")
def api_strategy_research_display():
    config = load_config()
    settings = load_tournament_profile_settings(TOURNAMENT_PROFILES_FILE)
    trades = [tournament_trade_to_dict(trade) for trade in list_tournament_trades(path=TOURNAMENT_TRADES_FILE)]
    with BOT_LOCK:
        states = dict(BOT_STATE.get("tournament_state_by_profile") or {})
    return jsonify({
        "comparison_html": render_human_bot_comparison(get_recent_trades(limit=None), config),
        "research_html": render_strategy_research_cards(trades, settings, states),
    })


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
Execution Diagnostics<br>
SPY Entry Price: {fmt_trade_price(trade.get("EntrySpyPrice"))}<br>
SPY Quote Timestamp / Age: {escape_html(trade.get("EntrySpyQuoteTimestamp") or "N/A")} / {fmt_seconds_value(trade.get("EntrySpyQuoteAgeSeconds"))}<br>
Option Bid / Ask / Mid / Last: {fmt_trade_price(trade.get("EntryOptionBid"))} / {fmt_trade_price(trade.get("EntryOptionAsk"))} / {fmt_trade_price(trade.get("EntryOptionMidpoint"))} / {fmt_trade_price(trade.get("EntryOptionLast"))}<br>
Option Quote Timestamp / Age: {escape_html(trade.get("EntryOptionQuoteTimestamp") or "N/A")} / {fmt_seconds_value(trade.get("EntryOptionQuoteAgeSeconds"))}<br>
Entry Spread: {fmt_trade_price(trade.get("EntrySpreadDollars"))} ({fmt_percent(trade.get("EntrySpreadPercent"))})<br>
Requested Order: {escape_html(trade.get("EntryRequestedOrderType") or "N/A")} @ {fmt_trade_price(trade.get("EntryRequestedOrderPrice"))}<br>
Broker Fill: {fmt_trade_price(trade.get("EntryBrokerFillPrice"))} at {escape_html(trade.get("EntryBrokerFillTimestamp") or "N/A")} ({escape_html(trade.get("EntryBrokerOrderStatus") or "N/A")})<br>
Entry Slippage vs Midpoint: {fmt_signed_money_value(trade.get("EntrySlippageFromMidpoint"))}<br>
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
Broker Exit Fill: {fmt_trade_price(trade.get("ExitBrokerFillPrice"))} ({escape_html(trade.get("ExitBrokerOrderStatus") or "N/A")})<br>
Broker Fill Timestamp: {escape_html(trade.get("ExitBrokerFillTimestamp") or "N/A")}<br>
Requested Exit Order: {escape_html(trade.get("ExitRequestedOrderType") or "N/A")} @ {fmt_trade_price(trade.get("ExitRequestedOrderPrice"))}<br>
Trigger Bid / Ask / Mid / Last: {fmt_trade_price(trade.get("ExitTriggerOptionBid"))} / {fmt_trade_price(trade.get("ExitTriggerOptionAsk"))} / {fmt_trade_price(trade.get("ExitTriggerOptionMidpoint"))} / {fmt_trade_price(trade.get("ExitTriggerOptionLast"))}<br>
Trigger Quote Timestamp / Age: {escape_html(trade.get("ExitTriggerQuoteTimestamp") or "N/A")} / {fmt_seconds_value(trade.get("ExitTriggerQuoteAgeSeconds"))}<br>
Stop Trigger Price / Source: {fmt_trade_price(trade.get("ExitTriggerPrice"))} / {escape_html(trade.get("ExitStopPriceSource") or "N/A")}<br>
Hard / Trailing / Effective Stop: {fmt_trade_price(trade.get("ExitHardStopPrice"))} / {fmt_trade_price(trade.get("ExitTrailingStopPrice"))} / {fmt_trade_price(trade.get("ExitEffectiveStopPrice"))}<br>
Pre-Exit Bid / Ask / Mid / Last: {fmt_trade_price(trade.get("ExitOptionBid"))} / {fmt_trade_price(trade.get("ExitOptionAsk"))} / {fmt_trade_price(trade.get("ExitOptionMidpoint"))} / {fmt_trade_price(trade.get("ExitOptionLast"))}<br>
Exit Quote Timestamp / Age: {escape_html(trade.get("ExitOptionQuoteTimestamp") or "N/A")} / {fmt_seconds_value(trade.get("ExitOptionQuoteAgeSeconds"))}<br>
Exit Slippage vs Trigger: {fmt_signed_money_value(trade.get("ExitSlippageFromTrigger"))}<br>
SPY Entry / Exit: {fmt_trade_price(trade.get("EntrySpyPrice"))} / {fmt_trade_price(trade.get("ExitSpyPrice"))}<br>
Underlying Movement During Trade: {fmt_signed_money_value(trade.get("SpyChangeDollars"))} ({fmt_percent(trade.get("SpyChangePercent"))})<br>
Peak Price: {fmt_trade_price(trade.get("PeakPrice"))}<br>
Hard Stop Price: {fmt_trade_price(trade.get("HardStopPrice"))}<br>
Trailing Stop Price: {fmt_trade_price(trade.get("TrailingStopPrice"))}<br>
Profit Floor Enabled: {escape_html(trade.get("ProfitFloorEnabled", ""))}<br>
Locked Profit Target: {fmt_money(trade.get("LockedProfitDollars") or trade.get("LockedProfitAmount"))}<br>
Required Premium Movement: {fmt_trade_price(trade.get("RequiredPremiumMovement"))}<br>
Profit Floor Price: {fmt_trade_price(trade.get("ProfitFloorPrice"))}<br>
Profit Floor Activated: {escape_html(trade.get("ProfitFloorActivated", ""))}<br>
Percentage Trailing Stop: {fmt_trade_price(trade.get("PercentageTrailingStop"))}<br>
Effective Trailing Stop: {fmt_trade_price(trade.get("EffectiveTrailingStop"))}<br>
Stop Control Rule: {escape_html(trade.get("StopControlRule", ""))}<br>
Max Drawdown From Peak: {fmt_percent(trade.get("MaxDrawdownFromPeakPercent"))}<br>
PnL: <span class="{pnl_class}">{pnl_display} ({fmt_percent(pnl_percent)})</span><br>
P/L Source: {escape_html(trade.get("PnLSource") or "QUOTE / LEGACY")}<br>
Trigger P/L: {fmt_money(trade.get("TriggerPnL"))} ({fmt_percent(trade.get("TriggerPnLPercent"))})<br>
Quote P/L: {fmt_money(trade.get("QuotePnL"))}<br>
Broker Realized P/L: {fmt_money(trade.get("BrokerRealizedPnL"))} ({fmt_percent(trade.get("BrokerRealizedPnLPercent"))})<br>
Entry Fill vs Ask: {fmt_percent(trade.get("EntryFillVsAskPercent"))}<br>
Entry Fill vs Mid: {fmt_percent(trade.get("EntryFillVsMidPercent"))}<br>
Execution Quality: {escape_html(trade.get("ExecutionQuality") or "UNKNOWN")}<br>
Strategy Performance Eligible: {escape_html(trade.get("StrategyPerformanceEligible") or "N/A")}<br>
Exclusion Reason: {escape_html(trade.get("ExecutionExclusionReason") or "N/A")}<br>
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
Profit Floor Enabled: {escape_html(trade.get("ProfitFloorEnabled", ""))}<br>
Locked Profit Target: {fmt_money(trade.get("LockedProfitDollars") or trade.get("LockedProfitAmount"))}<br>
Required Premium Movement: {fmt_trade_price(trade.get("RequiredPremiumMovement"))}<br>
Profit Floor Price: {fmt_trade_price(trade.get("ProfitFloorPrice"))}<br>
Profit Floor Activated: {escape_html(trade.get("ProfitFloorActivated", ""))}<br>
Percentage Trailing Stop: {fmt_trade_price(trade.get("PercentageTrailingStop"))}<br>
Effective Trailing Stop: {fmt_trade_price(trade.get("EffectiveTrailingStop"))}<br>
Stop Control Rule: {escape_html(trade.get("StopControlRule", ""))}<br>
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


def friendly_pending_status(status):
    status = str(status or "NONE").upper()
    if "WAITING" in status:
        return "Waiting"
    if status in ["CONFIRMED", "BUY SUBMITTED"]:
        return "Confirmed"
    if status == "CANCELLED":
        return "Cancelled"
    return status.title() if status else "None"


def render_current_pending_entry(pending):
    pending = refresh_pending_time_remaining(dict(pending or default_pending_entry()))
    if not pending.get("active"):
        return "No Pending Entry"

    return f"""
<div class="item">
Direction: {escape_html(pending.get("direction") or "N/A")}<br>
Status: {escape_html(friendly_pending_status(pending.get("status")))}<br>
<br>
Current Filter Progress:<br>
Momentum Confirmation: {escape_html(pending.get("momentum_status") or "WAITING")}<br>
Breakout Confirmation: {escape_html(pending.get("breakout_status") or "PASS")}<br>
<br>
Starting Option Price: {fmt_premium(pending.get("starting_option_price"))}<br>
Current Option Price: {fmt_premium(pending.get("current_option_price"))}<br>
Highest Option Price: {fmt_premium(pending.get("highest_option_price"))}<br>
Lowest Option Price: {fmt_premium(pending.get("lowest_option_price"))}<br>
Confirmation Target Price: {fmt_premium(pending.get("confirmation_price"))}<br>
Confirmation Price Source: {escape_html(pending.get("confirmation_price_source") or "N/A")}<br>
Required Momentum %: {fmt_percent(pending.get("required_confirmation_percent"))}<br>
Current Momentum Gain %: {fmt_percent(pending.get("current_momentum_gain_percent"))}<br>
Current Pre-Confirmation Drawdown %: {fmt_percent(pending.get("current_pre_confirmation_drawdown_percent"))}<br>
Maximum Allowed Drawdown %: {fmt_percent(pending.get("maximum_allowed_drawdown_percent"))}<br>
Momentum Confirmed: {"YES" if pending.get("momentum_confirmed") else "NO"}<br>
Breakout Confirmed: {"YES" if pending.get("breakout_confirmed") else "NO"}<br>
Momentum Timeout: {escape_html(pending.get("momentum_timeout_seconds", 0))} sec<br>
Elapsed Time: {escape_html(pending.get("elapsed_time_seconds", 0))} sec<br>
Retry Cooldown Remaining: {escape_html(pending.get("retry_cooldown_remaining_seconds", 0))} sec<br>
Current Breakout Candle: {escape_html(pending.get("current_breakout_candle", 0))}<br>
Required Breakout Candles: {escape_html(pending.get("required_breakout_candles", 0))}<br>
Breakout Level: {fmt_premium(pending.get("breakout_level"))}<br>
Time Remaining: {pending.get("time_remaining_seconds", 0)} sec<br>
Reason: {escape_html(pending.get("reason") or "Waiting for option momentum confirmation")}
</div>
"""


def render_pending_entry_history(history, limit):
    rows = list(history or [])[-limit:]
    if not rows:
        return "No pending entry history."

    html = []
    for row in reversed(rows):
        breakout_progress = f"""{row.get("current_breakout_candle", 0)}/{row.get("required_breakout_candles", 0)}"""
        final_status = row.get("final_status") or row.get("status") or "WAITING"
        buy_line = "<br>BUY SUBMITTED" if row.get("buy_submitted") else ""
        cancel_reason = row.get("cancellation_reason") or ""
        cancel_line = f"""<br>Cancellation Reason: {escape_html(cancel_reason)}""" if cancel_reason else ""
        html.append(f"""
<div class="trade-card bot">
{escape_html(row.get("timestamp", ""))}<br>
{escape_html(row.get("direction", ""))}<br>
Underlying: {escape_html(row.get("underlying", ""))}<br>
Option: {escape_html(row.get("option_symbol", ""))}<br>
Signal Generated: {escape_html(row.get("signal_generated", ""))}<br>
Momentum: {escape_html(row.get("momentum_status", "WAITING"))}<br>
Breakout: {escape_html(row.get("breakout_status", "PASS"))} {escape_html(breakout_progress)}<br>
Starting Option Price: {fmt_premium(row.get("starting_option_price"))}<br>
Current Option Price: {fmt_premium(row.get("current_option_price"))}<br>
Highest Option Price: {fmt_premium(row.get("highest_option_price"))}<br>
Lowest Option Price: {fmt_premium(row.get("lowest_option_price"))}<br>
Confirmation Target Price: {fmt_premium(row.get("confirmation_price"))}<br>
Confirmation Price Source: {escape_html(row.get("confirmation_price_source", "N/A"))}<br>
Current Momentum Gain %: {fmt_percent(row.get("current_momentum_gain_percent"))}<br>
Current Pre-Confirmation Drawdown %: {fmt_percent(row.get("current_pre_confirmation_drawdown_percent"))}<br>
Maximum Allowed Drawdown %: {fmt_percent(row.get("maximum_allowed_drawdown_percent"))}<br>
Momentum Confirmed: {"YES" if row.get("momentum_confirmed") else "NO"}<br>
Breakout Confirmed: {"YES" if row.get("breakout_confirmed") else "NO"}<br>
Momentum Timeout: {escape_html(row.get("momentum_timeout_seconds", 0))} sec<br>
Elapsed Time: {escape_html(row.get("elapsed_time_seconds", 0))} sec<br>
Retry Cooldown Remaining: {escape_html(row.get("retry_cooldown_remaining_seconds", 0))} sec<br>
Breakout Progress: {escape_html(breakout_progress)}<br>
Final Cancellation Trigger: {escape_html(row.get("final_cancellation_trigger", ""))}<br>
Final Status: {escape_html(final_status)}
{buy_line}
{cancel_line}
</div>
""")
    return "".join(html)


def render_shadow_research_panel(summary):
    summary = summary or {}
    rows = list(summary.get("latest_candidates") or [])[:20]
    gate_stats = summary.get("gate_outcome_statistics") or {}
    gate_rows = []
    for outcome in ["WOULD_ENTER", "REJECTED_MOMENTUM", "REJECTED_BREAKOUT", "REJECTED_DRAWDOWN", "REJECTED_TIMEOUT", "REJECTED_COOLDOWN", "WAITING", "N/A"]:
        stats = gate_stats.get(outcome) or {}
        gate_rows.append(
            f"{escape_html(outcome)}: "
            f"{escape_html(stats.get('count', 0))} candidates | "
            f"+5 before -5: {fmt_percent(stats.get('plus_5_before_minus_5_rate'))} | "
            f"Prevented Winners: {escape_html(stats.get('prevented_winner_count', 0))} ({fmt_percent(stats.get('prevented_winner_rate'))}) | "
            f"Prevented Losers: {escape_html(stats.get('prevented_loser_count', 0))} ({fmt_percent(stats.get('prevented_loser_rate'))})"
        )
    latest_html = []
    for row in rows:
        latest_html.append(f"""
<div class="trade-card bot">
{escape_html(row.get("timestamp", ""))}<br>
{escape_html(row.get("direction", ""))} {escape_html(row.get("option_symbol", ""))}<br>
Status: {escape_html(row.get("status", ""))}<br>
Data Quality: {escape_html(row.get("data_quality_status") or "OK")}<br>
Invalid Quotes: {escape_html(row.get("invalid_quote_count") or 0)}<br>
Last Invalid Quote Reason: {escape_html(row.get("last_invalid_quote_reason") or "N/A")}<br>
SPY: {fmt_premium(row.get("spy_price"))}<br>
Start: {fmt_premium(row.get("starting_option_price") or row.get("option_midpoint"))}<br>
Best Move: {fmt_percent(row.get("maximum_favorable_excursion_5m") or row.get("mfe_percent"))} at {fmt_seconds_or_na(row.get("time_to_maximum_favorable_excursion_seconds"))}<br>
Worst Move: {fmt_adverse_percent(row.get("maximum_adverse_excursion_5m") or row.get("mae_percent"))} at {fmt_seconds_or_na(row.get("time_to_maximum_adverse_excursion_seconds"))}<br>
Time to +5%: {fmt_seconds_or_na(row.get("time_to_plus_5_seconds"))}<br>
Time to -5%: {fmt_seconds_or_na(row.get("time_to_minus_5_seconds"))}<br>
+5 Before -5: {escape_html(row.get("hit_plus_5_before_minus_5", ""))}<br>
Class: {escape_html(row.get("classification", ""))}<br>
Momentum Gate: {escape_html(row.get("momentum_confirmation_state") or "N/A")} ({escape_html(row.get("momentum_percentage_observed") or "N/A")} / {escape_html(row.get("momentum_percentage_required") or "N/A")}%)<br>
Breakout Gate: {escape_html(row.get("breakout_confirmation_state") or "N/A")} ({escape_html(row.get("breakout_progress_current_candles") or "N/A")} / {escape_html(row.get("breakout_progress_required_candles") or "N/A")})<br>
Pre-Confirmation Drawdown: {escape_html(row.get("pre_confirmation_max_observed_drawdown_percent") or "N/A")} / {escape_html(row.get("pre_confirmation_configured_max_drawdown_percent") or "N/A")}% | Failed: {escape_html(row.get("pre_confirmation_drawdown_failed") or "N/A")}<br>
Timeout: {escape_html(row.get("confirmation_timed_out") or "N/A")} at {escape_html(row.get("confirmation_timeout_elapsed_seconds") or "N/A")}s<br>
Retry Cooldown: {escape_html(row.get("retry_cooldown_active") or "N/A")} | Remaining: {escape_html(row.get("retry_cooldown_remaining_seconds") or "N/A")}s<br>
Live Gate Outcome: {escape_html(row.get("final_live_gate_outcome") or "N/A")}
</div>
""")
    if not latest_html:
        latest_html.append("No shadow candidates recorded.")

    return f"""
Candidates Today: <span id="shadow-candidates-today">{escape_html(summary.get("candidates_today", 0))}</span><br>
Completed Candidates: <span id="shadow-completed-candidates">{escape_html(summary.get("completed_candidates", 0))}</span><br>
Valid Completed Candidates: <span id="shadow-valid-completed-candidates">{escape_html(summary.get("valid_completed_candidates", 0))}</span><br>
Contaminated Candidates: <span id="shadow-contaminated-candidates">{escape_html(summary.get("contaminated_candidates", 0))}</span><br>
Active Candidates: <span id="shadow-active-candidates">{escape_html(summary.get("active_candidates", 0))}</span><br>
Valid +5 Before -5 Success Rate: <span id="shadow-plus5-rate">{fmt_percent(summary.get("valid_plus_5_before_minus_5_success_rate", summary.get("plus_5_before_minus_5_success_rate")))}</span><br>
Valid Average 5m MFE: <span id="shadow-average-mfe">{fmt_percent(summary.get("valid_average_5m_mfe", summary.get("average_5m_mfe")))}</span><br>
Valid Average 5m MAE: <span id="shadow-average-mae">{fmt_adverse_percent(summary.get("valid_average_5m_mae", summary.get("average_5m_mae")))}</span><br>
<br>
Gate Outcome Research:<br>
{"<br>".join(gate_rows)}<br>
<br>
Latest 20 Candidates:
<div class="history-panel shadow-research-panel" id="shadow-research-latest">
{''.join(latest_html)}
</div>
"""


def load_bot_audit_rows(limit=None):
    try:
        return load_bot_audit_view_rows(limit)
    except:
        return []


def render_bot_audit_history(rows, limit):
    if not rows:
        return "No bot audit records."

    html = []
    for row in reversed(rows[-limit:]):
        html.append(f"""
<div class="trade-card bot">
{escape_html(row.get("timestamp", ""))}<br>
Action: {escape_html(row.get("action", ""))}<br>
Decision: {escape_html(row.get("decision", ""))}<br>
Symbol: {escape_html(row.get("symbol", ""))}<br>
Option: {escape_html(row.get("option_symbol", ""))}<br>
Market State: {escape_html(row.get("market_state", ""))}<br>
Bullish/Bearish/Confidence: {escape_html(row.get("bullish_score", ""))} / {escape_html(row.get("bearish_score", ""))} / {escape_html(row.get("confidence", ""))}<br>
Skip Reason: {escape_html(row.get("skip_reason", ""))}<br>
Exit Reason: {escape_html(row.get("exit_reason", ""))}<br>
Order Status: {escape_html(row.get("order_status", ""))}
</div>
""")
    return "".join(html)


def render_trade_list(summary, limit):
    if not summary["trade_list"]:
        return "No closed trades yet."

    lines = []
    for trade in summary["trade_list"][-limit:]:
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


def render_execution_performance(summary):
    sections = ['<p>All Broker Trades = actual broker results (sandbox paper results when using sandbox).<br>Clean Strategy Trades = strategy evaluation excluding execution anomalies and unknown execution quality. Account P/L is never removed.</p>']
    for key, title in (("all_broker_trades", "ALL BROKER TRADES"), ("clean_strategy_trades", "CLEAN STRATEGY TRADES")):
        values = summary.get(key)
        if values is None:
            continue
        sections.append(f'<h4>{title}</h4>Count: {values["count"]}<br>Wins / Losses: {values["wins"]} / {values["losses"]}<br>Broker Realized P/L: {fmt_money(values["realized_pnl"])}<br>')
        if key == "clean_strategy_trades":
            sections.append(f'Win Rate: {values["win_rate"]:.2f}%<br>Average Winner: {fmt_trade_price(values["average_winner"])}<br>Average Loser: {fmt_trade_price(values["average_loser"])}<br>')
    sections.append(f'Unknown execution quality (excluded from clean stats): {summary.get("unknown_execution_count", 0)}<br>Confirmed broker fills only; legacy quote-only P/L remains in the account panel.<br>')
    return "".join(sections)


def render_performance_panel(label, summary, limit):
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
    trade_list_label = (
        f"""Bot Trades (Last <input type="number" form="settings-form" name="bot_trades_limit" value="{limit}" min="1" max="500" style="width:70px;">)"""
        if label == "BOT"
        else f"""Human Trades (Last {limit})"""
    )
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
{render_execution_performance(summary.get("execution_performance", {})) if label == "BOT" else ""}
{trade_list_label}:<br>
<div class="history-panel bot-trades-panel">
{render_trade_list(summary, limit)}
</div>
</div>
"""


def get_position_pl_data(pos):
    symbol = pos.get("symbol", "")
    qty = float(pos.get("quantity", 0) or 0)
    cost_basis = float(pos.get("cost_basis", 0) or 0)

    quote = get_market_quote(symbol)
    is_option = len(symbol) > 6

    current_price = None
    exit_quote = None
    quote_data_error = ""
    if is_option:
        exit_quote = get_option_exit_price(quote)
        current_price = exit_quote["price"]
        quote_data_error = "" if exit_quote["ok"] else exit_quote["message"]
    elif quote:
        for key in ["last", "bid", "ask"]:
            if quote.get(key) is not None:
                try:
                    current_price = float(quote.get(key))
                    break
                except:
                    pass

    if qty == 0:
        entry_price = 0
    elif is_option:
        entry_price = option_entry_price(cost_basis, qty)
    else:
        entry_price = cost_basis / qty

    if current_price is None and not is_option:
        current_price = entry_price

    if current_price is None:
        # Option with no usable bid: report no value rather than inventing one.
        current_value = None
        pnl = None
        pnl_percent = None
    else:
        current_value = option_current_value(current_price, qty) if is_option else current_price * qty
        pnl = option_pnl(current_price, cost_basis, qty) if is_option else current_value - cost_basis
        pnl_percent = (pnl / cost_basis) * 100 if cost_basis != 0 else 0

    stop_values = stop_debug_values(symbol, entry_price, current_price)
    hold_time = position_hold_time(symbol)
    current_exit_decision, current_exit_reason = current_exit_display(symbol, stop_values, quote_data_error)
    pending_exit = get_pending_exit(symbol)
    closed_exit = get_closed_exit(symbol)
    recovery_block = exit_recovery_block_reason()
    if recovery_block:
        quote_status = recovery_block
    elif pending_exit:
        quote_status = pending_exit.get("status_message", "EXIT ORDER PENDING")
    elif closed_exit:
        quote_status = closed_exit.get("status_message", "EXIT CLOSED: awaiting position confirmation")
    elif quote_data_error:
        quote_status = f"QUOTE DATA ERROR: {quote_data_error}"
    elif exit_quote:
        quote_status = exit_quote_status_text(exit_quote)
    else:
        quote_status = "N/A"

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
        "profit_floor_enabled": stop_values["profit_floor_enabled"],
        "locked_profit_dollars": stop_values["locked_profit_dollars"],
        "locked_profit_amount": stop_values["locked_profit_amount"],
        "required_premium_movement": stop_values["required_premium_movement"],
        "profit_floor_price": stop_values["profit_floor_price"],
        "profit_floor_activated": stop_values["profit_floor_activated"],
        "percentage_trailing_stop": stop_values["percentage_trailing_stop"],
        "effective_trailing_stop": stop_values["effective_trailing_stop"],
        "stop_control_rule": stop_values["stop_control_rule"],
        "drawdown_from_peak_percent": stop_values["drawdown_from_peak_percent"],
        "cost_basis": cost_basis,
        "current_value": current_value,
        "pnl": pnl,
        "pnl_percent": pnl_percent,
        "hold_time": hold_time,
        "current_exit_decision": current_exit_decision,
        "current_exit_reason": current_exit_reason,
        "quote_data_error": quote_data_error,
        "quote_status": quote_status,
        "exit_order_pending": bool(pending_exit),
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
    scope_prefix = exit_order_key(exit_broker_scope(), "")
    with BOT_LOCK:
        internal_symbols = [key for key in BOT_STATE["position_peaks"] if key.startswith(scope_prefix)]
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
        "profit_floor_enabled": False,
        "locked_profit_dollars": None,
        "locked_profit_amount": None,
        "required_premium_movement": None,
        "profit_floor_price": None,
        "profit_floor_activated": False,
        "percentage_trailing_stop": None,
        "effective_trailing_stop": None,
        "stop_control_rule": "HARD STOP",
        "distance_to_stop_percent": None,
        "poll_interval_ms": clamp_int(
            (bot_snapshot.get("config_strategy") or {}).get("exit_poll_interval_ms", 1000),
            100,
            5000,
            1000
        ) if broker_has_position else None,
        "trailing_stop_percent": None,
        "stop_armed": False,
        "quote_data_error": "",
        "quote_status": "N/A",
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
            "profit_floor_enabled": pl.get("profit_floor_enabled"),
            "locked_profit_dollars": pl.get("locked_profit_dollars"),
            "locked_profit_amount": pl.get("locked_profit_amount"),
            "required_premium_movement": pl.get("required_premium_movement"),
            "profit_floor_price": pl.get("profit_floor_price"),
            "profit_floor_activated": pl.get("profit_floor_activated"),
            "percentage_trailing_stop": pl.get("percentage_trailing_stop"),
            "effective_trailing_stop": pl.get("effective_trailing_stop"),
            "stop_control_rule": pl.get("stop_control_rule"),
            "distance_to_stop_percent": (
                ((pl.get("current_price") - pl.get("effective_trailing_stop")) / pl.get("current_price")) * 100
                if pl.get("current_price") and pl.get("effective_trailing_stop") else None
            ),
            "trailing_stop_percent": pl.get("trailing_stop_percent"),
            "stop_armed": bool(pl.get("effective_trailing_stop") and pl.get("entry_price") and pl.get("effective_trailing_stop") >= pl.get("entry_price")),
            "quote_data_error": pl.get("quote_data_error", ""),
            "quote_status": pl.get("quote_status", "N/A"),
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

    market_open = scanner_market_is_open()
    quote = get_market_quote(symbol) if market_open else None
    positions = get_position() if market_open else None
    call = select_atm_contract(symbol, "CALL") if market_open else None
    put = select_atm_contract(symbol, "PUT") if market_open else None
    sync_trade_limits_from_file(config)
    tournament_health = tournament_health_snapshot()

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
            "pending_entry": refresh_pending_time_remaining(dict(BOT_STATE.get("pending_entry") or default_pending_entry())),
            "pending_entry_history": pending_entry_history_snapshot(),
            "tournament_decisions": dict(BOT_STATE["tournament_decisions"]),
            "tournament_last_snapshot": dict(BOT_STATE["tournament_last_snapshot"]),
            "tournament_decisions_evaluated": BOT_STATE["tournament_decisions_evaluated"],
            "tournament_decisions_evaluated_by_profile": dict(BOT_STATE["tournament_decisions_evaluated_by_profile"]),
            "tournament_virtual_positions": list(BOT_STATE["tournament_virtual_positions"]),
            "tournament_state_by_profile": dict(BOT_STATE["tournament_state_by_profile"]),
            "tournament_health": tournament_health,
            "shadow_research": dict(BOT_STATE.get("shadow_research") or shadow_summary(
                SHADOW_CANDIDATES_FILE,
                today=market_now().date().isoformat(),
                limit=20,
            )),
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
    history_settings = normalize_history_config(config)
    pending_history_limit = history_limit(config, "pending_entry_limit")
    trade_history_limit = history_limit(config, "trade_history_limit")
    bot_audit_limit = history_limit(config, "bot_audit_limit")
    bot_trades_limit = history_limit(config, "bot_trades_limit")
    bot_audit_rows = load_bot_audit_rows(bot_audit_limit)
    tournament_trade_rows = [tournament_trade_to_dict(trade) for trade in list_tournament_trades(limit=50, path=TOURNAMENT_TRADES_FILE)]
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

    tournament_settings = load_tournament_profile_settings(TOURNAMENT_PROFILES_FILE)

    def tournament_input(profile_id, row, field, input_type="number", step=None, minimum=None, maximum=None):
        attrs = []
        if step is not None:
            attrs.append(f'step="{step}"')
        if minimum is not None:
            attrs.append(f'min="{minimum}"')
        if maximum is not None:
            attrs.append(f'max="{maximum}"')
        attr_text = " ".join(attrs)
        return (
            f'<input type="{input_type}" name="tournament__{profile_id}__{field}" '
            f'value="{html_lib.escape(str(row.get(field, "")))}" {attr_text}>'
        )

    def tournament_checkbox(profile_id, row, field):
        return f'<input type="checkbox" name="tournament__{profile_id}__{field}" {checked(row.get(field))}>'

    def render_tournament_profile_settings():
        cards = []
        for profile_id in PROFILE_ORDER:
            row = tournament_settings[profile_id]
            mode = row.get("contract_selection_mode", "closest_within_budget")
            cards.append(f"""
<div class="item tournament-profile-card">
<h3>{html_lib.escape(row.get("display_name", profile_id))}</h3>
<input type="hidden" name="tournament__{profile_id}__display_name" value="{html_lib.escape(row.get("display_name", profile_id))}">
Enabled: {tournament_checkbox(profile_id, row, "enabled")}<br>
Minimum Confidence: {tournament_input(profile_id, row, "minimum_confidence", minimum=0)}<br>
Minimum Dominance %: {tournament_input(profile_id, row, "minimum_dominance_percent", step="0.1", minimum=0, maximum=100)}<br>
Minimum Signals: {tournament_input(profile_id, row, "minimum_signals", minimum=1)}<br>
Direction Threshold %: {tournament_input(profile_id, row, "direction_threshold_percent", step="0.1", minimum=0, maximum=100)}<br>
Momentum Enabled: {tournament_checkbox(profile_id, row, "option_momentum_confirmation_enabled")}<br>
Momentum Percent: {tournament_input(profile_id, row, "option_momentum_percent", step="0.1", minimum=0)}<br>
Confirmation Timeout: {tournament_input(profile_id, row, "confirmation_timeout_seconds", minimum=1)}<br>
Pre-Confirmation Max Drawdown %: {tournament_input(profile_id, row, "pre_confirmation_max_drawdown_percent", step="0.1", minimum=0, maximum=100)}<br>
Retry Cooldown Seconds: {tournament_input(profile_id, row, "pending_entry_retry_cooldown_seconds", minimum=0)}<br>
Two-Candle OR Enabled: {tournament_checkbox(profile_id, row, "two_candle_or_confirmation_enabled")}<br>
Required Breakout Candles: {tournament_input(profile_id, row, "required_breakout_candles", minimum=1)}<br>
Max Quote Age Seconds: {tournament_input(profile_id, row, "max_quote_age_seconds", minimum=1)}<br>
Allow Calls: {tournament_checkbox(profile_id, row, "allow_calls")}<br>
Allow Puts: {tournament_checkbox(profile_id, row, "allow_puts")}<br>
Hard Stop %: {tournament_input(profile_id, row, "hard_stop_percent", step="0.1", minimum=0, maximum=100)}<br>
Trailing Stop %: {tournament_input(profile_id, row, "trailing_stop_percent", step="0.1", minimum=0, maximum=100)}<br>
Profit Floor Enabled: {tournament_checkbox(profile_id, row, "enable_profit_floor_trailing_stop")}<br>
Locked Profit ($): {tournament_input(profile_id, row, "locked_profit_dollars", step="0.01", minimum=0)}<br>
Cooldown Minutes: {tournament_input(profile_id, row, "cooldown_minutes", minimum=0)}<br>
Max Trades Per Day: {tournament_input(profile_id, row, "max_trades_per_day", minimum=1)}<br>
Maximum Position Cost ($): {tournament_input(profile_id, row, "maximum_position_cost_dollars", step="0.01", minimum=0)}<br>
Contract Selection Mode:
<select name="tournament__{profile_id}__contract_selection_mode">
<option value="strict_atm" {selected("strict_atm", mode)}>Strict ATM</option>
<option value="closest_within_budget" {selected("closest_within_budget", mode)}>Closest Within Budget</option>
</select><br>
Contracts: {tournament_input(profile_id, row, "contracts", minimum=1)}<br>
</div>
""")
        return "\n".join(cards)

    def render_tournament_health_panel(health):
        health = health or {}
        warnings = health.get("warnings") or []
        warning_html = "<br>".join(escape_html(warning) for warning in warnings) or "None"
        return f"""
<div class="grid" id="tournament-health-grid">
<div class="item"><div class="label">Health</div><div class="value">{escape_html(health.get("overall_status", "UNKNOWN"))}</div></div>
<div class="item"><div class="label">Entry Loop</div><div class="value">{"YES" if health.get("entry_loop_alive") else "NO"}</div></div>
<div class="item"><div class="label">Exit Loop</div><div class="value">{"YES" if health.get("exit_loop_alive") else "NO"}</div></div>
<div class="item"><div class="label">Open Positions</div><div class="value">{health.get("open_position_count", 0)}</div></div>
<div class="item"><div class="label">Last Quote Age</div><div class="value">{health.get("seconds_since_last_successful_quote")}</div></div>
<div class="item"><div class="label">Last Save Age</div><div class="value">{health.get("state_save_age")}</div></div>
<div class="item"><div class="label">Recovery</div><div class="value">{escape_html(health.get("recovery_status", "UNKNOWN"))}</div></div>
<div class="item"><div class="label">Last Error</div><div class="value">{escape_html(health.get("last_error", "None"))}</div></div>
</div>
<div class="item warning">Warnings:<br>{warning_html}</div>
"""

    def render_tournament_decisions_table(decisions):
        rows = []
        decisions = decisions or {}
        for profile_id in PROFILE_ORDER:
            row = decisions.get(profile_id, {})
            settings_row = tournament_settings.get(profile_id, {})
            state = TOURNAMENT_RUNTIME_STATES.get(profile_id)
            evaluated = state.decisions_evaluated if state else 0
            position = state.virtual_position if state else None
            position_source = tournament_position_source(position)
            stops = stop_snapshot(position) if position else {}
            option_premium = row.get("option_premium")
            total_position_cost = row.get("total_position_cost")
            maximum_position_cost = row.get("maximum_position_cost_dollars")
            rows.append(f"""
<tr>
<td>{html_lib.escape(settings_row.get("display_name", profile_id))}</td>
<td>{settings_row.get("enabled", True)}</td>
<td>{html_lib.escape(str(row.get("status", "N/A")))}</td>
<td>{row.get("bullish_score", 0)}</td>
<td>{row.get("bearish_score", 0)}</td>
<td>{html_lib.escape(str(row.get("preliminary_direction") or "NONE"))}</td>
<td>{safe_float(row.get("bullish_dominance_percent")):.1f}% / {safe_float(row.get("bearish_dominance_percent")):.1f}%</td>
<td>{safe_float(row.get("direction_threshold")):.1f}%</td>
<td>{safe_float(row.get("minimum_dominance")):.1f}%</td>
<td>{html_lib.escape(str(row.get("momentum_status", "N/A")))}</td>
<td>{html_lib.escape(str(row.get("call_contract") or ""))}</td>
<td>{html_lib.escape(str(row.get("put_contract") or ""))}</td>
<td>{html_lib.escape(str(row.get("momentum_candidate_direction") or ""))}</td>
<td>{html_lib.escape(str(row.get("momentum_candidate_option_symbol") or ""))}</td>
<td>{html_lib.escape(str(row.get("candidate_contract_type") or ""))}</td>
<td>{html_lib.escape(str(row.get("decision_direction") or row.get("final_direction") or row.get("direction") or ""))}</td>
<td>{html_lib.escape(str(row.get("contract_direction_match") if row.get("contract_direction_match") is not None else ""))} {html_lib.escape(str(row.get("contract_direction_mismatch_reason") or ""))}</td>
<td>{fmt_trade_price(row.get("momentum_starting_price"))}</td>
<td>{fmt_trade_price(row.get("momentum_current_price"))}</td>
<td>{safe_float(row.get("momentum_observed_percent")):.2f}%</td>
<td>{safe_float(row.get("momentum_required_percent")):.2f}%</td>
<td>{safe_float(row.get("momentum_candidate_age_seconds")):.0f}s</td>
<td>{safe_float(row.get("momentum_time_remaining_seconds")):.0f}s</td>
<td>{html_lib.escape(str(row.get("momentum_block_reason") or ""))}</td>
<td>{html_lib.escape(str(row.get("or_confirmation_status", "N/A")))}</td>
<td>{html_lib.escape(str(row.get("final_direction") or row.get("direction") or "NONE"))}</td>
<td>{row.get("accepted", False)}</td>
<td>{html_lib.escape(str(row.get("rejection_reason") or ""))}</td>
<td>{html_lib.escape(str(row.get("entry_status") or ""))}</td>
<td>{html_lib.escape(str(row.get("entry_block_reason") or ""))}</td>
<td>{fmt_trade_price(option_premium)}</td>
<td>{escape_html(row.get("contracts") or settings_row.get("contracts", ""))}</td>
<td>{fmt_money(total_position_cost) if total_position_cost not in (None, "") else ""}</td>
<td>{fmt_money(maximum_position_cost) if maximum_position_cost not in (None, "") else ""}</td>
<td>{evaluated}</td>
<td>{escape_html(position.status if position else "NONE")}</td>
<td>{escape_html(position_source)}</td>
<td>{escape_html(position.option_symbol if position else "")}</td>
<td>{fmt_trade_price(position.entry_price if position else "")}</td>
<td>{fmt_trade_price(position.current_price if position else "")}</td>
<td>{fmt_money(position.unrealized_pnl_dollars) if position else ""}</td>
<td>{fmt_percent(position.unrealized_pnl_percent) if position else ""}</td>
<td>{fmt_trade_price(position.peak_price if position else "")}</td>
<td>{fmt_trade_price(stops.get("hard_stop_price")) if position else ""}</td>
<td>{fmt_trade_price(stops.get("trailing_stop_price")) if position else ""}</td>
<td>{fmt_trade_price(stops.get("profit_floor_price")) if position else ""}</td>
</tr>
""")
        return "".join(rows)

    def render_tournament_decision_cards(decisions):
        decisions = decisions or {}
        cards = []
        for profile_id in PROFILE_ORDER:
            row = decisions.get(profile_id, {})
            settings_row = tournament_settings.get(profile_id, {})
            state = TOURNAMENT_RUNTIME_STATES.get(profile_id)
            counters = tournament_pipeline_counters(state) if state else {}
            block_reasons = counters.get("entry_block_reasons") or {}
            block_reason_rows = "".join(
                f'<div class="debug-row"><span>{escape_html(reason)}</span><strong>{escape_html(count)}</strong></div>'
                for reason, count in block_reasons.items()
            ) or '<div class="debug-row"><span>Entry Block Reasons</span><strong>None</strong></div>'
            counter_rows = "".join(
                f'<div class="debug-row"><span>{escape_html(key.replace("_", " ").title())}</span><strong>{escape_html(value)}</strong></div>'
                for key, value in counters.items()
                if key != "entry_block_reasons"
            )
            dominant_side = row.get("preliminary_direction") or "NONE"
            final_direction = row.get("final_direction") or row.get("direction") or "NONE"
            accepted = bool(row.get("accepted"))
            accepted_class = "good" if accepted else "bad"
            rejection_reason = row.get("rejection_reason") or "None"
            entry_block_reason = row.get("entry_block_reason") or "None"
            position = state.virtual_position if state else None
            position_source = tournament_position_source(position)
            or_required = bool(row.get("or_confirmation_required"))
            or_progress = (
                f'{"PASS" if row.get("two_candle_or_passed") else "WAITING"} / '
                f'{escape_html(settings_row.get("required_breakout_candles", 2))} candles required'
                if or_required else "Not required"
            )
            cards.append(f"""
<article class="tournament-decision-card" data-profile-id="{escape_html(profile_id)}">
<div class="tournament-card-header">
<div><div class="profile-kicker">{escape_html(profile_id)}</div><h3>{escape_html(settings_row.get("display_name", profile_id))}</h3></div>
<div class="status-badges"><span class="status-pill">{"Enabled" if settings_row.get("enabled", True) else "Disabled"}</span><span class="status-pill">{escape_html(row.get("status", "N/A"))}</span></div>
</div>
<section class="decision-highlight">
<div><span>Final Direction</span><strong>{escape_html(final_direction)}</strong></div>
<div><span>Accepted</span><strong class="{accepted_class}">{escape_html(accepted)}</strong></div>
<div><span>Entry Status</span><strong>{escape_html(row.get("entry_status") or "N/A")}</strong></div>
<div class="wide"><span>Entry Block Reason</span><strong>{escape_html(entry_block_reason)}</strong></div>
<div class="wide"><span>Rejection Reason</span><strong>{escape_html(rejection_reason)}</strong></div>
</section>
<div class="tournament-section-grid">
<section class="tournament-section"><h4>Signal</h4>
<div class="kv-grid"><span>Bullish Score</span><strong>{escape_html(row.get("bullish_score", 0))}</strong><span>Bearish Score</span><strong>{escape_html(row.get("bearish_score", 0))}</strong><span>Confidence</span><strong>{escape_html(row.get("confidence", 0))}</strong><span>Dominant Side</span><strong>{escape_html(dominant_side)}</strong><span>Dominance</span><strong>{safe_float(row.get("dominance_percent")):.1f}%</strong><span>Direction Threshold</span><strong>{safe_float(row.get("direction_threshold")):.1f}%</strong><span>Minimum Dominance</span><strong>{safe_float(row.get("minimum_dominance")):.1f}%</strong></div>
</section>
<section class="tournament-section"><h4>Contract</h4>
<div class="kv-grid"><span>Call Contract</span><strong>{escape_html(row.get("call_contract") or "N/A")}</strong><span>Put Contract</span><strong>{escape_html(row.get("put_contract") or "N/A")}</strong><span>Candidate Direction</span><strong>{escape_html(row.get("momentum_candidate_direction") or "N/A")}</strong><span>Candidate Option</span><strong>{escape_html(row.get("momentum_candidate_option_symbol") or "N/A")}</strong><span>Direction Match</span><strong>{escape_html(row.get("contract_direction_match") if row.get("contract_direction_match") is not None else "N/A")}</strong><span>Starting Price</span><strong>{fmt_trade_price(row.get("momentum_starting_price"))}</strong><span>Current Price</span><strong>{fmt_trade_price(row.get("momentum_current_price"))}</strong><span>Option Premium</span><strong>{fmt_trade_price(row.get("option_premium"))}</strong><span>Contracts</span><strong>{escape_html(row.get("contracts") or settings_row.get("contracts", "N/A"))}</strong><span>Total Position Cost</span><strong>{fmt_money(row.get("total_position_cost"))}</strong><span>Maximum Position Cost</span><strong>{fmt_money(row.get("maximum_position_cost_dollars"))}</strong></div>
</section>
<section class="tournament-section"><h4>Momentum</h4>
<div class="kv-grid"><span>Result</span><strong>{escape_html(row.get("momentum_status", "N/A"))}</strong><span>Movement Observed</span><strong>{safe_float(row.get("momentum_observed_percent")):.2f}%</strong><span>Movement Required</span><strong>{safe_float(row.get("momentum_required_percent")):.2f}%</strong><span>Candidate Age</span><strong>{safe_float(row.get("momentum_candidate_age_seconds")):.0f}s</strong><span>Time Remaining</span><strong>{safe_float(row.get("momentum_time_remaining_seconds")):.0f}s</strong><span>Block Reason</span><strong>{escape_html(row.get("momentum_block_reason") or "None")}</strong></div>
</section>
<section class="tournament-section"><h4>Opening Range</h4>
<div class="kv-grid"><span>OR Result</span><strong>{escape_html(row.get("or_confirmation_status", "N/A"))}</strong><span>Required</span><strong>{escape_html(or_required)}</strong><span>Progress</span><strong>{or_progress}</strong></div>
</section>
</div>
<section class="final-decision-section"><h4>Final Decision</h4><div class="final-decision-grid"><div><span>Direction</span><strong>{escape_html(final_direction)}</strong></div><div><span>Accepted</span><strong class="{accepted_class}">{escape_html(accepted)}</strong></div><div><span>Entry Status</span><strong>{escape_html(row.get("entry_status") or "N/A")}</strong></div><div><span>Entry Block Reason</span><strong>{escape_html(entry_block_reason)}</strong></div><div><span>Rejection Reason</span><strong>{escape_html(rejection_reason)}</strong></div></div></section>
<details class="pipeline-debug"><summary>Show Advanced Pipeline Debug</summary><div class="debug-grid">{counter_rows}{block_reason_rows}<div class="debug-row"><span>Decisions Evaluated</span><strong>{escape_html(state.decisions_evaluated if state else 0)}</strong></div><div class="debug-row"><span>Position Status</span><strong>{escape_html(position.status if position else "NONE")}</strong></div><div class="debug-row"><span>Position Source</span><strong>{escape_html(position_source)}</strong></div><div class="debug-row"><span>Position Option</span><strong>{escape_html(position.option_symbol if position else "N/A")}</strong></div></div></details>
</article>
""")
        return "".join(cards)

    def render_tournament_candidate_transition_cards(state_by_profile):
        rows = []
        for profile_id in ["BOT_B_MOMENTUM", "BOT_D_COMBINED"]:
            state_row = (state_by_profile or {}).get(profile_id, {})
            rows.extend(state_row.get("candidate_transitions") or [])
        rows.sort(key=lambda row: safe_float(row.get("time")), reverse=True)
        if not rows:
            return "No candidate transitions yet."
        cards = []
        for row in rows[:20]:
            cards.append(f"""
<article class="candidate-transition-card">
<div class="transition-header"><strong>{escape_html(row.get("profile_name") or row.get("profile_id") or "")}</strong><span>{escape_html(row.get("time") or "")}</span><span class="status-pill">{escape_html(row.get("status") or "N/A")}</span></div>
<div class="transition-grid"><span>Direction</span><strong>{escape_html(row.get("old_direction") or "NONE")} &rarr; {escape_html(row.get("new_direction") or "NONE")}</strong><span>Option</span><strong>{escape_html(row.get("old_option") or "N/A")} &rarr; {escape_html(row.get("new_option") or "N/A")}</strong><span>Candidate Age</span><strong>{safe_float(row.get("candidate_age")):.1f}s</strong><span>Reason</span><strong>{escape_html(row.get("reason") or "None")}</strong></div>
</article>
""")
        return "".join(cards)

    def render_tournament_trades_table_rows(trades):
        if not trades:
            return '<tr><td colspan="14">No tournament trades yet.</td></tr>'
        rows = []
        for trade in trades:
            pnl = trade.get("pnl_dollars")
            pnl_percent = trade.get("pnl_percent")
            pnl_text = "" if pnl in (None, "") else fmt_money(pnl)
            if pnl is not None and safe_float(pnl) > 0:
                pnl_text = "+" + pnl_text
            percent_text = "" if pnl_percent in (None, "") else f"{safe_float(pnl_percent):+.2f}%"
            profile_label = f"""
<strong>{escape_html(trade.get("profile_display_name"))}</strong><br>
<small>{escape_html(trade.get("profile_id"))}</small>
"""
            rows.append(f"""
<tr>
<td>{escape_html(trade.get("entry_time"))}</td>
<td title="{escape_html(trade.get("profile_id"))}">{profile_label}</td>
<td>{escape_html(trade.get("direction"))}</td>
<td>{escape_html(trade.get("option_symbol"))}</td>
<td>{escape_html(trade.get("contracts"))}</td>
<td>{fmt_trade_price(trade.get("entry_price"))}</td>
<td>{"" if trade.get("exit_price") in (None, "") else fmt_trade_price(trade.get("exit_price"))}</td>
<td>{escape_html(trade.get("status"))}</td>
<td>{escape_html(trade.get("exit_reason") or "")}</td>
<td>{pnl_text}</td>
<td>{percent_text}</td>
<td>{escape_html(trade.get("confidence"))}</td>
<td>{safe_float(trade.get("dominance_percent")):.2f}%</td>
<td>{escape_html(trade.get("market_state"))}</td>
</tr>
""")
        return "".join(rows)

    html = f"""
<html>
<head>
<title>Trading Bot Dashboard</title>
<style>
:root {{
    --history-panel-height: 400px;
}}
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
.history-panel {{
    max-height: var(--history-panel-height);
    overflow-y: auto;
    overscroll-behavior: contain;
    padding-right: 8px;
}}
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
.tournament-profile-card input[type="number"],
.tournament-profile-card select {{ width: 140px; }}
.decision-table {{
    width: 100%;
    border-collapse: collapse;
}}
.decision-table th,
.decision-table td {{
    border-bottom: 1px solid #444;
    padding: 6px;
    text-align: left;
}}
.tournament-decision-grid {{
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 16px;
}}
.tournament-decision-card {{
    min-width: 0;
    background: #181a1b;
    border: 1px solid #4b4f52;
    border-top: 4px solid #00b7ff;
    border-radius: 10px;
    padding: 16px;
}}
.tournament-card-header,
.transition-header {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
}}
.tournament-card-header h3 {{ margin: 3px 0 0; font-size: 20px; }}
.profile-kicker {{ color: #8f969b; font-size: 11px; letter-spacing: .08em; }}
.status-badges {{ display: flex; gap: 6px; flex-wrap: wrap; }}
.status-pill {{
    display: inline-block;
    border: 1px solid #596066;
    background: #25282a;
    border-radius: 999px;
    padding: 4px 8px;
    font-size: 12px;
}}
.decision-highlight,
.final-decision-grid {{
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 8px;
}}
.decision-highlight {{ margin: 14px 0; }}
.decision-highlight > div,
.final-decision-grid > div {{ background: #242729; border-radius: 7px; padding: 9px; min-width: 0; }}
.decision-highlight .wide {{ grid-column: span 3; }}
.decision-highlight span,
.final-decision-grid span {{ display: block; color: #9ca3a8; font-size: 12px; margin-bottom: 4px; }}
.decision-highlight strong,
.final-decision-grid strong {{ overflow-wrap: anywhere; }}
.tournament-section-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }}
.tournament-section {{ background: #202325; border-radius: 8px; padding: 11px; min-width: 0; }}
.tournament-section h4,
.final-decision-section h4 {{ margin: 0 0 9px; color: #00d994; }}
.kv-grid {{ display: grid; grid-template-columns: minmax(125px, .8fr) minmax(0, 1.2fr); gap: 6px 10px; }}
.kv-grid span {{ color: #a9afb3; }}
.kv-grid strong {{ overflow-wrap: anywhere; }}
.final-decision-section {{ margin-top: 12px; padding: 12px; border: 1px solid #00b7ff; border-radius: 8px; background: #16242a; }}
.pipeline-debug {{ margin-top: 12px; background: #202325; border-radius: 8px; padding: 10px; }}
.pipeline-debug summary {{ cursor: pointer; font-weight: bold; color: #ffd166; }}
.debug-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px 14px; margin-top: 10px; }}
.debug-row {{ display: flex; justify-content: space-between; gap: 12px; border-bottom: 1px solid #383c3f; padding: 5px 0; min-width: 0; }}
.debug-row span {{ color: #a9afb3; overflow-wrap: anywhere; }}
.debug-row strong {{ text-align: right; overflow-wrap: anywhere; }}
.candidate-transition-list {{ display: grid; gap: 10px; }}
.candidate-transition-card {{ background: #1b1b1b; border-left: 4px solid #ffd166; border-radius: 8px; padding: 12px; }}
.transition-header {{ margin-bottom: 9px; }}
.transition-header > span:not(.status-pill) {{ color: #a9afb3; }}
.transition-grid {{ display: grid; grid-template-columns: 105px minmax(0, 1fr); gap: 6px 10px; }}
.transition-grid span {{ color: #a9afb3; }}
.transition-grid strong {{ overflow-wrap: anywhere; }}
.compact-raw-table {{ margin-top: 14px; }}
.compact-raw-table summary {{ cursor: pointer; font-weight: bold; }}
.compact-raw-table .history-panel {{ margin-top: 10px; }}
@media (max-width: 1050px) {{
    .tournament-decision-grid {{ grid-template-columns: 1fr; }}
}}
@media (max-width: 700px) {{
    body {{ margin: 12px; }}
    .tournament-section-grid,
    .decision-highlight,
    .final-decision-grid,
    .debug-grid {{ grid-template-columns: 1fr; }}
    .decision-highlight .wide {{ grid-column: span 1; }}
}}
</style>
</head>
<body>

<h1>Trading Bot Dashboard</h1>
<section class="card"><h2>Connected Broker Account</h2>
<div id="connected-account">Loading verified broker balances…</div></section>

<section class="card">
<h2>HUMAN VS BOT</h2>
<p>Completed trades · Original BOT includes execution anomalies in actual results.</p>
<div id="human-bot-comparison">{render_human_bot_comparison(get_recent_trades(limit=None), config)}</div>
</section>
<section class="card">
<h2>STRATEGY RESEARCH — SIMULATED</h2>
<p>A/B/C/D are virtual experiments, not broker fills. Each uses the baseline direction strategy with the confirmation filters shown below. Saved profile settings are shown; thresholds and risk settings can differ. Test trades are excluded. Results cover all recorded completed trades, not just the latest 50.</p>
<div id="strategy-research-cards">{render_strategy_research_cards([tournament_trade_to_dict(trade) for trade in list_tournament_trades(path=TOURNAMENT_TRADES_FILE)], tournament_settings, bot_snapshot.get("tournament_state_by_profile") or {})}</div>
</section>

<div class="card">
<h2>Status</h2>
Bot: <span class="{ "good" if bot_enabled else "bad" }">{ "ENABLED" if bot_enabled else "DISABLED" }</span><br>
Broker Environment: <span id="broker-environment-label">UNKNOWN / awaiting account validation</span><br>
New Automatic Entries: <span class="{ "good" if config.get("auto_entries_enabled") else "bad" }">{ "ENABLED" if config.get("auto_entries_enabled") else "DISABLED" }</span><br>
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
<details><summary><strong>Advanced Debug — Tournament Health</strong></summary>
<h2>Tournament Health</h2>
{render_tournament_health_panel(bot_snapshot.get("tournament_health"))}
</details>
</div>

<div class="card">
<details><summary><strong>Advanced Debug — Tournament Decisions &amp; Pipeline Counters</strong></summary>
<h2>Tournament Decisions</h2>
<div class="tournament-decision-grid" id="tournament-decision-cards">
{render_tournament_decision_cards(bot_snapshot.get("tournament_decisions"))}
</div>
<details class="compact-raw-table">
<summary>Compact Raw Table</summary>
<div class="history-panel">
<table class="decision-table">
<thead>
<tr>
<th>Profile</th>
<th>Enabled</th>
<th>Status</th>
<th>Bullish Score</th>
<th>Bearish Score</th>
<th>Dominant Side</th>
<th>Dominance %</th>
<th>Direction Threshold</th>
<th>Minimum Dominance</th>
<th>Momentum Result</th>
<th>Call Contract</th>
<th>Put Contract</th>
<th>Candidate Direction</th>
<th>Candidate Option</th>
<th>Candidate Type</th>
<th>Decision Direction</th>
<th>Contract Direction Match</th>
<th>Starting Price</th>
<th>Current Price</th>
<th>Movement Observed</th>
<th>Movement Required</th>
<th>Candidate Age</th>
<th>Time Remaining</th>
<th>Momentum Block Reason</th>
<th>OR Result</th>
<th>Final Direction</th>
<th>Accepted</th>
<th>Rejection Reason</th>
<th>Entry Status</th>
<th>Entry Block Reason</th>
<th>Option Premium</th>
<th>Contracts</th>
<th>Total Position Cost</th>
<th>Maximum Position Cost</th>
<th>Decisions Evaluated</th>
<th>Pipeline Counters</th>
<th>Position Status</th>
<th>Current Position Source</th>
<th>Option</th>
<th>Entry Price</th>
<th>Current Bid</th>
<th>Unrealized P/L</th>
<th>Unrealized P/L %</th>
<th>Peak</th>
<th>Hard Stop</th>
<th>Trailing Stop</th>
<th>Profit Floor</th>
</tr>
</thead>
<tbody id="tournament-decisions-raw-body">
{render_tournament_decisions_table(bot_snapshot.get("tournament_decisions"))}
</tbody>
</table>
</div>
</details>
</details>
</div>

<div class="card">
<details><summary><strong>Advanced Debug — Momentum Candidate Transitions</strong></summary>
<h2>Bot B / Bot D Momentum Candidate Transitions (Last 20)</h2>
<div class="history-panel candidate-transition-list" id="tournament-candidate-transitions">
{render_tournament_candidate_transition_cards(bot_snapshot.get("tournament_state_by_profile"))}
</div>
</details>
</div>

<div class="card">
<h2>TOURNAMENT TRADES — SIMULATED (not broker orders)</h2>
<details class="item">
<summary><strong>DEVELOPER TEST TOOLS</strong></summary>
<br>
<label>Test Profile:</label>
<select id="tournament-test-profile">
<option value="BOT_A_BASELINE">Bot A Baseline</option>
<option value="BOT_B_MOMENTUM">Bot B Momentum</option>
<option value="BOT_C_TWO_CANDLE_OR">Bot C Two-Candle OR</option>
<option value="BOT_D_COMBINED">Bot D Combined</option>
</select>
<button type="button" onclick="createTestTournamentTrade()">Create Test Tournament Trade</button>
<button type="button" class="yellow" onclick="closeLatestTestTournamentTrade()">Close Latest Test Trade</button>
<button type="button" onclick="runTournamentPipelineProof()">Run Bot B/D Pipeline Proof</button>
<button type="button" class="red" onclick="deleteTournamentPipelineProofTrades()">Delete Pipeline Proof Trades</button>
<br><small>Developer-only synthetic records. Direction is derived from the latest automatic ProfileDecision. These records are not proof that automatic direction selection works.</small>
<pre id="tournament-proof-result" style="white-space:pre-wrap;"></pre>
</details>
<br>
<div class="history-panel tournament-trades-panel">
<table class="decision-table">
<thead>
<tr>
<th>Time</th>
<th>Profile</th>
<th>Direction</th>
<th>Option</th>
<th>Contracts</th>
<th>Entry</th>
<th>Exit</th>
<th>Status</th>
<th>Exit Reason</th>
<th>P/L $</th>
<th>P/L %</th>
<th>Confidence</th>
<th>Dominance</th>
<th>Market State</th>
</tr>
</thead>
<tbody id="tournament-trades-body">
{render_tournament_trades_table_rows(tournament_trade_rows)}
</tbody>
</table>
</div>
</div>

<div class="card">
<h2>MANUAL BROKER TOOLS</h2>
<p>Manual broker tools do not control tournament direction.</p>
<h3>Manual Contract Picker</h3>

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
<div class="item"><div class="label">Market Data Environment</div><div class="value" id="quote-environment">{market_data_environment()}</div></div>
<div class="item"><div class="label">Broker Quote Age (seconds)</div><div class="value" id="quote-broker-age">{fmt_seconds_value(quote_execution_snapshot(quote)["quote_age_seconds"])}</div></div>
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
            option_premium = get_option_trade_price(opt)
            option_total_cost = option_position_cost(option_premium, config.get("contracts", 1)) if option_premium is not None else None

            html += f"""
<div class="item">
<div class="label">{title}</div>
<div class="value">{opt.get("symbol")}</div>
Strike: {opt.get("strike")}<br>
Exp: {opt.get("expiration")}<br>
Bid: {fmt_money(opt.get("bid"))}<br>
Ask: {fmt_money(opt.get("ask"))}<br>
Last: {fmt_money(opt.get("last"))}<br>
Option Premium: {fmt_money(option_premium)}<br>
Contracts: {config.get("contracts", 1)}<br>
Total Position Cost: {fmt_money(option_total_cost)}<br>
Maximum Position Cost: {fmt_money(config.get("maximum_position_cost_dollars", 0))}<br>
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

{render_pending_exit_panel()}<div class="card">
<h2>Exit Monitor</h2>
<div class="grid">
<div class="item"><div class="label">Entry</div><div class="value" id="exit-entry">{fmt_money(bot_health["entry_price"])}</div></div>
<div class="item"><div class="label">Current (Bid)</div><div class="value" id="exit-current">{fmt_money(bot_health["current_price"])}</div></div>
<div class="item"><div class="label">Exit Quote</div><div class="value" id="exit-quote-status">{escape_html(bot_health["quote_status"])}</div></div>
<div class="item"><div class="label">Peak</div><div class="value" id="exit-peak">{fmt_money(bot_health["peak_price"])}</div></div>
<div class="item"><div class="label">Trailing Stop</div><div class="value" id="exit-trailing-stop">{fmt_money(bot_health["trailing_stop_price"])}</div></div>
<div class="item"><div class="label">Profit Floor Enabled</div><div class="value" id="exit-profit-floor-enabled">{ "YES" if bot_health["profit_floor_enabled"] else "NO" }</div></div>
<div class="item"><div class="label">Locked Profit Target</div><div class="value" id="exit-locked-profit-amount">{fmt_money(bot_health["locked_profit_dollars"])}</div></div>
<div class="item"><div class="label">Required Premium Movement</div><div class="value" id="exit-required-premium-movement">{fmt_money(bot_health["required_premium_movement"])}</div></div>
<div class="item"><div class="label">Profit Floor Price</div><div class="value" id="exit-profit-floor-price">{fmt_money(bot_health["profit_floor_price"])}</div></div>
<div class="item"><div class="label">Profit Floor Activated</div><div class="value" id="exit-profit-floor-activated">{ "YES" if bot_health["profit_floor_activated"] else "NO" }</div></div>
<div class="item"><div class="label">Percentage Stop</div><div class="value" id="exit-percentage-trailing-stop">{fmt_money(bot_health["percentage_trailing_stop"])}</div></div>
<div class="item"><div class="label">Effective Stop</div><div class="value" id="exit-effective-trailing-stop">{fmt_money(bot_health["effective_trailing_stop"])}</div></div>
<div class="item"><div class="label">Stop Rule</div><div class="value" id="exit-stop-control-rule">{escape_html(bot_health["stop_control_rule"])}</div></div>
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
            pnl_color = "#00ff88" if (pl["pnl"] or 0) >= 0 else "#ff5555"
            html += f"""
<div class="item">
<div class="label">Symbol</div>
<div class="value">{pl["symbol"]}</div>
Qty: {pl["qty"]}<br>
Entry: {fmt_money(pl["entry_price"])}<br>
Current (Bid): {fmt_money(pl["current_price"])}<br>
Exit Quote: {escape_html(pl.get("quote_status") or "N/A")}<br>
Hard Stop %: {pl["hard_stop_percent"]:.2f}%<br>
Hard Stop Price: {fmt_money(pl["hard_stop_price"])}<br>
Peak Price: {fmt_money(pl["peak_price"])}<br>
Trailing Stop %: {pl["trailing_stop_percent"]:.2f}%<br>
Profit Floor Enabled: {"YES" if pl.get("profit_floor_enabled") else "NO"}<br>
Locked Profit Target: {fmt_money(pl.get("locked_profit_dollars"))}<br>
Required Premium Movement: {fmt_money(pl.get("required_premium_movement"))}<br>
Profit Floor Price: {fmt_money(pl.get("profit_floor_price"))}<br>
Profit Floor Activated: {"YES" if pl.get("profit_floor_activated") else "NO"}<br>
Percentage Trailing Stop: {fmt_money(pl.get("percentage_trailing_stop"))}<br>
Effective Trailing Stop: {fmt_money(pl.get("effective_trailing_stop"))}<br>
Stop Control Rule: {escape_html(pl.get("stop_control_rule"))}<br>
Trailing Stop Price: {fmt_money(pl["trailing_stop_price"])}<br>
Drawdown From Peak %: {pl["drawdown_from_peak_percent"]:.2f}%<br>
<span style="color:{pnl_color}; font-weight:bold;">
P/L: {fmt_money(pl["pnl"])}<br>
P/L %: {f"{pl['pnl_percent']:+.2f}%" if pl["pnl_percent"] is not None else "N/A"}
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
<h2>Current Decision</h2>
Market State: {escape_html(bot_snapshot["market_state"])}<br>
Bullish Score: {bot_snapshot["bullish_score"]} / 10<br>
Bearish Score: {bot_snapshot["bearish_score"]} / 10<br>
Confidence: {bot_snapshot["confidence"]} / 10<br>
Dominance: {float(bot_snapshot.get("dominance_percent") or 0):.1f}%<br>
Current Signal: {escape_html(bot_snapshot["current_signal"])}<br>
Last Bot Action: {escape_html(bot_snapshot["last_action"])}<br>
<br>
Bot Reason Log:<br>
<div class="item">{reason_log_html}</div>
</div>

<div class="card">
<h2>Shadow Research</h2>
<button type="button" id="shadow-refresh-toggle" onclick="toggleShadowAutoRefresh()">Pause Shadow Auto-Refresh</button>
<span style="margin-left:10px;">Last Shadow UI Update: <span id="shadow-last-ui-update">N/A</span></span>
<br><br>
<div id="shadow-research-content">
{render_shadow_research_panel(bot_snapshot.get("shadow_research"))}
</div>
</div>

<div class="card">
<h2>Pending Entry (LIVE)</h2>
<div id="pending-entry-live-content">
{render_current_pending_entry(bot_snapshot.get("pending_entry"))}
</div>
</div>

<div class="card">
<h2>Pending Entry History (Last <input type="number" form="settings-form" name="pending_entry_history_limit" value="{pending_history_limit}" min="1" max="500" style="width:70px;">)</h2>
<form method="POST" action="/clear-pending-entry-history" style="display:inline;">
<button type="submit" class="red">Clear Pending History</button>
</form>
<form method="POST" action="/restore-pending-entry-history" style="display:inline;">
<button type="submit" class="yellow">Undo Clear Pending History</button>
</form>
<br><br>
<div class="history-panel pending-entry-history-panel" id="pending-entry-history-content">
{render_pending_entry_history(bot_snapshot.get("pending_entry_history"), pending_history_limit)}
</div>
</div>
"""

    html += f"""
<div class="card">
<h2>ORIGINAL BOT &amp; HUMAN — Account Details and Trade History</h2>
<div class="performance-grid">
{render_performance_panel("HUMAN", trade_performance["HUMAN"], bot_trades_limit)}
{render_performance_panel("BOT", trade_performance["BOT"], bot_trades_limit)}
</div>
</div>
"""

    visible_trades = enrich_trade_rows(get_trade_history_trades(limit=trade_history_limit))
    html += """
<div class="card">
"""
    html += f"""
<h2>Trade History (Last <input type="number" form="settings-form" name="trade_history_limit" value="{trade_history_limit}" min="1" max="500" style="width:70px;">)</h2>
"""
    html += """
<form method="POST" action="/clear-trade-history-view" style="display:inline;">
<button type="submit" class="red">Clear Trade History</button>
</form>
<form method="POST" action="/restore-trade-history-view" style="display:inline;">
<button type="submit" class="yellow">Undo Clear Trade History</button>
</form>
<br><br>
<div class="history-panel trade-history-panel">
"""

    if visible_trades:
        for trade in reversed(visible_trades):
            html += render_recent_trade_card(trade)
    else:
        html += "No trades visible."

    html += """
</div>
</div>
"""

    html += f"""
<div class="card">
<h2>Bot Audit (Last <input type="number" form="settings-form" name="bot_audit_limit" value="{bot_audit_limit}" min="1" max="500" style="width:70px;">)</h2>
<form method="POST" action="/clear-bot-audit-view" style="display:inline;">
<button type="submit" class="red">Clear Bot Audit</button>
</form>
<form method="POST" action="/restore-bot-audit-view" style="display:inline;">
<button type="submit" class="yellow">Undo Clear Bot Audit</button>
</form>
<br><br>
<div class="history-panel bot-audit-panel">
{render_bot_audit_history(bot_audit_rows, bot_audit_limit)}
</div>
</div>
"""

    html += f"""
<div class="card">
<h2>Settings</h2>

<form method="POST" action="/save-settings" id="settings-form">

<h3>General</h3>
<h4>Market Data / Entry Freshness</h4>
Execution Anomaly Threshold (%):
<input type="number" name="execution_anomaly_threshold_percent" min="0" step="0.1" value="{config.get("execution_anomaly_threshold_percent", 15.0)}"><br>
Reporting only; does not block orders.<br>
Environment: <strong>{market_data_environment()}</strong><br>
Sandbox quotes are delayed approximately 15 minutes. Quote ages below are broker data ages.<br>
Require fresh quotes for original BOT entries:
<input type="checkbox" name="require_realtime_entry_quotes" {checked(config.get("require_realtime_entry_quotes", False))}><br>
Maximum Entry Quote Age (seconds):
<input type="number" name="max_entry_quote_age_seconds" min="1" max="3600" value="{config.get("max_entry_quote_age_seconds", 30)}"><br>
Allow delayed sandbox entries for research (explicit override):
<input type="checkbox" name="allow_delayed_sandbox_entries" {checked(config.get("allow_delayed_sandbox_entries", False))}><br>

Bot Enabled (runs exit management for open positions):
<input type="checkbox" name="bot_enabled" {checked(bot_enabled)}><br>

Allow New Automatic Entries (opens new positions with real money in LIVE):
<input type="checkbox" name="auto_entries_enabled" {checked(config.get("auto_entries_enabled", False))}><br>

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

Maximum Position Cost ($):
<input type="number" step="0.01" min="0" name="maximum_position_cost_dollars" value="{config.get("maximum_position_cost_dollars", 100)}"><br>

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

Broker Environment (matching credentials required):
<select name="mode">
<option value="sandbox" {selected("sandbox", mode)}>Sandbox</option>
<option value="live" {selected("live", mode)}>Live</option>
</select><br>
<small>Switching disables auto-entry. After account validation, explicitly enable the bot and save again. Switching requires no open broker positions or pending orders. Tournament balances remain SIMULATED.</small><br>

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

<h3>History Panels</h3>

Use Global History Limit:
<input type="checkbox" name="use_global_history_limit" {checked(history_settings.get("use_global_limit", True))}><br>

Global History Limit:
<input type="number" min="1" max="500" name="global_history_limit" value="{history_settings.get("global_limit", DEFAULT_HISTORY_LIMIT)}"><br>

If global limit is enabled, Pending Entry History, Bot Trades, Trade History, and Bot Audit use this same value. Disable it to use the per-panel Last inputs above.<br>

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

<div style="border:1px solid #666; padding:10px; margin:10px 0;">
<h3>Option Momentum Confirmation</h3>

Enable Option Momentum Confirmation:
<input type="checkbox" name="option_momentum_confirmation_enabled" {checked(config.get("option_momentum_confirmation_enabled", True))}><br>

Confirmation Percent:
<input type="number" step="0.1" min="0.1" max="20" name="option_momentum_percent" value="{config.get("option_momentum_percent", 1.0)}"><br>

Confirmation Timeout Seconds:
<input type="number" min="1" max="300" name="confirmation_timeout_seconds" value="{config.get("confirmation_timeout_seconds", 60)}"><br>

Pre-Confirmation Max Drawdown %:
<input type="number" step="0.1" min="0" max="50" name="pre_confirmation_max_drawdown_percent" value="{config.get("pre_confirmation_max_drawdown_percent", 5.0)}"><br>

Pending Entry Retry Cooldown Seconds:
<input type="number" min="0" max="600" name="pending_entry_retry_cooldown_seconds" value="{config.get("pending_entry_retry_cooldown_seconds", 60)}"><br>

Enable Two-Candle OR Confirmation:
<input type="checkbox" name="two_candle_or_confirmation_enabled" {checked(config.get("two_candle_or_confirmation_enabled", True))}><br>

Required Breakout Candles:
<input type="number" min="1" max="10" name="required_breakout_candles" value="{config.get("required_breakout_candles", 2)}"><br>
</div>

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

<h3>Risk</h3>

Hard Stop %:
<input type="number" step="0.1" name="hard_stop_percent" value="{s.get("hard_stop_percent")}"><br>

Trailing Stop %:
<input type="number" step="0.1" name="trailing_stop_percent" value="{s.get("trailing_stop_percent")}"><br>

Profit Floor Trailing Stop:
<input type="checkbox" name="enable_profit_floor_trailing_stop" {checked(s.get("enable_profit_floor_trailing_stop", True))}><br>

Locked Profit ($):
<input type="number" step="0.01" min="0" name="locked_profit_dollars" value="{s.get("locked_profit_dollars", 0.0)}"><br>

<h3>Opening Direction</h3>

Exit Poll Interval (ms):
<input type="number" min="100" max="5000" step="50" name="exit_poll_interval_ms" value="{s.get("exit_poll_interval_ms", 1000)}"><br>

Direction Threshold %:
<input type="number" step="0.1" name="direction_threshold_percent" value="{s.get("direction_threshold_percent")}"><br>

Bot Scan Every Seconds:
<input type="number" name="interval_seconds" value="{config.get("scanner", {}).get("interval_seconds", 60)}"><br><br>

<h2>TOURNAMENT PROFILE SETTINGS</h2>
<p>These settings are separate from the original live bot settings. Tournament profiles are decision-only and do not submit Tradier orders.</p>
<div class="grid">
{render_tournament_profile_settings()}
</div>
<br>
Copy Mode:
<select name="copy_mode">
<option value="SHARED_ONLY">SHARED_ONLY - preserve experiment flags</option>
<option value="EVERYTHING">EVERYTHING - copy all settings</option>
</select><br>
<button type="submit" formaction="/api/tournament/settings" formmethod="POST">Save Tournament Settings</button>
<button type="submit" formaction="/api/tournament/settings/reset" formmethod="POST" class="yellow">Reset Tournament Profiles to Defaults</button><br>
<button type="submit" formaction="/api/tournament/settings/copy" formmethod="POST" name="source_profile_id" value="BOT_A_BASELINE">Copy Bot A Settings to All</button>
<button type="submit" formaction="/api/tournament/settings/copy" formmethod="POST" name="source_profile_id" value="BOT_B_MOMENTUM">Copy Bot B Settings to All</button>
<button type="submit" formaction="/api/tournament/settings/copy" formmethod="POST" name="source_profile_id" value="BOT_C_TWO_CANDLE_OR">Copy Bot C Settings to All</button>
<button type="submit" formaction="/api/tournament/settings/copy" formmethod="POST" name="source_profile_id" value="BOT_D_COMBINED">Copy Bot D Settings to All</button>
<br><br>

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
Current (Bid): ${{fmtMoney(pl.current_price)}}<br>
Exit Quote: ${{escapeHtml(pl.quote_status || "N/A")}}<br>
Hard Stop %: ${{Number(pl.hard_stop_percent || 0).toFixed(2)}}%<br>
Hard Stop Price: ${{fmtMoney(pl.hard_stop_price)}}<br>
Peak Price: ${{fmtMoney(pl.peak_price)}}<br>
Trailing Stop %: ${{Number(pl.trailing_stop_percent || 0).toFixed(2)}}%<br>
Profit Floor Enabled: ${{pl.profit_floor_enabled ? "YES" : "NO"}}<br>
Locked Profit Target: ${{fmtMoney(pl.locked_profit_dollars)}}<br>
Required Premium Movement: ${{fmtMoney(pl.required_premium_movement)}}<br>
Profit Floor Price: ${{fmtMoney(pl.profit_floor_price)}}<br>
Profit Floor Activated: ${{pl.profit_floor_activated ? "YES" : "NO"}}<br>
Percentage Trailing Stop: ${{fmtMoney(pl.percentage_trailing_stop)}}<br>
Effective Trailing Stop: ${{fmtMoney(pl.effective_trailing_stop)}}<br>
Stop Control Rule: ${{escapeHtml(pl.stop_control_rule || "HARD STOP")}}<br>
Trailing Stop Price: ${{fmtMoney(pl.trailing_stop_price)}}<br>
Drawdown From Peak %: ${{Number(pl.drawdown_from_peak_percent || 0).toFixed(2)}}%<br>
<span style="color:${{pnlColor}}; font-weight:bold;">
P/L: ${{fmtMoney(pl.pnl)}}<br>
P/L %: ${{pl.pnl_percent === null || pl.pnl_percent === undefined ? "N/A" : Number(pl.pnl_percent).toFixed(2) + "%"}}
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

function escapeHtml(value) {{
    return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}}

function friendlyPendingStatus(status) {{
    const text = String(status || "NONE").toUpperCase();
    if (text.includes("WAITING")) return "Waiting";
    if (text === "CONFIRMED" || text === "BUY SUBMITTED") return "Confirmed";
    if (text === "CANCELLED") return "Cancelled";
    return text;
}}

function renderPendingEntry(pending) {{
    const el = document.getElementById("pending-entry-live-content");
    if (!el) return;

    pending = pending || {{}};
    if (!pending.active) {{
        el.innerHTML = "No Pending Entry";
        return;
    }}

    el.innerHTML = `
<div class="item">
Direction: ${{escapeHtml(pending.direction || "N/A")}}<br>
Status: ${{escapeHtml(friendlyPendingStatus(pending.status))}}<br>
<br>
Current Filter Progress:<br>
Momentum Confirmation: ${{escapeHtml(pending.momentum_status || "WAITING")}}<br>
Breakout Confirmation: ${{escapeHtml(pending.breakout_status || "PASS")}}<br>
<br>
Starting Option Price: ${{fmtMoney(pending.starting_option_price)}}<br>
Current Option Price: ${{fmtMoney(pending.current_option_price)}}<br>
Highest Option Price: ${{fmtMoney(pending.highest_option_price)}}<br>
Lowest Option Price: ${{fmtMoney(pending.lowest_option_price)}}<br>
Confirmation Target Price: ${{fmtMoney(pending.confirmation_price)}}<br>
Confirmation Price Source: ${{escapeHtml(pending.confirmation_price_source || "N/A")}}<br>
Required Momentum %: ${{fmtPercent(pending.required_confirmation_percent)}}<br>
Current Momentum Gain %: ${{fmtPercent(pending.current_momentum_gain_percent)}}<br>
Current Pre-Confirmation Drawdown %: ${{fmtPercent(pending.current_pre_confirmation_drawdown_percent)}}<br>
Maximum Allowed Drawdown %: ${{fmtPercent(pending.maximum_allowed_drawdown_percent)}}<br>
Momentum Confirmed: ${{pending.momentum_confirmed ? "YES" : "NO"}}<br>
Breakout Confirmed: ${{pending.breakout_confirmed ? "YES" : "NO"}}<br>
Momentum Timeout: ${{escapeHtml(pending.momentum_timeout_seconds ?? 0)}} sec<br>
Elapsed Time: ${{escapeHtml(pending.elapsed_time_seconds ?? 0)}} sec<br>
Retry Cooldown Remaining: ${{escapeHtml(pending.retry_cooldown_remaining_seconds ?? 0)}} sec<br>
Current Breakout Candle: ${{escapeHtml(pending.current_breakout_candle ?? 0)}}<br>
Required Breakout Candles: ${{escapeHtml(pending.required_breakout_candles ?? 0)}}<br>
Breakout Level: ${{fmtMoney(pending.breakout_level)}}<br>
Time Remaining: ${{pending.time_remaining_seconds || 0}} sec<br>
Reason: ${{escapeHtml(pending.reason || "Waiting for option momentum confirmation")}}
</div>`;
}}

function renderPendingEntryHistory(history, limit) {{
    const el = document.getElementById("pending-entry-history-content");
    if (!el) return;

    limit = Number(limit || {DEFAULT_HISTORY_LIMIT});
    const rows = (history || []).slice(-limit).reverse();
    if (!rows.length) {{
        el.innerHTML = "No pending entry history.";
        return;
    }}

    el.innerHTML = rows.map((row) => {{
        const breakoutProgress = `${{row.current_breakout_candle ?? 0}}/${{row.required_breakout_candles ?? 0}}`;
        const buyLine = row.buy_submitted ? "<br>BUY SUBMITTED" : "";
        const cancelLine = row.cancellation_reason ? `<br>Cancellation Reason: ${{escapeHtml(row.cancellation_reason)}}` : "";
        return `
<div class="trade-card bot">
${{escapeHtml(row.timestamp || "")}}<br>
${{escapeHtml(row.direction || "")}}<br>
Underlying: ${{escapeHtml(row.underlying || "")}}<br>
Option: ${{escapeHtml(row.option_symbol || "")}}<br>
Signal Generated: ${{escapeHtml(row.signal_generated || "")}}<br>
Momentum: ${{escapeHtml(row.momentum_status || "WAITING")}}<br>
Breakout: ${{escapeHtml(row.breakout_status || "PASS")}} ${{escapeHtml(breakoutProgress)}}<br>
Starting Option Price: ${{fmtMoney(row.starting_option_price)}}<br>
Current Option Price: ${{fmtMoney(row.current_option_price)}}<br>
Highest Option Price: ${{fmtMoney(row.highest_option_price)}}<br>
Lowest Option Price: ${{fmtMoney(row.lowest_option_price)}}<br>
Confirmation Target Price: ${{fmtMoney(row.confirmation_price)}}<br>
Confirmation Price Source: ${{escapeHtml(row.confirmation_price_source || "N/A")}}<br>
Current Momentum Gain %: ${{fmtPercent(row.current_momentum_gain_percent)}}<br>
Current Pre-Confirmation Drawdown %: ${{fmtPercent(row.current_pre_confirmation_drawdown_percent)}}<br>
Maximum Allowed Drawdown %: ${{fmtPercent(row.maximum_allowed_drawdown_percent)}}<br>
Momentum Confirmed: ${{row.momentum_confirmed ? "YES" : "NO"}}<br>
Breakout Confirmed: ${{row.breakout_confirmed ? "YES" : "NO"}}<br>
Momentum Timeout: ${{escapeHtml(row.momentum_timeout_seconds ?? 0)}} sec<br>
Elapsed Time: ${{escapeHtml(row.elapsed_time_seconds ?? 0)}} sec<br>
Retry Cooldown Remaining: ${{escapeHtml(row.retry_cooldown_remaining_seconds ?? 0)}} sec<br>
Breakout Progress: ${{escapeHtml(breakoutProgress)}}<br>
Final Cancellation Trigger: ${{escapeHtml(row.final_cancellation_trigger || "")}}<br>
Final Status: ${{escapeHtml(row.final_status || "WAITING")}}
${{buyLine}}
${{cancelLine}}
</div>`;
    }}).join("");
}}

function fmtAdversePercent(value) {{
    if (value === null || value === undefined || value === "") return "N/A";
    return `${{(-Math.abs(Number(value || 0))).toFixed(2)}}%`;
}}

function fmtSecondsOrNa(value) {{
    if (value === null || value === undefined || value === "") return "N/A";
    return `${{escapeHtml(value)}}s`;
}}

let shadowAutoRefreshPaused = false;
let latestShadowResearchData = null;

function currentTimeString() {{
    return new Date().toLocaleTimeString([], {{ hour12: false }});
}}

function setShadowRefreshButton() {{
    const button = document.getElementById("shadow-refresh-toggle");
    if (!button) return;
    button.textContent = shadowAutoRefreshPaused ? "Resume Shadow Auto-Refresh" : "Pause Shadow Auto-Refresh";
}}

function toggleShadowAutoRefresh() {{
    shadowAutoRefreshPaused = !shadowAutoRefreshPaused;
    setShadowRefreshButton();
    if (!shadowAutoRefreshPaused && latestShadowResearchData) {{
        renderShadowResearch(latestShadowResearchData, true);
    }}
}}

function renderShadowResearch(summary) {{
    const el = document.getElementById("shadow-research-content");
    if (!el) return;
    latestShadowResearchData = summary || {{}};
    if (shadowAutoRefreshPaused) return;
    renderShadowResearchNow(latestShadowResearchData);
}}

function renderShadowResearchNow(summary) {{
    const el = document.getElementById("shadow-research-content");
    if (!el) return;
    summary = summary || {{}};
    const rows = (summary.latest_candidates || []).slice(0, 20);
    const gateStats = summary.gate_outcome_statistics || {{}};
    const gateOutcomes = ["WOULD_ENTER", "REJECTED_MOMENTUM", "REJECTED_BREAKOUT", "REJECTED_DRAWDOWN", "REJECTED_TIMEOUT", "REJECTED_COOLDOWN", "WAITING", "N/A"];
    const gateRows = gateOutcomes.map((outcome) => {{
        const stats = gateStats[outcome] || {{}};
        return `${{escapeHtml(outcome)}}: ${{escapeHtml(stats.count ?? 0)}} candidates | +5 before -5: ${{fmtPercent(stats.plus_5_before_minus_5_rate)}} | Prevented Winners: ${{escapeHtml(stats.prevented_winner_count ?? 0)}} (${{fmtPercent(stats.prevented_winner_rate)}}) | Prevented Losers: ${{escapeHtml(stats.prevented_loser_count ?? 0)}} (${{fmtPercent(stats.prevented_loser_rate)}})`;
    }}).join("<br>");
    const latest = rows.length ? rows.map((row) => `
<div class="trade-card bot">
${{escapeHtml(row.timestamp || "")}}<br>
${{escapeHtml(row.direction || "")}} ${{escapeHtml(row.option_symbol || "")}}<br>
Status: ${{escapeHtml(row.status || "")}}<br>
Data Quality: ${{escapeHtml(row.data_quality_status || "OK")}}<br>
Invalid Quotes: ${{escapeHtml(row.invalid_quote_count || 0)}}<br>
Last Invalid Quote Reason: ${{escapeHtml(row.last_invalid_quote_reason || "N/A")}}<br>
SPY: ${{fmtMoney(row.spy_price)}}<br>
Start: ${{fmtMoney(row.starting_option_price || row.option_midpoint)}}<br>
Best Move: ${{fmtPercent(row.maximum_favorable_excursion_5m || row.mfe_percent)}} at ${{fmtSecondsOrNa(row.time_to_maximum_favorable_excursion_seconds)}}<br>
Worst Move: ${{fmtAdversePercent(row.maximum_adverse_excursion_5m || row.mae_percent)}} at ${{fmtSecondsOrNa(row.time_to_maximum_adverse_excursion_seconds)}}<br>
Time to +5%: ${{fmtSecondsOrNa(row.time_to_plus_5_seconds)}}<br>
Time to -5%: ${{fmtSecondsOrNa(row.time_to_minus_5_seconds)}}<br>
+5 Before -5: ${{escapeHtml(row.hit_plus_5_before_minus_5 || "")}}<br>
Class: ${{escapeHtml(row.classification || "")}}<br>
Momentum Gate: ${{escapeHtml(row.momentum_confirmation_state || "N/A")}} (${{escapeHtml(row.momentum_percentage_observed || "N/A")}} / ${{escapeHtml(row.momentum_percentage_required || "N/A")}}%)<br>
Breakout Gate: ${{escapeHtml(row.breakout_confirmation_state || "N/A")}} (${{escapeHtml(row.breakout_progress_current_candles || "N/A")}} / ${{escapeHtml(row.breakout_progress_required_candles || "N/A")}})<br>
Pre-Confirmation Drawdown: ${{escapeHtml(row.pre_confirmation_max_observed_drawdown_percent || "N/A")}} / ${{escapeHtml(row.pre_confirmation_configured_max_drawdown_percent || "N/A")}}% | Failed: ${{escapeHtml(row.pre_confirmation_drawdown_failed || "N/A")}}<br>
Timeout: ${{escapeHtml(row.confirmation_timed_out || "N/A")}} at ${{escapeHtml(row.confirmation_timeout_elapsed_seconds || "N/A")}}s<br>
Retry Cooldown: ${{escapeHtml(row.retry_cooldown_active || "N/A")}} | Remaining: ${{escapeHtml(row.retry_cooldown_remaining_seconds || "N/A")}}s<br>
Live Gate Outcome: ${{escapeHtml(row.final_live_gate_outcome || "N/A")}}
</div>`).join("") : "No shadow candidates recorded.";

    el.innerHTML = `
Candidates Today: <span id="shadow-candidates-today">${{escapeHtml(summary.candidates_today ?? 0)}}</span><br>
Completed Candidates: <span id="shadow-completed-candidates">${{escapeHtml(summary.completed_candidates ?? 0)}}</span><br>
Valid Completed Candidates: <span id="shadow-valid-completed-candidates">${{escapeHtml(summary.valid_completed_candidates ?? 0)}}</span><br>
Contaminated Candidates: <span id="shadow-contaminated-candidates">${{escapeHtml(summary.contaminated_candidates ?? 0)}}</span><br>
Active Candidates: <span id="shadow-active-candidates">${{escapeHtml(summary.active_candidates ?? 0)}}</span><br>
Valid +5 Before -5 Success Rate: <span id="shadow-plus5-rate">${{fmtPercent(summary.valid_plus_5_before_minus_5_success_rate ?? summary.plus_5_before_minus_5_success_rate)}}</span><br>
Valid Average 5m MFE: <span id="shadow-average-mfe">${{fmtPercent(summary.valid_average_5m_mfe ?? summary.average_5m_mfe)}}</span><br>
Valid Average 5m MAE: <span id="shadow-average-mae">${{fmtAdversePercent(summary.valid_average_5m_mae ?? summary.average_5m_mae)}}</span><br>
<br>
Gate Outcome Research:<br>
${{gateRows}}<br>
<br>
Latest 20 Candidates:
<div class="history-panel shadow-research-panel" id="shadow-research-latest">
${{latest}}
</div>`;
    setText("shadow-last-ui-update", currentTimeString());
}}

const TOURNAMENT_PROFILES = [
    ["BOT_A_BASELINE", "Bot A Baseline"],
    ["BOT_B_MOMENTUM", "Bot B Momentum"],
    ["BOT_C_TWO_CANDLE_OR", "Bot C Two-Candle OR"],
    ["BOT_D_COMBINED", "Bot D Combined"]
];

function renderTournamentDecisions(decisions, evaluatedByProfile, settings, stateByProfile) {{
    const cardsEl = document.getElementById("tournament-decision-cards");
    const rawEl = document.getElementById("tournament-decisions-raw-body");
    if (!cardsEl && !rawEl) return;

    decisions = decisions || {{}};
    evaluatedByProfile = evaluatedByProfile || {{}};
    settings = settings || {{}};
    stateByProfile = stateByProfile || {{}};
    if (cardsEl) {{
        cardsEl.innerHTML = TOURNAMENT_PROFILES.map(([profileId, label]) => {{
            const row = decisions[profileId] || {{}};
            const profileSettings = settings[profileId] || {{}};
            const state = stateByProfile[profileId] || {{}};
            const counters = state.pipeline_counters || {{}};
            const blockReasons = counters.entry_block_reasons || {{}};
            const finalDirection = row.final_direction || row.direction || "NONE";
            const dominantSide = row.preliminary_direction || "NONE";
            const accepted = Boolean(row.accepted);
            const acceptedClass = accepted ? "good" : "bad";
            const orRequired = Boolean(row.or_confirmation_required);
            const orProgress = orRequired
                ? `${{row.two_candle_or_passed ? "PASS" : "WAITING"}} / ${{escapeHtml(profileSettings.required_breakout_candles ?? 2)}} candles required`
                : "Not required";
            const counterRows = Object.entries(counters)
                .filter(([key]) => key !== "entry_block_reasons")
                .map(([key, value]) => `<div class="debug-row"><span>${{escapeHtml(key.replaceAll("_", " "))}}</span><strong>${{escapeHtml(value)}}</strong></div>`)
                .join("");
            const blockRows = Object.entries(blockReasons).length
                ? Object.entries(blockReasons).map(([reason, count]) => `<div class="debug-row"><span>${{escapeHtml(reason)}}</span><strong>${{escapeHtml(count)}}</strong></div>`).join("")
                : '<div class="debug-row"><span>Entry Block Reasons</span><strong>None</strong></div>';
            const position = state.virtual_position || null;
            const positionSource = position?.position_source || state.position_source || "NONE";
            return `
<article class="tournament-decision-card" data-profile-id="${{escapeHtml(profileId)}}">
<div class="tournament-card-header">
<div><div class="profile-kicker">${{escapeHtml(profileId)}}</div><h3>${{escapeHtml(label)}}</h3></div>
<div class="status-badges"><span class="status-pill">${{profileSettings.enabled === false ? "Disabled" : "Enabled"}}</span><span class="status-pill">${{escapeHtml(row.status || "N/A")}}</span></div>
</div>
<section class="decision-highlight">
<div><span>Final Direction</span><strong>${{escapeHtml(finalDirection)}}</strong></div>
<div><span>Accepted</span><strong class="${{acceptedClass}}">${{escapeHtml(accepted)}}</strong></div>
<div><span>Entry Status</span><strong>${{escapeHtml(row.entry_status || "N/A")}}</strong></div>
<div class="wide"><span>Entry Block Reason</span><strong>${{escapeHtml(row.entry_block_reason || "None")}}</strong></div>
<div class="wide"><span>Rejection Reason</span><strong>${{escapeHtml(row.rejection_reason || "None")}}</strong></div>
</section>
<div class="tournament-section-grid">
<section class="tournament-section"><h4>Signal</h4><div class="kv-grid">
<span>Bullish Score</span><strong>${{escapeHtml(row.bullish_score ?? 0)}}</strong><span>Bearish Score</span><strong>${{escapeHtml(row.bearish_score ?? 0)}}</strong><span>Confidence</span><strong>${{escapeHtml(row.confidence ?? 0)}}</strong><span>Dominant Side</span><strong>${{escapeHtml(dominantSide)}}</strong><span>Dominance</span><strong>${{Number(row.dominance_percent || 0).toFixed(1)}}%</strong><span>Direction Threshold</span><strong>${{Number(row.direction_threshold || 0).toFixed(1)}}%</strong><span>Minimum Dominance</span><strong>${{Number(row.minimum_dominance || 0).toFixed(1)}}%</strong>
</div></section>
<section class="tournament-section"><h4>Contract</h4><div class="kv-grid">
<span>Call Contract</span><strong>${{escapeHtml(row.call_contract || "N/A")}}</strong><span>Put Contract</span><strong>${{escapeHtml(row.put_contract || "N/A")}}</strong><span>Candidate Direction</span><strong>${{escapeHtml(row.momentum_candidate_direction || "N/A")}}</strong><span>Candidate Option</span><strong>${{escapeHtml(row.momentum_candidate_option_symbol || "N/A")}}</strong><span>Direction Match</span><strong>${{escapeHtml(row.contract_direction_match ?? "N/A")}}</strong><span>Starting Price</span><strong>${{fmtMoney(row.momentum_starting_price)}}</strong><span>Current Price</span><strong>${{fmtMoney(row.momentum_current_price)}}</strong><span>Option Premium</span><strong>${{fmtMoney(row.option_premium)}}</strong><span>Contracts</span><strong>${{escapeHtml(row.contracts || profileSettings.contracts || "N/A")}}</strong><span>Total Position Cost</span><strong>${{fmtMoney(row.total_position_cost)}}</strong><span>Maximum Position Cost</span><strong>${{fmtMoney(row.maximum_position_cost_dollars)}}</strong>
</div></section>
<section class="tournament-section"><h4>Momentum</h4><div class="kv-grid">
<span>Result</span><strong>${{escapeHtml(row.momentum_status || "N/A")}}</strong><span>Movement Observed</span><strong>${{Number(row.momentum_observed_percent || 0).toFixed(2)}}%</strong><span>Movement Required</span><strong>${{Number(row.momentum_required_percent || 0).toFixed(2)}}%</strong><span>Candidate Age</span><strong>${{Number(row.momentum_candidate_age_seconds || 0).toFixed(0)}}s</strong><span>Time Remaining</span><strong>${{Number(row.momentum_time_remaining_seconds || 0).toFixed(0)}}s</strong><span>Block Reason</span><strong>${{escapeHtml(row.momentum_block_reason || "None")}}</strong>
</div></section>
<section class="tournament-section"><h4>Opening Range</h4><div class="kv-grid">
<span>OR Result</span><strong>${{escapeHtml(row.or_confirmation_status || "N/A")}}</strong><span>Required</span><strong>${{escapeHtml(orRequired)}}</strong><span>Progress</span><strong>${{orProgress}}</strong>
</div></section>
</div>
<section class="final-decision-section"><h4>Final Decision</h4><div class="final-decision-grid"><div><span>Direction</span><strong>${{escapeHtml(finalDirection)}}</strong></div><div><span>Accepted</span><strong class="${{acceptedClass}}">${{escapeHtml(accepted)}}</strong></div><div><span>Entry Status</span><strong>${{escapeHtml(row.entry_status || "N/A")}}</strong></div><div><span>Entry Block Reason</span><strong>${{escapeHtml(row.entry_block_reason || "None")}}</strong></div><div><span>Rejection Reason</span><strong>${{escapeHtml(row.rejection_reason || "None")}}</strong></div></div></section>
<details class="pipeline-debug"><summary>Show Advanced Pipeline Debug</summary><div class="debug-grid">${{counterRows}}${{blockRows}}<div class="debug-row"><span>Decisions Evaluated</span><strong>${{escapeHtml(evaluatedByProfile[profileId] || 0)}}</strong></div><div class="debug-row"><span>Position Status</span><strong>${{escapeHtml(position?.status || "NONE")}}</strong></div><div class="debug-row"><span>Position Source</span><strong>${{escapeHtml(positionSource)}}</strong></div><div class="debug-row"><span>Position Option</span><strong>${{escapeHtml(position?.option_symbol || "N/A")}}</strong></div></div></details>
</article>`;
        }}).join("");
    }}
    if (rawEl) rawEl.innerHTML = TOURNAMENT_PROFILES.map(([profileId, label]) => {{
        const row = decisions[profileId] || {{}};
        const enabled = settings[profileId]?.enabled;
        const state = stateByProfile[profileId] || {{}};
        const position = state.virtual_position || null;
        const positionSource = position?.position_source || state.position_source || "NONE";
        const counters = state.pipeline_counters || {{}};
        const blockReasons = counters.entry_block_reasons || {{}};
        const blockText = Object.entries(blockReasons).map(([reason, count]) => `${{reason}}:${{count}}`).join(", ");
        return `
<tr>
<td>${{escapeHtml(label)}}</td>
<td>${{escapeHtml(enabled ?? "N/A")}}</td>
<td>${{escapeHtml(row.status || "N/A")}}</td>
<td>${{escapeHtml(row.bullish_score ?? 0)}}</td>
<td>${{escapeHtml(row.bearish_score ?? 0)}}</td>
<td>${{escapeHtml(row.preliminary_direction || "NONE")}}</td>
<td>${{Number(row.bullish_dominance_percent || 0).toFixed(1)}}% / ${{Number(row.bearish_dominance_percent || 0).toFixed(1)}}%</td>
<td>${{Number(row.direction_threshold || 0).toFixed(1)}}%</td>
<td>${{Number(row.minimum_dominance || 0).toFixed(1)}}%</td>
<td>${{escapeHtml(row.momentum_status || "N/A")}}</td>
<td>${{escapeHtml(row.call_contract || "")}}</td>
<td>${{escapeHtml(row.put_contract || "")}}</td>
<td>${{escapeHtml(row.momentum_candidate_direction || "")}}</td>
<td>${{escapeHtml(row.momentum_candidate_option_symbol || "")}}</td>
<td>${{escapeHtml(row.candidate_contract_type || "")}}</td>
<td>${{escapeHtml(row.decision_direction || row.final_direction || row.direction || "")}}</td>
<td>${{escapeHtml(row.contract_direction_match === null || row.contract_direction_match === undefined ? "" : row.contract_direction_match)}} ${{escapeHtml(row.contract_direction_mismatch_reason || "")}}</td>
<td>${{fmtMoney(row.momentum_starting_price)}}</td>
<td>${{fmtMoney(row.momentum_current_price)}}</td>
<td>${{Number(row.momentum_observed_percent || 0).toFixed(2)}}%</td>
<td>${{Number(row.momentum_required_percent || 0).toFixed(2)}}%</td>
<td>${{Number(row.momentum_candidate_age_seconds || 0).toFixed(0)}}s</td>
<td>${{Number(row.momentum_time_remaining_seconds || 0).toFixed(0)}}s</td>
<td>${{escapeHtml(row.momentum_block_reason || "")}}</td>
<td>${{escapeHtml(row.or_confirmation_status || "N/A")}}</td>
<td>${{escapeHtml(row.final_direction || row.direction || "NONE")}}</td>
<td>${{escapeHtml(row.accepted ?? false)}}</td>
<td>${{escapeHtml(row.rejection_reason || "")}}</td>
<td>${{escapeHtml(row.entry_status || "")}}</td>
<td>${{escapeHtml(row.entry_block_reason || "")}}</td>
<td>${{fmtMoney(row.option_premium)}}</td>
<td>${{escapeHtml(row.contracts || settings.contracts || "")}}</td>
<td>${{fmtMoney(row.total_position_cost)}}</td>
<td>${{fmtMoney(row.maximum_position_cost_dollars)}}</td>
<td>${{escapeHtml(evaluatedByProfile[profileId] || 0)}}</td>
<td>
C:${{escapeHtml(counters.candidates_started || 0)}}
 M:${{escapeHtml(counters.momentum_confirmed || 0)}}
 OR:${{escapeHtml(counters.or_confirmed || 0)}}
 A:${{escapeHtml(counters.decisions_accepted || 0)}}
 Try:${{escapeHtml(counters.entries_attempted || 0)}}
 Open:${{escapeHtml(counters.entries_opened || 0)}}<br>
<small>${{escapeHtml(blockText)}}</small>
</td>
<td>${{escapeHtml(position?.status || "NONE")}}</td>
<td>${{escapeHtml(positionSource)}}</td>
<td>${{escapeHtml(position?.option_symbol || "")}}</td>
<td>${{position ? fmtMoney(position.entry_price) : ""}}</td>
<td>${{position ? fmtMoney(position.current_price) : ""}}</td>
<td>${{position ? fmtSignedMoney(position.unrealized_pnl_dollars) : ""}}</td>
<td>${{position ? fmtSignedPercent(position.unrealized_pnl_percent) : ""}}</td>
<td>${{position ? fmtMoney(position.peak_price) : ""}}</td>
<td>${{position ? fmtMoney(position.hard_stop_price) : ""}}</td>
<td>${{position ? fmtMoney(position.trailing_stop_price) : ""}}</td>
<td>${{position ? fmtMoney(position.profit_floor_price) : ""}}</td>
</tr>`;
    }}).join("");
}}

function renderTournamentCandidateTransitions(stateByProfile) {{
    const el = document.getElementById("tournament-candidate-transitions");
    if (!el) return;
    stateByProfile = stateByProfile || {{}};
    const rows = [];
    for (const profileId of ["BOT_B_MOMENTUM", "BOT_D_COMBINED"]) {{
        const state = stateByProfile[profileId] || {{}};
        for (const row of (state.candidate_transitions || [])) {{
            rows.push(row);
        }}
    }}
    rows.sort((a, b) => Number(b.time || 0) - Number(a.time || 0));
    const latest = rows.slice(0, 20);
    if (!latest.length) {{
        el.innerHTML = 'No candidate transitions yet.';
        return;
    }}
    el.innerHTML = latest.map((row) => `
<article class="candidate-transition-card">
<div class="transition-header"><strong>${{escapeHtml(row.profile_name || row.profile_id || "")}}</strong><span>${{escapeHtml(row.time || "")}}</span><span class="status-pill">${{escapeHtml(row.status || "N/A")}}</span></div>
<div class="transition-grid"><span>Direction</span><strong>${{escapeHtml(row.old_direction || "NONE")}} &rarr; ${{escapeHtml(row.new_direction || "NONE")}}</strong><span>Option</span><strong>${{escapeHtml(row.old_option || "N/A")}} &rarr; ${{escapeHtml(row.new_option || "N/A")}}</strong><span>Candidate Age</span><strong>${{Number(row.candidate_age || 0).toFixed(1)}}s</strong><span>Reason</span><strong>${{escapeHtml(row.reason || "None")}}</strong></div>
</article>`).join("");
}}

function fmtSignedMoney(value) {{
    if (value === null || value === undefined || value === "") return "";
    const num = Number(value);
    if (Number.isNaN(num)) return "";
    const prefix = num > 0 ? "+" : "";
    return `${{prefix}}${{fmtMoney(num)}}`;
}}

function fmtSignedPercent(value) {{
    if (value === null || value === undefined || value === "") return "";
    const num = Number(value);
    if (Number.isNaN(num)) return "";
    const prefix = num > 0 ? "+" : "";
    return `${{prefix}}${{num.toFixed(2)}}%`;
}}

function renderTournamentTrades(trades) {{
    const el = document.getElementById("tournament-trades-body");
    if (!el) return;
    trades = trades || [];
    if (!trades.length) {{
        el.innerHTML = '<tr><td colspan="14">No tournament trades yet.</td></tr>';
        return;
    }}
    el.innerHTML = trades.map((trade) => `
<tr>
<td>${{escapeHtml(trade.entry_time || "")}}</td>
<td title="${{escapeHtml(trade.profile_id || "")}}"><strong>${{escapeHtml(trade.profile_display_name || "")}}</strong><br><small>${{escapeHtml(trade.profile_id || "")}}</small></td>
<td>${{escapeHtml(trade.direction || "")}}</td>
<td>${{escapeHtml(trade.option_symbol || "")}}</td>
<td>${{escapeHtml(trade.contracts ?? "")}}</td>
<td>${{fmtMoney(trade.entry_price)}}</td>
<td>${{trade.exit_price === null || trade.exit_price === undefined ? "" : fmtMoney(trade.exit_price)}}</td>
<td>${{escapeHtml(trade.status || "")}}</td>
<td>${{escapeHtml(trade.exit_reason || "")}}</td>
<td>${{fmtSignedMoney(trade.pnl_dollars)}}</td>
<td>${{fmtSignedPercent(trade.pnl_percent)}}</td>
<td>${{escapeHtml(trade.confidence ?? "")}}</td>
<td>${{Number(trade.dominance_percent || 0).toFixed(2)}}%</td>
<td>${{escapeHtml(trade.market_state || "")}}</td>
</tr>`).join("");
}}

function renderTournamentHealth(health) {{
    const el = document.getElementById("tournament-health-grid");
    if (!el) return;
    health = health || {{}};
    el.innerHTML = `
<div class="item"><div class="label">Health</div><div class="value">${{escapeHtml(health.overall_status || "UNKNOWN")}}</div></div>
<div class="item"><div class="label">Entry Loop</div><div class="value">${{health.entry_loop_alive ? "YES" : "NO"}}</div></div>
<div class="item"><div class="label">Exit Loop</div><div class="value">${{health.exit_loop_alive ? "YES" : "NO"}}</div></div>
<div class="item"><div class="label">Open Positions</div><div class="value">${{escapeHtml(health.open_position_count ?? 0)}}</div></div>
<div class="item"><div class="label">Last Quote Age</div><div class="value">${{escapeHtml(health.seconds_since_last_successful_quote ?? "N/A")}}</div></div>
<div class="item"><div class="label">Last Save Age</div><div class="value">${{escapeHtml(health.state_save_age ?? "N/A")}}</div></div>
<div class="item"><div class="label">Recovery</div><div class="value">${{escapeHtml(health.recovery_status || "UNKNOWN")}}</div></div>
<div class="item"><div class="label">Last Error</div><div class="value">${{escapeHtml(health.last_error || "None")}}</div></div>`;
    const warningEl = el.nextElementSibling;
    if (warningEl) {{
        const warnings = health.warnings || [];
        warningEl.innerHTML = `Warnings:<br>${{warnings.length ? warnings.map(escapeHtml).join("<br>") : "None"}}`;
    }}
}}

async function updateTournamentTrades() {{
    const data = await getJson("/api/tournament/trades?limit=50");
    renderTournamentTrades(data.trades || []);
    const reporting = await getJson("/api/strategy-research-display");
    for (const [id, markup] of [["human-bot-comparison", reporting.comparison_html], ["strategy-research-cards", reporting.research_html]]) {{
        const target = document.getElementById(id);
        if (target && typeof markup === "string" && target.innerHTML !== markup) target.innerHTML = markup;
    }}
}}

async function createTestTournamentTrade() {{
    const profileId = document.getElementById("tournament-test-profile")?.value || "BOT_A_BASELINE";
    await fetch("/api/tournament/trades/test-record", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ profile_id: profileId, entry_price: 1.0 }})
    }});
    await updateTournamentTrades();
}}

async function closeLatestTestTournamentTrade() {{
    await fetch("/api/tournament/trades/test-close", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{}})
    }});
    await updateTournamentTrades();
}}

async function runTournamentPipelineProof() {{
    const el = document.getElementById("tournament-proof-result");
    if (el) el.textContent = "Running pipeline proof...";
    const response = await fetch("/api/tournament/pipeline-proof/run", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{}})
    }});
    const data = await response.json();
    if (el) el.textContent = JSON.stringify(data, null, 2);
    await updateTournamentTrades();
}}

async function deleteTournamentPipelineProofTrades() {{
    const el = document.getElementById("tournament-proof-result");
    const response = await fetch("/api/tournament/pipeline-proof/delete", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{}})
    }});
    const data = await response.json();
    if (el) el.textContent = JSON.stringify(data, null, 2);
    await updateTournamentTrades();
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
    const pendingHistoryLimit = Number(data.history_limits?.pending_entry_limit || {DEFAULT_HISTORY_LIMIT});
    renderPendingEntry(data.pending_entry);
    renderPendingEntryHistory(data.pending_entry_history, pendingHistoryLimit);
    renderShadowResearch(data.shadow_research);
    renderTournamentDecisions(data.tournament_decisions, data.tournament_decisions_evaluated_by_profile, data.tournament_profile_settings, data.tournament_state_by_profile);
    renderTournamentCandidateTransitions(data.tournament_state_by_profile);
    renderTournamentHealth(data.tournament_health);
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
    setText("quote-environment", data.market_data_environment || "UNKNOWN");
    setText("quote-broker-age", data.quote_age_seconds === "" || data.quote_age_seconds == null ? "N/A" : `${{Number(data.quote_age_seconds).toFixed(1)}}s`);
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
    setText("exit-quote-status", position.quote_status || "N/A");
    setText("exit-peak", fmtMoney(position.peak_price));
    setText("exit-trailing-stop", fmtMoney(position.trailing_stop_price));
    setText("exit-profit-floor-enabled", position.profit_floor_enabled ? "YES" : "NO");
    setText("exit-locked-profit-amount", fmtMoney(position.locked_profit_dollars));
    setText("exit-required-premium-movement", fmtMoney(position.required_premium_movement));
    setText("exit-profit-floor-price", fmtMoney(position.profit_floor_price));
    setText("exit-profit-floor-activated", position.profit_floor_activated ? "YES" : "NO");
    setText("exit-percentage-trailing-stop", fmtMoney(position.percentage_trailing_stop));
    setText("exit-effective-trailing-stop", fmtMoney(position.effective_trailing_stop));
    setText("exit-stop-control-rule", position.stop_control_rule || "HARD STOP");
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
setInterval(updateTournamentTrades, 5000);
let accountRefreshBusy = false;
async function updateConnectedAccount() {{
    if (accountRefreshBusy) return;
    accountRefreshBusy = true;
    try {{
        const data = await getJson("/api/account");
        const panel = document.getElementById("connected-account");
        if (panel && panel.innerHTML !== data.html) panel.innerHTML = data.html;
        document.getElementById("broker-environment-label").textContent = data.account.environment_label;
    }} catch (error) {{
        document.getElementById("broker-environment-label").textContent = "UNKNOWN / CONNECTION ERROR";
        document.getElementById("connected-account").textContent = "Account refresh failed — values unavailable. Retrying.";
    }} finally {{ accountRefreshBusy = false; }}
}}
updateConnectedAccount();
setInterval(updateConnectedAccount, 10000);

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
    initialize_pending_entry_history()
    recover_shadow_research_on_startup()
    initialize_tournament_recovery()
    load_exit_state()
    atexit.register(shutdown_tournament_monitors)
    threading.Thread(target=surfer_bot_loop, daemon=True).start()
    start_tournament_exit_monitor()
    start_tournament_watchdog()
    app.run(host="127.0.0.1", port=5000)


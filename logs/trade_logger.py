import csv
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")


def market_now():
    return datetime.now(MARKET_TZ)

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADES_FILE = os.path.join(APP_DIR, "trades.csv")
VISIBLE_TRADES_FILE = os.path.join(APP_DIR, "dashboard_visible_trades.csv")
TRADE_HISTORY_VISIBLE_FILE = os.path.join(APP_DIR, "dashboard_trade_history_visible.csv")
TRADE_HISTORY_BACKUP_FILE = os.path.join(APP_DIR, "dashboard_trade_history_last_cleared.csv")
BOT_VISIBLE_TRADES_BACKUP_FILE = os.path.join(APP_DIR, "dashboard_bot_trades_last_cleared.csv")
HUMAN_VISIBLE_TRADES_BACKUP_FILE = os.path.join(APP_DIR, "dashboard_human_trades_last_cleared.csv")
TRADE_COLUMNS = [
    "Time", "Action", "Symbol", "Qty", "Price", "PnL",
    "Source", "EntryGrade", "LiveGrade", "ExitGrade", "BotGrade", "OverallGrade",
    "TradeScore", "GradeReason", "HoldTime", "PeakPrice",
    "HardStopPrice", "TrailingStopPrice", "MaxDrawdownFromPeakPercent",
    "PnLPercent", "ExitReason", "EntryPriceSource", "EstimatedEntryPrice",
    "EntryMarketState", "EntryBullishScore", "EntryBearishScore",
    "EntryConfidence", "EntryDominancePercent", "EntryDecision", "EntryReasonLog",
    "EntryIndicatorBreakdown"
]

_CALLBACKS = {}


def configure_trade_logger(**callbacks):
    _CALLBACKS.update(callbacks)


def _callback(name, default=None):
    return _CALLBACKS.get(name, default)


def safe_float(value, default=0.0):
    try:
        return float(value)
    except:
        return default


def grade_score(grade):
    return {"A+": 97, "A": 92, "B": 85, "C": 75, "D": 65, "F": 45}.get(grade, 0)


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


def entry_indicator_breakdown(reasons):
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
        ("Opening Range High/Low", "Broke opening range high", "Broke opening range low"),
    ]

    rows = []
    for label, bullish_phrase, bearish_phrase in rules:
        bullish_hit = bullish_phrase in reason_text
        bearish_hit = bearish_phrase in reason_text
        rows.append({
            "label": label,
            "passed": bullish_hit or bearish_hit,
            "points": 1 if bullish_hit or bearish_hit else 0,
            "direction": "bullish" if bullish_hit else "bearish" if bearish_hit else "neutral",
        })
    return rows


def ensure_trade_file(path):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        write_trade_header(path)
        return

    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames or []

    if fieldnames == TRADE_COLUMNS:
        return

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in TRADE_COLUMNS})


def append_trade_row(path, row):
    ensure_trade_file(path)

    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_COLUMNS)
        writer.writerow({column: row.get(column, "") for column in TRADE_COLUMNS})


def write_trade_header(path):
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(TRADE_COLUMNS)


def find_last_buy(symbol):
    try:
        with open(TRADES_FILE, "r", newline="") as f:
            rows = list(csv.DictReader(f))
    except:
        return None

    for row in reversed(rows):
        if row.get("Symbol") == symbol and row.get("Action") == "BUY":
            return row
    return None


def log_trade(
    action,
    symbol,
    qty,
    price="",
    pnl="",
    source="HUMAN",
    market_context=None,
    entry_price_source="",
    estimated_entry_price=""
):
    current_market_context_snapshot = _callback("current_market_context_snapshot")
    grade_entry_setup = _callback("grade_entry_setup")
    grade_exit_trade = _callback("grade_exit_trade")
    stop_debug_values = _callback("stop_debug_values")
    load_config = _callback("load_config")
    sync_trade_limits_from_file = _callback("sync_trade_limits_from_file")

    if market_context is None:
        market_context = current_market_context_snapshot() if current_market_context_snapshot else {}

    entry_grade = ""
    exit_grade = ""
    bot_grade = ""
    trade_score = ""
    grade_reason = ""
    hold_time = ""
    peak_price = ""
    hard_stop_price = ""
    trailing_stop_price = ""
    max_drawdown_from_peak_percent = ""
    pnl_percent = ""
    exit_reason = ""
    entry_market_state = ""
    entry_bullish_score = ""
    entry_bearish_score = ""
    entry_confidence = ""
    entry_dominance_percent = ""
    entry_decision = ""
    entry_reason_log = ""
    entry_indicator_breakdown_json = ""

    if action == "BUY" and grade_entry_setup:
        entry_grade, trade_score, grade_reason = grade_entry_setup(market_context)
        bot_grade = entry_grade
        entry_reasons = [str(reason) for reason in (market_context.get("decision_reasons") or market_context.get("reasons") or [])]
        if market_context.get("decision") in ["BUY CALL", "BUY PUT"]:
            entry_reasons.append(f"SIGNAL entered {market_context.get('decision').replace('BUY ', '')}")
        entry_market_state = market_context.get("market_state", "")
        entry_bullish_score = market_context.get("bullish_score", "")
        entry_bearish_score = market_context.get("bearish_score", "")
        entry_confidence = market_context.get("confidence", "")
        entry_dominance_percent = market_context.get("dominance_percent", "")
        entry_decision = market_context.get("decision", "")
        entry_reason_log = "\n".join(entry_reasons)
        entry_indicator_breakdown_json = json.dumps(entry_indicator_breakdown(entry_reasons))
    elif action == "SELL" and grade_exit_trade:
        exit_grade, trade_score, grade_reason, hold_time = grade_exit_trade(symbol, qty, price, pnl, market_context)
        last_buy = find_last_buy(symbol)
        entry_grade = last_buy.get("EntryGrade", "") if last_buy else ""
        if entry_grade and exit_grade:
            bot_grade = grade_from_score((grade_score(entry_grade) + grade_score(exit_grade)) / 2)
        else:
            bot_grade = exit_grade or entry_grade
        entry_price = safe_float(last_buy.get("Price"), 0) if last_buy else 0
        sell_price = safe_float(price)
        qty_value = safe_float(qty, 1)
        entry_cost = entry_price * qty_value * 100
        pnl_percent = (safe_float(pnl) / entry_cost) * 100 if entry_cost else ""
        if stop_debug_values:
            stop_values = stop_debug_values(symbol, entry_price, sell_price)
            peak_price = stop_values["peak_price"]
            hard_stop_price = stop_values["hard_stop_price"]
            trailing_stop_price = stop_values["trailing_stop_price"]
            max_drawdown_from_peak_percent = stop_values["drawdown_from_peak_percent"]
            trailing_percent = stop_values["trailing_stop_percent"]
            if not stop_values.get("stop_armed", False):
                trailing_lines = ["Trailing stop inactive.", "Trailing stop price has not reached entry."]
            else:
                trailing_lines = (
                    ["Trailing stop hit.", f"Drawdown {max_drawdown_from_peak_percent:.2f}% >= {trailing_percent:.2f}%."]
                    if max_drawdown_from_peak_percent >= trailing_percent
                    else ["Trailing stop not hit.", f"Drawdown {max_drawdown_from_peak_percent:.2f}% < {trailing_percent:.2f}%."]
                )
            exit_reason = "\n".join(trailing_lines + [str(reason) for reason in (market_context.get("decision_reasons") or [])])

    row = {
        "Time": market_now().strftime("%Y-%m-%d %H:%M:%S"),
        "Action": action,
        "Symbol": symbol,
        "Qty": qty,
        "Price": price,
        "PnL": pnl,
        "Source": source,
        "EntryGrade": entry_grade,
        "LiveGrade": "",
        "ExitGrade": exit_grade,
        "BotGrade": bot_grade,
        "OverallGrade": bot_grade,
        "TradeScore": trade_score,
        "GradeReason": grade_reason,
        "HoldTime": hold_time,
        "PeakPrice": peak_price,
        "HardStopPrice": hard_stop_price,
        "TrailingStopPrice": trailing_stop_price,
        "MaxDrawdownFromPeakPercent": max_drawdown_from_peak_percent,
        "PnLPercent": pnl_percent,
        "ExitReason": exit_reason,
        "EntryPriceSource": entry_price_source,
        "EstimatedEntryPrice": estimated_entry_price,
        "EntryMarketState": entry_market_state,
        "EntryBullishScore": entry_bullish_score,
        "EntryBearishScore": entry_bearish_score,
        "EntryConfidence": entry_confidence,
        "EntryDominancePercent": entry_dominance_percent,
        "EntryDecision": entry_decision,
        "EntryReasonLog": entry_reason_log,
        "EntryIndicatorBreakdown": entry_indicator_breakdown_json
    }

    append_trade_row(TRADES_FILE, row)
    append_trade_row(VISIBLE_TRADES_FILE, row)
    append_trade_row(TRADE_HISTORY_VISIBLE_FILE, row)
    try:
        if load_config and sync_trade_limits_from_file:
            sync_trade_limits_from_file(load_config())
    except:
        pass
    return row


def clear_recent_trades():
    write_trade_header(VISIBLE_TRADES_FILE)


def restore_cleared_trades():
    if not os.path.exists(TRADES_FILE):
        write_trade_header(VISIBLE_TRADES_FILE)
        return True

    ensure_trade_file(TRADES_FILE)
    with open(TRADES_FILE, "r", newline="") as src:
        permanent_history = src.read()

    with open(VISIBLE_TRADES_FILE, "w", newline="") as dst:
        dst.write(permanent_history)

    return True


def source_backup_file(source):
    return BOT_VISIBLE_TRADES_BACKUP_FILE if source.upper() == "BOT" else HUMAN_VISIBLE_TRADES_BACKUP_FILE


def load_visible_trade_rows():
    if os.path.exists(TRADES_FILE):
        ensure_trade_file(TRADES_FILE)
    if os.path.exists(VISIBLE_TRADES_FILE):
        ensure_trade_file(VISIBLE_TRADES_FILE)
    visible_path = VISIBLE_TRADES_FILE if os.path.exists(VISIBLE_TRADES_FILE) else TRADES_FILE

    with open(visible_path, "r", newline="") as f:
        return list(csv.DictReader(f))


def write_visible_trade_rows(rows):
    ensure_trade_file(VISIBLE_TRADES_FILE)
    with open(VISIBLE_TRADES_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in TRADE_COLUMNS})


def clear_visible_trades_by_source(source):
    source = source.upper()
    rows = load_visible_trade_rows()
    cleared_rows = [row for row in rows if (row.get("Source") or "HUMAN").upper() == source]
    remaining_rows = [row for row in rows if (row.get("Source") or "HUMAN").upper() != source]

    with open(source_backup_file(source), "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_COLUMNS)
        writer.writeheader()
        for row in cleared_rows:
            writer.writerow({column: row.get(column, "") for column in TRADE_COLUMNS})

    write_visible_trade_rows(remaining_rows)


def restore_visible_trades_by_source(source):
    source = source.upper()
    backup_file = source_backup_file(source)
    if not os.path.exists(backup_file):
        return False

    ensure_trade_file(backup_file)
    current_rows = load_visible_trade_rows()

    with open(backup_file, "r", newline="") as f:
        backup_rows = list(csv.DictReader(f))

    existing_keys = {tuple(row.get(column, "") for column in TRADE_COLUMNS) for row in current_rows}
    restored_rows = list(current_rows)
    for row in backup_rows:
        key = tuple(row.get(column, "") for column in TRADE_COLUMNS)
        if key not in existing_keys:
            restored_rows.append(row)
            existing_keys.add(key)

    restored_rows.sort(key=lambda row: row.get("Time", ""))
    write_visible_trade_rows(restored_rows)
    return True


def get_recent_trades(limit=10):
    try:
        trades = load_visible_trade_rows()

        if limit is None:
            return trades

        return trades[-limit:]
    except:
        return []


def get_trade_history_trades(limit=None):
    try:
        if os.path.exists(TRADES_FILE):
            ensure_trade_file(TRADES_FILE)
        if os.path.exists(TRADE_HISTORY_VISIBLE_FILE):
            ensure_trade_file(TRADE_HISTORY_VISIBLE_FILE)
        visible_path = TRADE_HISTORY_VISIBLE_FILE if os.path.exists(TRADE_HISTORY_VISIBLE_FILE) else TRADES_FILE

        with open(visible_path, "r", newline="") as f:
            trades = list(csv.DictReader(f))

        if limit is None:
            return trades
        return trades[-limit:]
    except:
        return []


def clear_trade_history_view():
    current_rows = get_trade_history_trades(limit=None)
    with open(TRADE_HISTORY_BACKUP_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_COLUMNS)
        writer.writeheader()
        for row in current_rows:
            writer.writerow({column: row.get(column, "") for column in TRADE_COLUMNS})
    write_trade_header(TRADE_HISTORY_VISIBLE_FILE)


def restore_trade_history_view():
    if not os.path.exists(TRADE_HISTORY_BACKUP_FILE):
        return False
    ensure_trade_file(TRADE_HISTORY_BACKUP_FILE)
    with open(TRADE_HISTORY_BACKUP_FILE, "r", newline="") as src:
        backup_rows = list(csv.DictReader(src))

    with open(TRADE_HISTORY_VISIBLE_FILE, "w", newline="") as dst:
        writer = csv.DictWriter(dst, fieldnames=TRADE_COLUMNS)
        writer.writeheader()
        for row in backup_rows:
            writer.writerow({column: row.get(column, "") for column in TRADE_COLUMNS})
    return True


def read_permanent_trades():
    try:
        ensure_trade_file(TRADES_FILE)
        with open(TRADES_FILE, "r", newline="") as f:
            return list(csv.DictReader(f))
    except:
        return []

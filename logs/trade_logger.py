import csv
import os
from datetime import datetime

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADES_FILE = os.path.join(APP_DIR, "trades.csv")
VISIBLE_TRADES_FILE = os.path.join(APP_DIR, "dashboard_visible_trades.csv")
TRADE_COLUMNS = [
    "Time", "Action", "Symbol", "Qty", "Price", "PnL",
    "Source", "EntryGrade", "LiveGrade", "ExitGrade", "BotGrade",
    "TradeScore", "GradeReason", "HoldTime", "PeakPrice",
    "HardStopPrice", "TrailingStopPrice", "MaxDrawdownFromPeakPercent",
    "PnLPercent", "ExitReason", "EntryPriceSource", "EstimatedEntryPrice"
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

    if action == "BUY" and grade_entry_setup:
        entry_grade, trade_score, grade_reason = grade_entry_setup(market_context)
        bot_grade = entry_grade
    elif action == "SELL" and grade_exit_trade:
        exit_grade, trade_score, grade_reason, hold_time = grade_exit_trade(symbol, qty, price, pnl, market_context)
        bot_grade = exit_grade
        last_buy = find_last_buy(symbol)
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
            trailing_lines = (
                ["Trailing stop hit.", f"Drawdown {max_drawdown_from_peak_percent:.2f}% >= {trailing_percent:.2f}%."]
                if max_drawdown_from_peak_percent >= trailing_percent
                else ["Trailing stop not hit.", f"Drawdown {max_drawdown_from_peak_percent:.2f}% < {trailing_percent:.2f}%."]
            )
            exit_reason = "\n".join(trailing_lines + [str(reason) for reason in (market_context.get("decision_reasons") or [])])

    row = {
        "Time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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
        "EstimatedEntryPrice": estimated_entry_price
    }

    append_trade_row(TRADES_FILE, row)
    append_trade_row(VISIBLE_TRADES_FILE, row)
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


def get_recent_trades(limit=10):
    try:
        if os.path.exists(TRADES_FILE):
            ensure_trade_file(TRADES_FILE)
        if os.path.exists(VISIBLE_TRADES_FILE):
            ensure_trade_file(VISIBLE_TRADES_FILE)
        visible_path = VISIBLE_TRADES_FILE if os.path.exists(VISIBLE_TRADES_FILE) else TRADES_FILE

        with open(visible_path, "r", newline="") as f:
            reader = csv.DictReader(f)
            trades = list(reader)

        if limit is None:
            return trades

        return trades[-limit:]
    except:
        return []


def read_permanent_trades():
    try:
        ensure_trade_file(TRADES_FILE)
        with open(TRADES_FILE, "r", newline="") as f:
            return list(csv.DictReader(f))
    except:
        return []

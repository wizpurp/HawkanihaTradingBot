import re
from flask import Flask, request, redirect, jsonify
import requests
import json
import csv
import os
import threading
import time
from datetime import datetime, timedelta, time as datetime_time



app = Flask(__name__)

TOKEN = "0kkGC4Wj40dAv9GjOO6c7hioOiXM"
ACCOUNT = "VA52467186"
BASE_URL = "https://sandbox.tradier.com/v1"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
TRADES_FILE = os.path.join(APP_DIR, "trades.csv")
CLEARED_TRADES_FILE = os.path.join(APP_DIR, "trades_last_cleared.csv")
BOT_LOCK = threading.Lock()
BOT_STATE = {
    "samples": [],
    "bullish_score": 0,
    "bearish_score": 0,
    "confidence": 0,
    "bullish_percent": 0,
    "bearish_percent": 0,
    "current_signal": "NONE",
    "last_action": "Idle",
    "reason_log": [],
    "trades_today": 0,
    "spent_today": 0,
    "next_call_cost": None,
    "next_put_cost": None,
    "market_state": "NEUTRAL",
    "market_context": {},
    "level_distances": {},
    "historical_levels": {},
    "historical_levels_day": None,
    "historical_levels_symbol": None,
    "running": False,
    "day": None,
    "position_peaks": {}
}


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
    config.setdefault("max_contract_price", 1.00)
    config.setdefault("minimum_confidence", 2)
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
    config["strategy"].setdefault("tick_interval_seconds", 10)
    config["strategy"].setdefault("direction_threshold_percent", 60)

    return config


def save_config(config):
    with open("config.json", "w") as f:
        json.dump(config, f, indent=4)


def log_trade(action, symbol, qty, price="", pnl=""):
    file_exists = os.path.exists(TRADES_FILE)
    needs_header = not file_exists or os.path.getsize(TRADES_FILE) == 0

    with open(TRADES_FILE, "a", newline="") as f:
        writer = csv.writer(f)

        if needs_header:
            writer.writerow(["Time", "Action", "Symbol", "Qty", "Price", "PnL"])

        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            action,
            symbol,
            qty,
            price,
            pnl
        ])


def write_trade_header(path):
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Time", "Action", "Symbol", "Qty", "Price", "PnL"])


def clear_recent_trades():
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, "r", newline="") as src:
            existing = src.read()

        if existing.strip():
            with open(CLEARED_TRADES_FILE, "w", newline="") as backup:
                backup.write(existing)

    write_trade_header(TRADES_FILE)


def restore_cleared_trades():
    if not os.path.exists(CLEARED_TRADES_FILE):
        return False

    with open(CLEARED_TRADES_FILE, "r", newline="") as src:
        previous = src.read()

    with open(TRADES_FILE, "w", newline="") as dst:
        dst.write(previous)

    return True


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


def option_market_is_open():
    now = datetime.now().time()
    return datetime_time(9, 30) <= now <= datetime_time(16, 0)


def submit_and_parse_option_order(option_symbol, qty, action, label):
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


def get_market_quote(symbol):
    try:
        r = requests.get(
            f"{BASE_URL}/markets/quotes",
            params={"symbols": symbol},
            headers=headers()
        )
        if r.status_code != 200:
            return None

        data = r.json()["quotes"]
        if "quote" not in data:
            return None

        quote = data["quote"]
        if isinstance(quote, list):
            return quote[0]
        return quote
    except:
        return None


def get_position():
    try:
        r = requests.get(
            f"{BASE_URL}/accounts/{ACCOUNT}/positions",
            headers=headers()
        )
        if r.status_code != 200:
            return None

        data = r.json()
        print("POSITIONS DATA:", data)

        if data.get("positions") == "null":
            return None

        pos = data["positions"]["position"]
        return pos if isinstance(pos, list) else [pos]
    except:
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

    r = requests.post(
        f"{BASE_URL}/accounts/{ACCOUNT}/orders",
        headers=headers(),
        data=data
    )

    return r.status_code, r.text


def add_bot_reason(message):
    timestamp = datetime.now().strftime("%H:%M:%S")
    line = f"{timestamp} {message}"

    with BOT_LOCK:
        BOT_STATE["last_action"] = message
        BOT_STATE["reason_log"].append(line)
        BOT_STATE["reason_log"] = BOT_STATE["reason_log"][-20:]

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


def parse_clock(value, default_hour=9, default_minute=35):
    try:
        hour, minute = value.split(":")
        return datetime_time(int(hour), int(minute))
    except:
        return datetime_time(default_hour, default_minute)


def is_after_decision_time(config):
    decision = parse_clock(config.get("decision_time", "09:35"))
    return datetime.now().time() >= decision


def sync_trade_limits_from_file(config):
    today = datetime.now().strftime("%Y-%m-%d")
    trades_today = 0
    spent_today = 0.0

    try:
        with open(TRADES_FILE, "r", newline="") as f:
            for row in csv.DictReader(f):
                if not row.get("Time", "").startswith(today):
                    continue
                if row.get("Action") == "BUY":
                    trades_today += 1
                    try:
                        spent_today += float(row.get("Price") or 0) * float(row.get("Qty") or 0) * 100
                    except:
                        pass
    except:
        pass

    with BOT_LOCK:
        if BOT_STATE["day"] != today:
            BOT_STATE["samples"] = []
            BOT_STATE["position_peaks"] = {}
            BOT_STATE["day"] = today
        BOT_STATE["trades_today"] = trades_today
        BOT_STATE["spent_today"] = spent_today

    return trades_today, spent_today


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
    today = datetime.now().date().isoformat()

    with BOT_LOCK:
        if (
            BOT_STATE["historical_levels_day"] == today
            and BOT_STATE["historical_levels_symbol"] == symbol
            and BOT_STATE["historical_levels"]
        ):
            return dict(BOT_STATE["historical_levels"])

    try:
        end = datetime.now().date()
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
        "level_distances": {key: None for key in levels},
        "current_pl": 0,
        "distance_to_trailing_stop": None,
        "current_range_size": 0,
        "bullish_score": 0,
        "bearish_score": 0,
        "confidence": 0,
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

    now = datetime.now()
    today_samples = [sample for sample in samples if sample["time"].date() == now.date()]
    premarket_samples = [sample for sample in today_samples if sample["time"].time() < datetime_time(9, 30)]
    last_hour_samples = [sample for sample in today_samples if sample["time"] >= now - timedelta(hours=1)]
    opening_samples = [
        sample for sample in today_samples
        if datetime_time(9, 30) <= sample["time"].time() <= datetime_time(9, 35)
    ]

    history_levels = get_historical_levels(symbol)
    today_prices = [sample["price"] for sample in today_samples] or prices
    premarket_prices = [sample["price"] for sample in premarket_samples]
    last_hour_prices = [sample["price"] for sample in last_hour_samples] or prices[-1:]
    opening_prices = [sample["price"] for sample in opening_samples] or prices[:min(len(prices), 30)]

    levels = {
        "previous_week_high": history_levels.get("previous_week_high"),
        "previous_week_low": history_levels.get("previous_week_low"),
        "previous_day_high": history_levels.get("previous_day_high"),
        "previous_day_low": history_levels.get("previous_day_low"),
        "today_high": max(today_prices),
        "today_low": min(today_prices),
        "today_open": today_prices[0],
        "premarket_high": max(premarket_prices) if premarket_prices else None,
        "premarket_low": min(premarket_prices) if premarket_prices else None,
        "last_hour_high": max(last_hour_prices),
        "last_hour_low": min(last_hour_prices),
        "opening_range_high": max(opening_prices),
        "opening_range_low": min(opening_prices)
    }

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

    nearest_resistance = min(
        [value - current_price for key, value in levels.items() if "high" in key and value is not None and value >= current_price],
        default=None
    )
    nearest_support = min(
        [current_price - value for key, value in levels.items() if "low" in key and value is not None and value <= current_price],
        default=None
    )

    current_range_size = levels["today_high"] - levels["today_low"]
    stagnant_range = current_price * 0.001 if current_price else 0

    if nearest_resistance is not None and nearest_resistance > current_price * 0.001:
        bullish_score, bearish_score = add_score(
            reasons, "bullish", "Not near major resistance", bullish_score, bearish_score
        )
    if nearest_support is not None and nearest_support > current_price * 0.001:
        bullish_score, bearish_score = add_score(
            reasons, "bearish", "Not near major support", bullish_score, bearish_score
        )

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
            entry_price = cost_basis / qty / 100 if len(position_symbol) > 6 else cost_basis / qty
            multiplier = 100 if len(position_symbol) > 6 else 1
            current_pl = (position_price - entry_price) * qty * multiplier
            with BOT_LOCK:
                peak = BOT_STATE["position_peaks"].get(position_symbol, entry_price)
            trailing_stop_price = peak * (1 - (float(s.get("trailing_stop_percent", 15)) / 100))
            distance_to_trailing_stop = position_price - trailing_stop_price
            if current_pl > 0:
                reasons.append("Current P/L supports holding")

    minimum_signals = int(e.get("minimum_signals", 3))
    confidence = abs(bullish_score - bearish_score)
    market_state = "NEUTRAL"

    if confidence <= 1:
        market_state = "CHOPPY"
        reasons.append(f"Confidence {confidence} is too low")
    elif current_range_size <= stagnant_range:
        market_state = "STAGNANT"
        reasons.append("Current range is stagnant")
    elif bullish_percent < 55 and bearish_percent < 55 and confidence <= 1:
        market_state = "CHOPPY"
        reasons.append("Ticks and scores are choppy")
    elif bullish_score >= minimum_signals and bullish_score > bearish_score:
        market_state = "BULLISH"
    elif bearish_score >= minimum_signals and bearish_score > bullish_score:
        market_state = "BEARISH"

    current_signal = "CALL" if market_state == "BULLISH" else "PUT" if market_state == "BEARISH" else "NONE"

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
        "level_distances": level_distances,
        "current_pl": current_pl,
        "distance_to_trailing_stop": distance_to_trailing_stop,
        "current_range_size": current_range_size,
        "bullish_score": bullish_score,
        "bearish_score": bearish_score,
        "confidence": confidence,
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
    minimum_signals = int(config["entry_rules"].get("minimum_signals", 3))
    minimum_confidence = int(config.get("minimum_confidence", 2))
    hard_stop_percent = float(config["strategy"].get("hard_stop_percent", 20))
    trailing_stop_percent = float(config["strategy"].get("trailing_stop_percent", 15))
    market_state = market_context["market_state"]
    confidence = market_context.get("confidence", 0)

    if positions:
        pos = positions[0]
        symbol = pos.get("symbol", "")
        qty = float(pos.get("quantity", 0) or 0)
        cost_basis = float(pos.get("cost_basis", 0) or 0)
        quote = get_market_quote(symbol)
        current_price = get_quote_price(quote)
        entry_price = cost_basis / qty / 100 if len(symbol) > 6 and qty else cost_basis / qty if qty else 0

        with BOT_LOCK:
            peak = max(BOT_STATE["position_peaks"].get(symbol, entry_price), current_price or entry_price)
            BOT_STATE["position_peaks"][symbol] = peak

        pnl_percent = ((current_price - entry_price) / entry_price) * 100 if current_price and entry_price else 0
        trailing_drawdown = ((peak - current_price) / peak) * 100 if current_price and peak else 0
        is_call = "C" in symbol[-9:]

        if pnl_percent <= -hard_stop_percent:
            return "SELL", ["Hard stop hit", f"P/L {pnl_percent:.2f}%"]
        if trailing_drawdown >= trailing_stop_percent:
            return "SELL", ["Trailing stop hit", f"Drawdown {trailing_drawdown:.2f}%"]
        if is_call and market_state == "BEARISH" and market_context["bearish_score"] >= minimum_signals:
            return "SELL", ["Full market context flipped bearish"] + market_context["reasons"]
        if not is_call and market_state == "BULLISH" and market_context["bullish_score"] >= minimum_signals:
            return "SELL", ["Full market context flipped bullish"] + market_context["reasons"]

        return "HOLD", ["Holding because market context has not confirmed exit"] + market_context["reasons"]

    if market_state == "BULLISH" and confidence >= minimum_confidence:
        return "BUY CALL", market_context["reasons"]
    if market_state == "BEARISH" and confidence >= minimum_confidence:
        return "BUY PUT", market_context["reasons"]
    if market_state in ["BULLISH", "BEARISH"]:
        return "DO NOTHING", [f"Confidence {confidence} below required {minimum_confidence}"] + market_context["reasons"]
    if market_state == "STAGNANT":
        return "DO NOTHING", ["Market stagnant"] + market_context["reasons"]
    if market_state == "CHOPPY":
        return "DO NOTHING", ["Market choppy"] + market_context["reasons"]

    return "DO NOTHING", ["Market neutral"] + market_context["reasons"]


def calculate_surfer_signal(config, positions=None):
    market_context = build_market_context(config, positions)
    decision, decision_reasons = decide_surfer_action(config, positions or [], market_context)
    market_context["decision"] = decision
    market_context["decision_reasons"] = decision_reasons
    return market_context


def format_market_reason_log(market_context):
    lines = [
        f"Market State: {market_context['market_state']}",
        f"Bullish Score: {market_context['bullish_score']}",
        f"Bearish Score: {market_context['bearish_score']}",
        f"Confidence: {market_context.get('confidence', 0)}",
        f"Decision: {market_context.get('decision', 'DO NOTHING')}"
    ]
    lines.extend(market_context.get("decision_reasons") or market_context.get("reasons", []))
    return lines[-20:]


def update_bot_signal_state(signal, call_cost=None, put_cost=None):
    with BOT_LOCK:
        BOT_STATE["bullish_score"] = signal["bullish_score"]
        BOT_STATE["bearish_score"] = signal["bearish_score"]
        BOT_STATE["confidence"] = signal["confidence"]
        BOT_STATE["bullish_percent"] = signal["bullish_percent"]
        BOT_STATE["bearish_percent"] = signal["bearish_percent"]
        BOT_STATE["current_signal"] = signal["current_signal"]
        BOT_STATE["next_call_cost"] = call_cost
        BOT_STATE["next_put_cost"] = put_cost
        BOT_STATE["market_state"] = signal["market_state"]
        BOT_STATE["market_context"] = signal
        BOT_STATE["level_distances"] = signal["level_distances"]
        BOT_STATE["reason_log"] = format_market_reason_log(signal)


def log_accepted_trade(action, symbol, qty, price, pnl=""):
    log_trade(action, symbol, qty, price, pnl)
    add_bot_reason(f"{action} logged {symbol} qty {qty} price {price}")


def try_surfer_entry(config, positions, market_context, call, put):
    if positions:
        add_bot_reason("SIGNAL no entry: already holding one position")
        return

    if not is_after_decision_time(config):
        add_bot_reason("SIGNAL no entry: waiting for decision_time")
        return

    trades_today, spent_today = sync_trade_limits_from_file(config)
    max_trades = int(config["entry_rules"].get("max_trades_per_day", 10))

    if trades_today >= max_trades:
        add_bot_reason("SIGNAL no entry: max trades reached")
        return

    contracts = int(config.get("contracts", 1))
    bot_budget = float(config.get("bot_budget", 100))
    max_contract_price = float(config.get("max_contract_price", 1))
    decision = market_context.get("decision", "DO NOTHING")
    side = "CALL" if decision == "BUY CALL" else "PUT" if decision == "BUY PUT" else "NONE"
    contract = call if decision == "BUY CALL" else put if decision == "BUY PUT" else None

    if not contract:
        add_bot_reason(f"SIGNAL no entry: {decision}")
        return

    ask = get_option_trade_price(contract)
    if ask is None:
        add_bot_reason(f"BUDGET CHECK skipped {side}: no option price")
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
        return

    if real_cost > bot_budget or spent_today + real_cost > bot_budget:
        add_bot_reason(f"BUDGET CHECK skipped {side}: cost {real_cost:.2f} exceeds budget")
        return

    if not option_market_is_open():
        add_bot_reason("ENTRY skipped: options market is closed")
        return

    ok, order_status, status, text = submit_and_parse_option_order(
        contract["symbol"],
        contracts,
        "buy_to_open",
        "ENTRY"
    )

    if ok:
        log_accepted_trade("BUY", contract["symbol"], contracts, ask)
        add_bot_reason(f"SIGNAL entered {side}: {'; '.join(market_context.get('decision_reasons', []))}")
    else:
        add_bot_reason(f"SIGNAL entry rejected or not accepted: {order_status}")


def try_surfer_exit(config, positions, market_context):
    if not positions:
        return

    decision = market_context.get("decision", "DO NOTHING")
    if decision != "SELL":
        add_bot_reason(f"EXIT hold: {'; '.join(market_context.get('decision_reasons', []))}")
        return

    for pos in positions:
        symbol = pos.get("symbol", "")
        if len(symbol) <= 6:
            continue

        qty = int(float(pos.get("quantity", 1)))
        cost_basis = float(pos.get("cost_basis", 0) or 0)
        entry_price = cost_basis / qty / 100 if qty else 0
        quote = get_market_quote(symbol)
        current_price = get_quote_price(quote) or entry_price

        exit_reasons = market_context.get("decision_reasons", [])
        reason = "; ".join(exit_reasons)
        print("EXIT REASON:", reason)

        if not option_market_is_open():
            add_bot_reason("EXIT skipped: options market is closed")
            return

        ok, order_status, status, text = submit_and_parse_option_order(
            symbol,
            qty,
            "sell_to_close",
            "EXIT"
        )

        if ok:
            pnl = (current_price - entry_price) * qty * 100
            log_accepted_trade("SELL", symbol, qty, current_price, pnl)
            add_bot_reason(f"EXIT sold {symbol}: {reason}")
            with BOT_LOCK:
                BOT_STATE["position_peaks"].pop(symbol, None)
        else:
            add_bot_reason(f"EXIT rejected or not accepted: {order_status}")


def surfer_bot_tick():
    config = load_config()
    sync_trade_limits_from_file(config)

    if not config.get("bot_enabled") or config.get("strategy_mode") != "SURFER":
        return

    symbol = config.get("symbol", "SPY")
    quote = get_market_quote(symbol)
    price = get_quote_price(quote)

    print("BOT TICK")
    print("symbol:", symbol)
    print("price:", price)

    if price is None:
        add_bot_reason("BOT TICK skipped: quote unavailable")
        return

    volume = None
    try:
        volume = float(quote.get("volume")) if quote.get("volume") is not None else None
    except:
        volume = None

    with BOT_LOCK:
        BOT_STATE["samples"].append({
            "time": datetime.now(),
            "price": price,
            "volume": volume
        })
        BOT_STATE["samples"] = BOT_STATE["samples"][-500:]

    call = select_atm_contract(symbol, "CALL")
    put = select_atm_contract(symbol, "PUT")
    call_price = get_option_trade_price(call)
    put_price = get_option_trade_price(put)
    contracts = int(config.get("contracts", 1))
    call_cost = call_price * 100 * contracts if call_price is not None else None
    put_cost = put_price * 100 * contracts if put_price is not None else None
    positions = get_position()
    signal = calculate_surfer_signal(config, positions)
    update_bot_signal_state(signal, call_cost, put_cost)

    print("DIRECTION")
    print("green_percent:", signal["bullish_percent"])
    print("red_percent:", signal["bearish_percent"])
    print("SIGNAL")
    print("bullish_score:", signal["bullish_score"])
    print("bearish_score:", signal["bearish_score"])
    print("current_signal:", signal["current_signal"])
    print("market_state:", signal["market_state"])
    print("decision:", signal["decision"])

    try_surfer_exit(config, positions, signal)
    positions = get_position()
    try_surfer_entry(config, positions, signal, call, put)


def surfer_bot_loop():
    with BOT_LOCK:
        if BOT_STATE["running"]:
            return
        BOT_STATE["running"] = True

    while True:
        try:
            config = load_config()
            interval = int(config["strategy"].get("tick_interval_seconds", 10))
            surfer_bot_tick()
            time.sleep(max(1, interval))
        except Exception as exc:
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
        entry_price = cost_basis / qty / 100 if is_option and qty else cost_basis / qty if qty else 0
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
        pnl = (sell_price - entry_price) * qty * multiplier

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
            price = get_quote_price(quote) or ""
            log_trade("BUY", option_symbol, qty, price)

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
            log_trade("BUY", contract["symbol"], config["contracts"], get_option_trade_price(contract) or "")
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
            log_trade("BUY", contract["symbol"], config["contracts"], get_option_trade_price(contract) or "")
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
    config["max_contract_price"] = float(request.form.get("max_contract_price", 1))
    config["minimum_confidence"] = int(request.form.get("minimum_confidence", 2))

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
    s["tick_interval_seconds"] = int(request.form.get("tick_interval_seconds", 10))
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

def get_recent_trades():
    try:
        with open(TRADES_FILE, "r", newline="") as f:
            reader = csv.DictReader(f)
            return list(reader)[-10:]
    except:
        return []


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
        entry_price = cost_basis / qty / 100
    else:
        entry_price = cost_basis / qty

    if current_price is None:
        current_price = entry_price

    if is_option:
        current_value = current_price * qty * 100
    else:
        current_value = current_price * qty

    pnl = current_value - cost_basis

    if cost_basis != 0:
        pnl_percent = (pnl / cost_basis) * 100
    else:
        pnl_percent = 0

    return {
        "symbol": symbol,
        "qty": qty,
        "entry_price": entry_price,
        "current_price": current_price,
        "cost_basis": cost_basis,
        "current_value": current_value,
        "pnl": pnl,
        "pnl_percent": pnl_percent,
        "is_option": is_option
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
            "current_signal": BOT_STATE["current_signal"],
            "last_action": BOT_STATE["last_action"],
            "trades_today": BOT_STATE["trades_today"],
            "spent_today": BOT_STATE["spent_today"],
            "next_call_cost": BOT_STATE["next_call_cost"],
            "next_put_cost": BOT_STATE["next_put_cost"],
            "market_state": BOT_STATE["market_state"],
            "market_context": dict(BOT_STATE["market_context"]),
            "level_distances": dict(BOT_STATE["level_distances"]),
            "reason_log": list(BOT_STATE["reason_log"])
        }

    call_price = get_option_trade_price(call)
    put_price = get_option_trade_price(put)
    call_cost = bot_snapshot["next_call_cost"]
    put_cost = bot_snapshot["next_put_cost"]

    if call_cost is None and call_price is not None:
        call_cost = call_price * 100 * contracts
    if put_cost is None and put_price is not None:
        put_cost = put_price * 100 * contracts

    reason_log_html = "<br>".join(bot_snapshot["reason_log"][-10:]) or "No bot actions yet."
    market_context = bot_snapshot["market_context"] or empty_market_context(get_quote_price(quote))
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
Bot Budget: {fmt_money(config.get("bot_budget", 100))}<br>
Spent Today: {fmt_money(bot_snapshot["spent_today"])}<br>
Cost of Next ATM CALL: {fmt_money(call_cost)}<br>
Cost of Next ATM PUT: {fmt_money(put_cost)}<br>
Market State: {bot_snapshot["market_state"]}<br>
Bullish Score: {bot_snapshot["bullish_score"]}<br>
Bearish Score: {bot_snapshot["bearish_score"]}<br>
Confidence: {bot_snapshot["confidence"]}<br>
Current Signal: {bot_snapshot["current_signal"]}<br>
Last Bot Action: {bot_snapshot["last_action"]}<br>
Trades Today: {bot_snapshot["trades_today"]}<br>
Current P/L: {fmt_money(market_context.get("current_pl"))}<br>
Distance to Trailing Stop: {fmt_money(market_context.get("distance_to_trailing_stop"))}<br>
Current Range Size: {fmt_money(market_context.get("current_range_size"))}<br>
<br>
Market Structure:<br>
<div class="item">{level_rows}</div>
<br>
Bot Reason Log:<br>
<div class="item">{reason_log_html}</div>
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
<div class="item"><div class="label">Symbol</div><div class="value">{quote.get("symbol")}</div></div>
<div class="item"><div class="label">Last</div><div class="value">{fmt_money(quote.get("last"))}</div></div>
<div class="item"><div class="label">Bid</div><div class="value">{fmt_money(quote.get("bid"))}</div></div>
<div class="item"><div class="label">Ask</div><div class="value">{fmt_money(quote.get("ask"))}</div></div>
<div class="item"><div class="label">Volume</div><div class="value">{fmt_int(quote.get("volume"))}</div></div>
<div class="item"><div class="label">Avg Volume</div><div class="value">{fmt_int(quote.get("average_volume"))}</div></div>
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
Direction Tick: {s.get("tick_interval_seconds")} sec<br>
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
<h2>Current Position</h2>

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
Cost Basis: {fmt_money(pl["cost_basis"])}<br>
Current Value: {fmt_money(pl["current_value"])}<br>
<span style="color:{pnl_color}; font-weight:bold;">
P/L: {fmt_money(pl["pnl"])}<br>
P/L %: {pl["pnl_percent"]:.2f}%
</span><br>
Status: OPEN
</div>
<br>
"""
    else:
        html += "No Position"

    html += """
</div>
"""

    recent = get_recent_trades()

    html += """
<div class="card">
<h2>Recent Trades</h2>
<form method="POST" action="/clear-trades" style="display:inline;">
<button type="submit" class="red">Clear Recent Trades</button>
</form>
<form method="POST" action="/restore-cleared-trades" style="display:inline;">
<button type="submit" class="yellow">Undo Clear</button>
</form>
<br><br>
"""

    if recent:
        for trade in reversed(recent):
            html += f"""
<b>{trade.get("Action", "")}</b><br>
{trade.get("Symbol", "")}<br>
Qty: {trade.get("Qty", "")}<br>
Price: ${trade.get("Price", "")}<br>
PnL: {trade.get("PnL", "")}<br>
Time: {trade.get("Time", "")}<br><br>
"""
    else:
        html += "No trades yet."

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

Bot Budget $:
<input type="number" step="0.01" name="bot_budget" value="{config.get("bot_budget", 100)}"><br>

Max Contract Price:
<input type="number" step="0.01" name="max_contract_price" value="{config.get("max_contract_price", 1)}"><br>

Minimum Confidence:
<input type="number" name="minimum_confidence" value="{config.get("minimum_confidence", 2)}"><br>

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

<h3>Risk</h3>

Hard Stop %:
<input type="number" step="0.1" name="hard_stop_percent" value="{s.get("hard_stop_percent")}"><br>

Trailing Stop %:
<input type="number" step="0.1" name="trailing_stop_percent" value="{s.get("trailing_stop_percent")}"><br>

<h3>Opening Direction</h3>

Tick Interval Seconds:
<input type="number" name="tick_interval_seconds" value="{s.get("tick_interval_seconds")}"><br>

Direction Threshold %:
<input type="number" step="0.1" name="direction_threshold_percent" value="{s.get("direction_threshold_percent")}"><br>

Bot Scan Every Seconds:
<input type="number" name="interval_seconds" value="{config.get("scanner", {}).get("interval_seconds", 60)}"><br><br>

<button type="submit">Save Settings</button>

</form>
</div>

<script>
const CURRENT_SYMBOL = "{symbol}";

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

from __future__ import annotations

import csv
import os
import uuid
from datetime import datetime

CHECKPOINT_SECONDS = (15, 30, 60, 120, 300)
OBSERVATION_SECONDS = 300
SHADOW_CANDIDATES_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "shadow_candidates.csv",
)

BASE_COLUMNS = [
    "candidate_id",
    "fingerprint",
    "status",
    "created_epoch",
    "updated_epoch",
    "completed_epoch",
    "timestamp",
    "symbol",
    "direction",
    "spy_price",
    "option_symbol",
    "option_bid",
    "option_ask",
    "option_midpoint",
    "starting_option_price",
    "option_price_source",
    "bid_ask_spread_dollars",
    "bid_ask_spread_percent",
    "bullish_score",
    "bearish_score",
    "confidence",
    "dominance_percent",
    "market_state",
    "ema_state",
    "ema_value",
    "ema_slope",
    "ma_state",
    "ma_value",
    "macd_state",
    "macd_value",
    "macd_signal_value",
    "macd_histogram",
    "macd_histogram_slope",
    "vwap_value",
    "distance_from_vwap_dollars",
    "distance_from_vwap_percent",
    "volume_confirmation",
    "green_ticks",
    "red_ticks",
    "green_percent",
    "red_percent",
    "opening_range_high",
    "opening_range_low",
    "distance_beyond_opening_range",
    "previous_day_high",
    "previous_day_low",
    "distance_from_relevant_previous_day_level",
    "previous_week_high",
    "previous_week_low",
    "distance_from_relevant_previous_week_level",
    "time_of_day",
    "highest_option_price_observed",
    "lowest_option_price_observed",
    "mfe_percent",
    "mae_percent",
    "first_plus_3_epoch",
    "first_minus_3_epoch",
    "first_plus_5_epoch",
    "first_minus_5_epoch",
    "first_plus_8_epoch",
    "first_minus_4_epoch",
    "first_plus_10_epoch",
    "first_minus_5_for_10_epoch",
    "hit_plus_3_before_minus_3",
    "hit_plus_5_before_minus_5",
    "hit_plus_8_before_minus_4",
    "hit_plus_10_before_minus_5",
    "maximum_favorable_excursion_5m",
    "maximum_adverse_excursion_5m",
    "classification",
]

CHECKPOINT_COLUMNS = []
for checkpoint in CHECKPOINT_SECONDS:
    CHECKPOINT_COLUMNS.extend([
        f"checkpoint_{checkpoint}_epoch",
        f"checkpoint_{checkpoint}_option_price",
        f"checkpoint_{checkpoint}_spy_price",
        f"checkpoint_{checkpoint}_mfe_percent",
        f"checkpoint_{checkpoint}_mae_percent",
    ])

COLUMNS = BASE_COLUMNS + CHECKPOINT_COLUMNS


def safe_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value, default=0):
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def iso_timestamp(now):
    if isinstance(now, datetime):
        return now.isoformat()
    return datetime.fromtimestamp(float(now)).isoformat()


def option_midpoint(contract_or_quote):
    bid = safe_float((contract_or_quote or {}).get("bid"))
    ask = safe_float((contract_or_quote or {}).get("ask"))
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return (bid + ask) / 2, "MIDPOINT"
    last = safe_float((contract_or_quote or {}).get("last"))
    if last is not None and last > 0:
        return last, "LAST"
    if ask is not None and ask > 0:
        return ask, "ASK"
    if bid is not None and bid > 0:
        return bid, "BID"
    return None, "NONE"


def load_candidates(path=SHADOW_CANDIDATES_FILE):
    if not os.path.exists(path):
        return []
    with open(path, "r", newline="", encoding="utf-8") as handle:
        return [normalize_row(row) for row in csv.DictReader(handle)]


def save_candidates(rows, path=SHADOW_CANDIDATES_FILE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in COLUMNS})
    return rows


def normalize_row(row):
    normalized = {column: (row or {}).get(column, "") for column in COLUMNS}
    return normalized


def direction_from_decision(signal):
    decision = str((signal or {}).get("decision") or "").upper()
    if decision == "BUY CALL":
        return "CALL"
    if decision == "BUY PUT":
        return "PUT"
    current_signal = str((signal or {}).get("current_signal") or "").upper()
    return current_signal if current_signal in {"CALL", "PUT"} else "NONE"


def setup_fingerprint(signal, option_symbol):
    return "|".join([
        str((signal or {}).get("symbol") or "SPY").upper(),
        direction_from_decision(signal),
        str(option_symbol or "").upper(),
        str(safe_int((signal or {}).get("bullish_score"))),
        str(safe_int((signal or {}).get("bearish_score"))),
        str(safe_int((signal or {}).get("confidence"))),
        str(round(safe_float((signal or {}).get("dominance_percent"), 0) or 0, 1)),
        str((signal or {}).get("market_state") or "UNKNOWN"),
    ])


def spread_fields(contract_or_quote):
    bid = safe_float((contract_or_quote or {}).get("bid"))
    ask = safe_float((contract_or_quote or {}).get("ask"))
    midpoint, source = option_midpoint(contract_or_quote)
    spread = ask - bid if bid is not None and ask is not None else None
    spread_percent = (spread / midpoint) * 100 if midpoint and spread is not None else None
    return bid, ask, midpoint, source, spread, spread_percent


def relevant_level_distance(direction, price, high, low):
    price = safe_float(price)
    if price is None:
        return ""
    if direction == "CALL" and high is not None:
        return price - high
    if direction == "PUT" and low is not None:
        return price - low
    return ""


def opening_range_distance(direction, price, high, low):
    return relevant_level_distance(direction, price, high, low)


def create_candidate(signal, contract, now_epoch, now_dt):
    symbol = str((signal or {}).get("symbol") or "SPY").upper()
    direction = direction_from_decision(signal)
    option_symbol = (contract or {}).get("symbol") or ""
    bid, ask, midpoint, source, spread, spread_percent = spread_fields(contract)
    levels = (signal or {}).get("levels") or {}
    ticks = (signal or {}).get("tick_statistics") or {}
    spy_price = safe_float((signal or {}).get("price"))
    vwap = safe_float((signal or {}).get("vwap"))
    previous_day_high = safe_float(levels.get("previous_day_high"))
    previous_day_low = safe_float(levels.get("previous_day_low"))
    previous_week_high = safe_float(levels.get("previous_week_high"))
    previous_week_low = safe_float(levels.get("previous_week_low"))
    opening_range_high = safe_float(levels.get("opening_range_high"))
    opening_range_low = safe_float(levels.get("opening_range_low"))
    distance_from_vwap_dollars = spy_price - vwap if spy_price is not None and vwap is not None else ""
    distance_from_vwap_percent = (distance_from_vwap_dollars / vwap) * 100 if vwap else ""
    candidate_id = f"shadow-{uuid.uuid4().hex}"
    row = {column: "" for column in COLUMNS}
    row.update({
        "candidate_id": candidate_id,
        "fingerprint": setup_fingerprint(dict(signal or {}, symbol=symbol), option_symbol),
        "status": "ACTIVE",
        "created_epoch": now_epoch,
        "updated_epoch": now_epoch,
        "timestamp": iso_timestamp(now_dt),
        "symbol": symbol,
        "direction": direction,
        "spy_price": spy_price if spy_price is not None else "",
        "option_symbol": option_symbol,
        "option_bid": bid if bid is not None else "",
        "option_ask": ask if ask is not None else "",
        "option_midpoint": midpoint if midpoint is not None else "",
        "option_price_source": source,
        "bid_ask_spread_dollars": spread if spread is not None else "",
        "bid_ask_spread_percent": spread_percent if spread_percent is not None else "",
        "bullish_score": safe_int((signal or {}).get("bullish_score")),
        "bearish_score": safe_int((signal or {}).get("bearish_score")),
        "confidence": safe_int((signal or {}).get("confidence")),
        "dominance_percent": safe_float((signal or {}).get("dominance_percent"), 0) or 0,
        "market_state": (signal or {}).get("market_state") or "UNKNOWN",
        "ema_state": (signal or {}).get("ema_state") or "NEUTRAL",
        "ema_value": safe_float((signal or {}).get("ema_value"), ""),
        "ema_slope": safe_float((signal or {}).get("ema_slope"), ""),
        "ma_state": (signal or {}).get("ma_state") or "NEUTRAL",
        "ma_value": safe_float((signal or {}).get("ma_value"), ""),
        "macd_state": (signal or {}).get("macd_state") or "NEUTRAL",
        "macd_value": safe_float((signal or {}).get("macd_value"), ""),
        "macd_signal_value": safe_float((signal or {}).get("macd_signal_value"), ""),
        "macd_histogram": safe_float((signal or {}).get("macd_histogram"), ""),
        "macd_histogram_slope": safe_float((signal or {}).get("macd_histogram_slope"), ""),
        "vwap_value": vwap if vwap is not None else "",
        "distance_from_vwap_dollars": distance_from_vwap_dollars,
        "distance_from_vwap_percent": distance_from_vwap_percent,
        "volume_confirmation": (signal or {}).get("volume_state") or "NEUTRAL",
        "green_ticks": ticks.get("green_ticks", ""),
        "red_ticks": ticks.get("red_ticks", ""),
        "green_percent": ticks.get("green_percent", (signal or {}).get("bullish_percent", "")),
        "red_percent": ticks.get("red_percent", (signal or {}).get("bearish_percent", "")),
        "opening_range_high": opening_range_high if opening_range_high is not None else "",
        "opening_range_low": opening_range_low if opening_range_low is not None else "",
        "distance_beyond_opening_range": opening_range_distance(direction, spy_price, opening_range_high, opening_range_low),
        "previous_day_high": previous_day_high if previous_day_high is not None else "",
        "previous_day_low": previous_day_low if previous_day_low is not None else "",
        "distance_from_relevant_previous_day_level": relevant_level_distance(direction, spy_price, previous_day_high, previous_day_low),
        "previous_week_high": previous_week_high if previous_week_high is not None else "",
        "previous_week_low": previous_week_low if previous_week_low is not None else "",
        "distance_from_relevant_previous_week_level": relevant_level_distance(direction, spy_price, previous_week_high, previous_week_low),
        "time_of_day": now_dt.strftime("%H:%M:%S") if hasattr(now_dt, "strftime") else "",
        "highest_option_price_observed": midpoint if midpoint is not None else "",
        "lowest_option_price_observed": midpoint if midpoint is not None else "",
        "mfe_percent": 0,
        "mae_percent": 0,
    })
    return row


def current_option_price(row, quote):
    price, source = option_midpoint(quote)
    if price is None:
        return None, source
    return price, source


def percent_change(start, current):
    start = safe_float(start)
    current = safe_float(current)
    if not start or current is None:
        return 0.0
    return ((current - start) / start) * 100


def update_threshold_hits(row, now_epoch, mfe_percent, mae_percent):
    if mfe_percent >= 3 and not row.get("first_plus_3_epoch"):
        row["first_plus_3_epoch"] = now_epoch
    if mae_percent >= 3 and not row.get("first_minus_3_epoch"):
        row["first_minus_3_epoch"] = now_epoch
    if mfe_percent >= 5 and not row.get("first_plus_5_epoch"):
        row["first_plus_5_epoch"] = now_epoch
    if mae_percent >= 5 and not row.get("first_minus_5_epoch"):
        row["first_minus_5_epoch"] = now_epoch
    if mfe_percent >= 8 and not row.get("first_plus_8_epoch"):
        row["first_plus_8_epoch"] = now_epoch
    if mae_percent >= 4 and not row.get("first_minus_4_epoch"):
        row["first_minus_4_epoch"] = now_epoch
    if mfe_percent >= 10 and not row.get("first_plus_10_epoch"):
        row["first_plus_10_epoch"] = now_epoch
    if mae_percent >= 5 and not row.get("first_minus_5_for_10_epoch"):
        row["first_minus_5_for_10_epoch"] = now_epoch


def hit_before(row, plus_key, minus_key):
    plus = safe_float(row.get(plus_key))
    minus = safe_float(row.get(minus_key))
    return bool(plus is not None and (minus is None or plus <= minus))


def classify_candidate(row):
    mfe = safe_float(row.get("mfe_percent"), 0) or 0
    mae = safe_float(row.get("mae_percent"), 0) or 0
    if mfe >= 8 and mae < 4:
        return "STRONG_CONTINUATION"
    if mfe >= 5:
        return "WEAK_CONTINUATION"
    if mae >= 5 and mfe < 3:
        return "IMMEDIATE_FAILURE"
    if mae >= 5 and mfe >= 5:
        return "REVERSAL"
    return "CHOP"


def finalize_candidate(row, now_epoch):
    row["status"] = "COMPLETED"
    row["completed_epoch"] = now_epoch
    row["maximum_favorable_excursion_5m"] = row.get("mfe_percent", 0)
    row["maximum_adverse_excursion_5m"] = row.get("mae_percent", 0)
    row["hit_plus_3_before_minus_3"] = hit_before(row, "first_plus_3_epoch", "first_minus_3_epoch")
    row["hit_plus_5_before_minus_5"] = hit_before(row, "first_plus_5_epoch", "first_minus_5_epoch")
    row["hit_plus_8_before_minus_4"] = hit_before(row, "first_plus_8_epoch", "first_minus_4_epoch")
    row["hit_plus_10_before_minus_5"] = hit_before(row, "first_plus_10_epoch", "first_minus_5_for_10_epoch")
    row["classification"] = classify_candidate(row)
    return row


def update_candidate(row, quote, spy_price, now_epoch):
    row = normalize_row(row)
    created_epoch = safe_float(row.get("created_epoch"), now_epoch) or now_epoch
    age = now_epoch - created_epoch
    option_price, source = current_option_price(row, quote)
    if option_price is not None:
        row["option_price_source"] = source
        row["updated_epoch"] = now_epoch
        row["option_bid"] = safe_float((quote or {}).get("bid"), row.get("option_bid"))
        row["option_ask"] = safe_float((quote or {}).get("ask"), row.get("option_ask"))
        row["option_midpoint"] = option_price
        highest = max(safe_float(row.get("highest_option_price_observed"), option_price) or option_price, option_price)
        lowest = min(safe_float(row.get("lowest_option_price_observed"), option_price) or option_price, option_price)
        row["highest_option_price_observed"] = highest
        row["lowest_option_price_observed"] = lowest
        start = safe_float(row.get("option_midpoint"), option_price) or option_price
        original_start = safe_float(row.get("checkpoint_0_option_price"), None)
        start_price = safe_float(row.get("starting_option_price"), None) or safe_float(row.get("option_midpoint"), option_price) or option_price
        if not row.get("starting_option_price"):
            row["starting_option_price"] = start_price
        mfe_percent = max(0.0, percent_change(start_price, highest))
        mae_percent = max(0.0, -percent_change(start_price, lowest))
        row["mfe_percent"] = mfe_percent
        row["mae_percent"] = mae_percent
        update_threshold_hits(row, now_epoch, mfe_percent, mae_percent)
        for checkpoint in CHECKPOINT_SECONDS:
            if age >= checkpoint and not row.get(f"checkpoint_{checkpoint}_epoch"):
                row[f"checkpoint_{checkpoint}_epoch"] = now_epoch
                row[f"checkpoint_{checkpoint}_option_price"] = option_price
                row[f"checkpoint_{checkpoint}_spy_price"] = spy_price if spy_price is not None else ""
                row[f"checkpoint_{checkpoint}_mfe_percent"] = mfe_percent
                row[f"checkpoint_{checkpoint}_mae_percent"] = mae_percent
    if age >= OBSERVATION_SECONDS and row.get("status") == "ACTIVE":
        finalize_candidate(row, now_epoch)
    return row


def update_active_candidates(rows, quote_provider, spy_price, now_epoch):
    changed = False
    for index, row in enumerate(rows):
        if row.get("status") != "ACTIVE":
            continue
        symbol = row.get("option_symbol")
        quote = quote_provider(symbol) if symbol else None
        updated = update_candidate(row, quote, spy_price, now_epoch)
        if updated != row:
            rows[index] = updated
            changed = True
    return changed


def create_shadow_candidate_if_needed(signal, contract, now_epoch, now_dt, path=SHADOW_CANDIDATES_FILE):
    direction = direction_from_decision(signal)
    if direction not in {"CALL", "PUT"} or not contract or not contract.get("symbol"):
        return None, False
    option_price, _ = option_midpoint(contract)
    if option_price is None:
        return None, False
    signal = dict(signal or {})
    signal.setdefault("symbol", signal.get("underlying") or "SPY")
    fingerprint = setup_fingerprint(signal, contract.get("symbol"))
    rows = load_candidates(path)
    for row in reversed(rows):
        if row.get("fingerprint") == fingerprint and row.get("status") == "ACTIVE":
            return row, False
        if row.get("fingerprint") == fingerprint and safe_float(row.get("created_epoch"), 0) and now_epoch - safe_float(row.get("created_epoch"), 0) < OBSERVATION_SECONDS:
            return row, False
    candidate = create_candidate(signal, contract, now_epoch, now_dt)
    candidate["fingerprint"] = fingerprint
    candidate["starting_option_price"] = candidate.get("option_midpoint", "")
    rows.append(candidate)
    save_candidates(rows, path)
    return candidate, True


def record_shadow_research(signal, contracts_by_direction, quote_provider, spy_price, market_open, now_epoch, now_dt, path=SHADOW_CANDIDATES_FILE):
    if not market_open:
        return {"created": False, "updated": False, "active": 0, "completed": 0}
    rows = load_candidates(path)
    updated = update_active_candidates(rows, quote_provider, spy_price, now_epoch)
    if updated:
        save_candidates(rows, path)
    direction = direction_from_decision(signal)
    created = False
    if direction in {"CALL", "PUT"}:
        contract = (contracts_by_direction or {}).get(direction)
        _, created = create_shadow_candidate_if_needed(signal, contract, now_epoch, now_dt, path)
    summary = shadow_summary(path=path)
    return {"created": created, "updated": updated, **summary}


def recover_shadow_candidates(quote_provider, spy_price_provider, market_open, now_epoch, path=SHADOW_CANDIDATES_FILE):
    if not market_open:
        return False
    rows = load_candidates(path)
    changed = False
    for index, row in enumerate(rows):
        if row.get("status") != "ACTIVE":
            continue
        spy_price = spy_price_provider(row.get("symbol"))
        quote = quote_provider(row.get("option_symbol")) if row.get("option_symbol") else None
        updated = update_candidate(row, quote, spy_price, now_epoch)
        if updated != row:
            rows[index] = updated
            changed = True
    if changed:
        save_candidates(rows, path)
    return changed


def shadow_summary(path=SHADOW_CANDIDATES_FILE, today=None, limit=20):
    rows = load_candidates(path)
    if today:
        today_rows = [row for row in rows if str(row.get("timestamp", "")).startswith(str(today))]
    else:
        today_rows = rows
    completed = [row for row in rows if row.get("status") == "COMPLETED"]
    active = [row for row in rows if row.get("status") == "ACTIVE"]
    successes = [row for row in completed if str(row.get("hit_plus_5_before_minus_5")).lower() == "true"]
    avg_mfe = sum(safe_float(row.get("maximum_favorable_excursion_5m"), 0) or 0 for row in completed) / len(completed) if completed else 0
    avg_mae = sum(safe_float(row.get("maximum_adverse_excursion_5m"), 0) or 0 for row in completed) / len(completed) if completed else 0
    return {
        "candidates_today": len(today_rows),
        "completed_candidates": len(completed),
        "active_candidates": len(active),
        "plus_5_before_minus_5_success_rate": (len(successes) / len(completed) * 100) if completed else 0,
        "average_5m_mfe": avg_mfe,
        "average_5m_mae": avg_mae,
        "latest_candidates": list(reversed(rows[-limit:])),
        "active": len(active),
        "completed": len(completed),
    }

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MarketSnapshot:
    timestamp: str
    symbol: str
    current_price: float | None
    bullish_score: int
    bearish_score: int
    confidence: int
    dominance_percent: float
    market_state: str | None
    suggested_direction: str | None
    signal: str | None
    ema_bullish: bool
    ema_bearish: bool
    macd_bullish: bool
    macd_bearish: bool
    above_vwap: bool
    below_vwap: bool
    volume_confirmed: bool
    opening_range_ready: bool
    opening_range_high: float | None
    opening_range_low: float | None
    completed_closes: tuple[float, ...]
    option_symbol: str | None = None
    option_bid: float | None = None
    option_ask: float | None = None
    option_last: float | None = None
    option_midpoint: float | None = None
    option_quote_timestamp: str | None = None
    call_option_symbol: str | None = None
    call_option_bid: float | None = None
    call_option_ask: float | None = None
    call_option_last: float | None = None
    call_option_midpoint: float | None = None
    put_option_symbol: str | None = None
    put_option_bid: float | None = None
    put_option_ask: float | None = None
    put_option_last: float | None = None
    put_option_midpoint: float | None = None
    momentum_confirmed: bool = False


def _float_or_none(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _direction_from_signal(signal_value):
    if signal_value in {"CALL", "PUT"}:
        return signal_value
    if signal_value == "BUY CALL":
        return "CALL"
    if signal_value == "BUY PUT":
        return "PUT"
    return None


def build_market_snapshot_from_signal(signal: dict, symbol: str, timestamp: str) -> MarketSnapshot:
    signal = dict(signal or {})
    levels = dict(signal.get("levels") or {})
    current_signal = signal.get("current_signal") or signal.get("signal")
    completed_closes = tuple(
        close for close in (_float_or_none(value) for value in signal.get("completed_closes", ()))
        if close is not None
    )
    opening_range_high = _float_or_none(levels.get("opening_range_high"))
    opening_range_low = _float_or_none(levels.get("opening_range_low"))
    macd_state = signal.get("macd_state")
    vwap_state = signal.get("vwap_state")
    volume_state = signal.get("volume_state")

    return MarketSnapshot(
        timestamp=timestamp,
        symbol=symbol,
        current_price=_float_or_none(signal.get("price")),
        bullish_score=int(signal.get("bullish_score") or 0),
        bearish_score=int(signal.get("bearish_score") or 0),
        confidence=int(signal.get("confidence") or 0),
        dominance_percent=float(signal.get("dominance_percent") or 0),
        market_state=signal.get("market_state"),
        suggested_direction=None,
        signal=current_signal,
        ema_bullish=bool(signal.get("ema_bullish") or signal.get("ema_state") == "BULLISH"),
        ema_bearish=bool(signal.get("ema_bearish") or signal.get("ema_state") == "BEARISH"),
        macd_bullish=macd_state == "BULLISH",
        macd_bearish=macd_state == "BEARISH",
        above_vwap=vwap_state == "BULLISH",
        below_vwap=vwap_state == "BEARISH",
        volume_confirmed=volume_state in {"BULLISH", "BEARISH"},
        opening_range_ready=opening_range_high is not None and opening_range_low is not None,
        opening_range_high=opening_range_high,
        opening_range_low=opening_range_low,
        completed_closes=completed_closes,
        option_symbol=signal.get("option_symbol"),
        option_bid=_float_or_none(signal.get("option_bid")),
        option_ask=_float_or_none(signal.get("option_ask")),
        option_last=_float_or_none(signal.get("option_last")),
        option_midpoint=_float_or_none(signal.get("option_midpoint")),
        option_quote_timestamp=signal.get("option_quote_timestamp"),
        call_option_symbol=signal.get("call_option_symbol"),
        call_option_bid=_float_or_none(signal.get("call_option_bid")),
        call_option_ask=_float_or_none(signal.get("call_option_ask")),
        call_option_last=_float_or_none(signal.get("call_option_last")),
        call_option_midpoint=_float_or_none(signal.get("call_option_midpoint")),
        put_option_symbol=signal.get("put_option_symbol"),
        put_option_bid=_float_or_none(signal.get("put_option_bid")),
        put_option_ask=_float_or_none(signal.get("put_option_ask")),
        put_option_last=_float_or_none(signal.get("put_option_last")),
        put_option_midpoint=_float_or_none(signal.get("put_option_midpoint")),
        momentum_confirmed=bool(signal.get("momentum_confirmed") or signal.get("option_momentum_confirmed")),
    )

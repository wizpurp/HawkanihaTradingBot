from __future__ import annotations

import re


OCC_SYMBOL_PATTERN = re.compile(r"^[A-Z]{1,6}\d{6}([CP])\d{8}$")


def option_symbol_direction(symbol: str | None) -> str | None:
    text = str(symbol or "").strip().upper()
    if not text:
        return None
    if text.endswith("CALL"):
        return "CALL"
    if text.endswith("PUT"):
        return "PUT"
    match = OCC_SYMBOL_PATTERN.match(text)
    if match:
        return "CALL" if match.group(1) == "C" else "PUT"
    return None


def option_symbol_matches_direction(symbol: str | None, direction: str | None) -> bool:
    resolved = option_symbol_direction(symbol)
    return resolved is not None and resolved == str(direction or "").upper()

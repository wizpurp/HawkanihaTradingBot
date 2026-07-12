import csv
from datetime import datetime
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")


def market_now():
    return datetime.now(MARKET_TZ)

def log_trade(action, symbol, qty, price, pnl=""):
    with open("trades.csv", "a", newline="") as f:
        writer = csv.writer(f)

        writer.writerow([
            market_now().strftime("%Y-%m-%d %H:%M:%S"),
            action,
            symbol,
            qty,
            price,
            pnl
        ])

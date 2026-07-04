import csv
from datetime import datetime

def log_trade(action, symbol, qty, price, pnl=""):
    with open("trades.csv", "a", newline="") as f:
        writer = csv.writer(f)

        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            action,
            symbol,
            qty,
            price,
            pnl
        ])
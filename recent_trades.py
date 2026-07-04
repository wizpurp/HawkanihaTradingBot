import csv

def get_recent_trades():
    trades = []

    try:
        with open("trades.csv", "r") as f:
            reader = csv.DictReader(f)

            for row in reader:
                trades.append(row)

        return trades[-10:]

    except:
        return []


if __name__ == "__main__":
    recent = get_recent_trades()

    if not recent:
        print("No trades yet.")
    else:
        print("\nRecent Trades\n")

        for trade in reversed(recent):
            print(
                f"{trade['Time']} | "
                f"{trade['Action']} | "
                f"{trade['Symbol']} | "
                f"Qty:{trade['Qty']} | "
                f"Price:${trade['Price']} | "
                f"PnL:{trade['PnL']}"
            )
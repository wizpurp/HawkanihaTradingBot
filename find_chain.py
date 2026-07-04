import requests
import json

TOKEN = "0kkGC4Wj40dAv9GjOO6c7hioOiXM"

with open("config.json", "r") as f:
    config = json.load(f)

symbol = config["symbol"]
expiration = "2026-06-26"

headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json"
}

# 1. Get current SPY quote
quote_response = requests.get(
    "https://sandbox.tradier.com/v1/markets/quotes",
    params={"symbols": symbol},
    headers=headers
)

quote_data = quote_response.json()
spy_price = float(quote_data["quotes"]["quote"]["last"])

print("SPY price:", spy_price)

# 2. Get option chain
chain_response = requests.get(
    "https://sandbox.tradier.com/v1/markets/options/chains",
    params={
        "symbol": symbol,
        "expiration": expiration
    },
    headers=headers
)

print("Chain status:", chain_response.status_code)

chain_data = chain_response.json()
options = chain_data["options"]["option"]

# 3. Only show strikes near current price
nearby = []

for option in options:
    strike = float(option["strike"])

    if abs(strike - spy_price) <= 20:
        nearby.append(option)

# 4. Sort by nearest strike
nearby.sort(key=lambda x: abs(float(x["strike"]) - spy_price))

print("\nNearby contracts:")

for option in nearby[:30]:
    print(
        option["symbol"],
        option["strike"],
        option["option_type"],
        "bid:", option.get("bid"),
        "ask:", option.get("ask"),
        "last:", option.get("last")
    )
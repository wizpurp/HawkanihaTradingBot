import requests
import json

TOKEN = "0kkGC4Wj40dAv9GjOO6c7hioOiXM"

with open("config.json", "r") as f:
    config = json.load(f)

symbol = config["symbol"]

headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json"
}

# Get SPY price
r = requests.get(
    "https://sandbox.tradier.com/v1/markets/quotes",
    params={"symbols": symbol},
    headers=headers
)

spy_price = float(r.json()["quotes"]["quote"]["last"])

# Get nearest expiration
r = requests.get(
    "https://sandbox.tradier.com/v1/markets/options/expirations",
    params={
        "symbol": symbol,
        "includeAllRoots": "true"
    },
    headers=headers
)

expiration = r.json()["expirations"]["date"][0]

# Get chain
r = requests.get(
    "https://sandbox.tradier.com/v1/markets/options/chains",
    params={
        "symbol": symbol,
        "expiration": expiration
    },
    headers=headers
)

options = r.json()["options"]["option"]

# Find ATM strike
closest = min(
    options,
    key=lambda o: abs(float(o["strike"]) - spy_price)
)

atm_strike = float(closest["strike"])

print("SPY:", spy_price)
print("Expiration:", expiration)
print("ATM Strike:", atm_strike)

call = None
put = None

for option in options:

    strike = float(option["strike"])

    if strike == atm_strike:

        if option["option_type"] == "call":
            call = option

        if option["option_type"] == "put":
            put = option

print("\nCALL:")
print(call["symbol"])
print(call["bid"], call["ask"], call["last"])

print("\nPUT:")
print(put["symbol"])
print(put["bid"], put["ask"], put["last"])
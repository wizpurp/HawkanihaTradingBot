import requests
import json

TOKEN = "0kkGC4Wj40dAv9GjOO6c7hioOiXM"

with open("config.json") as f:
    config = json.load(f)

symbol = config["symbol"]

r = requests.get(
    "https://sandbox.tradier.com/v1/markets/options/expirations",
    params={
        "symbol": symbol,
        "includeAllRoots": "true"
    },
    headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json"
    }
)

print(r.status_code)
print(r.text)
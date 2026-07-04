import requests
import json

# Load config
with open("config.json", "r") as f:
    config = json.load(f)

TOKEN = "0kkGC4Wj40dAv9GjOO6c7hioOiXM"
SYMBOL = config["symbol"]

headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json"
}

r = requests.get(
    f"https://sandbox.tradier.com/v1/markets/quotes?symbols={SYMBOL}",
    headers=headers
)

print(r.status_code)
print(r.text)
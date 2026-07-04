import requests

TOKEN = "0kkGC4Wj40dAv9GjOO6c7hioOiXM"

r = requests.get(
    "https://sandbox.tradier.com/v1/markets/options/chains",
    params={
        "symbol": "SPY",
        "expiration": "2026-06-23"
    },
    headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json"
    }
)

data = r.json()

for option in data["options"]["option"][:10]:
    print(option["symbol"])
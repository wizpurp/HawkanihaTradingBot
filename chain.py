import requests

TOKEN = "rtGeL1UJ7QgsiKLqP3ixRGIJWmCS"

r = requests.get(
    "https://api.tradier.com/v1/markets/options/chains",
    params={
        "symbol": "SPY",
        "expiration": "2026-06-23"
    },
    headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json"
    }
)

print(r.status_code)
print(r.text[:2000])
import requests

TOKEN = "0kkGC4Wj40dAv9GjOO6c7hioOiXM"
ACCOUNT = "VA52467186"

data = {
    "class": "equity",
    "symbol": "SPY",
    "side": "sell",
    "quantity": "1",
    "type": "market",
    "duration": "day"
}

r = requests.post(
    f"https://sandbox.tradier.com/v1/accounts/{ACCOUNT}/orders",
    headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json"
    },
    data=data
)

print(r.status_code)
print(r.text)
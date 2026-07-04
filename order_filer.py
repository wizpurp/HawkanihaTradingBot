import requests

TOKEN = "0kkGC4Wj40dAv9GjOO6c7hioOiXM"
ACCOUNT = "VA52467186"

r = requests.get(
    f"https://sandbox.tradier.com/v1/accounts/{ACCOUNT}/orders",
    headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json"
    }
)

print(r.status_code)
print(r.text)
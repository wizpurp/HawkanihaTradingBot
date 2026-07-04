import json
import requests

TOKEN = "0kkGC4Wj40dAv9GjOO6c7hioOiXM"

with open("config.json", "r") as f:
    config = json.load(f)

OPTION = config["option_symbol"]

r = requests.get(
    "https://sandbox.tradier.com/v1/markets/quotes",
    params={"symbols": OPTION},
    headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json"
    }
)

print(r.status_code)
print(r.text)
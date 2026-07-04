import requests

TOKEN = "rtGeL1UJ7QgsiKLqP3ixRGIJWmCS"

r = requests.get(
    "https://api.tradier.com/v1/user/profile",
    headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json"
    }
)

print(r.status_code)
print(r.text)
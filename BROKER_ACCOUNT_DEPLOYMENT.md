# Verified broker account dashboard

The account panel uses Tradier's `/accounts/{account}/balances` endpoint, verifies
the returned account number, and masks it in the UI. A successful verified
response labels sandbox as PAPER MONEY or production as REAL MONEY. An error or
unverified response shows UNKNOWN / CONNECTION ERROR. Balance requests are cached
for 60 seconds (including failures); last successful balances remain visible but
are marked stale after an error or after 120 seconds without a successful update.
The browser refreshes the account panel every 10 seconds without requesting ticker
data. This works outside market hours.

Mapping: total_equity -> total account value; total_cash -> cash; market_value ->
positions value; open_pl -> unrealized P/L; margin/pdt.option_buying_power -> options
buying power. Explicit settled_cash and option_buying_power fields are used when
present. cash.cash_available is shown separately, not guessed to be settled cash
or options buying power. Missing fields remain N/A. Bot budget is a separate limit.

Quote reporting uses actual broker trade/bid/ask timestamps and separately reports
the most recently observed quote symbol, age, request status and last successful
fetch. Production is NOT assumed fresh. The display-only freshness indicator
requires a broker timestamp no more than 30 seconds old and a successful fetch
within 60 seconds. These display thresholds do not change stop/entry strategy.
Existing explicitly enabled entry-freshness settings still apply independently.

## Credentials and selection

Set matching pairs in the server's protected `.env` (never commit actual secrets):

```
TRADIER_SANDBOX_TOKEN=...
TRADIER_SANDBOX_ACCOUNT=...
TRADIER_LIVE_TOKEN=...
TRADIER_LIVE_ACCOUNT=...
```

Sandbox routes to https://sandbox.tradier.com/v1; live routes to
https://api.tradier.com/v1. The old TRADIER_TOKEN/TRADIER_ACCOUNT pair is supported
only for the environment exactly identified by TRADIER_BASE_URL, and only when
neither specific credential for that environment is set. A partial pair fails;
there is no cross-environment fallback. Secrets are loaded at process startup;
restart after changing `.env`. Before the first explicit selection, the actual
legacy URL determines the environment rather than the old cosmetic mode setting.

The Settings selector saves `broker_environment` and mirrors it in `mode`.
Switching requires verified zero open broker positions and zero pending orders.
It disables the bot, cancels its pending entry and discards original-bot market
caches. Wait for the account panel to validate the new account, then separately
check Bot Enabled and Save Settings. Enabling on the switch request is ignored.
Invalid matching credentials never cause fallback. Buy requests require account
validation; balance-service errors do not add a new gate to sell orders.
Tournament positions and balances remain SIMULATED. Historical trade ledgers are
not rewritten or re-attributed to a newly selected account.

## Deployment (not performed by this change)

Local PowerShell, after reviewing the changes:

```powershell
cd C:\Users\pearl\HawkanihaTradingBot
git add dashboard.py broker_connection.py .env.example BROKER_ACCOUNT_DEPLOYMENT.md tests/test_broker_connection.py tests/test_broker_execution_diagnostics.py tests/test_broker_realized_pnl.py
git commit -m "Add verified broker accounts and explicit environment routing"
git push origin main
ssh root@142.93.59.239
```

On the VPS:

```bash
cd /root/HawkanihaTradingBot
git status --short
cp -p config.json "config.server.backup.$(date +%Y%m%d-%H%M%S).json"
git pull --ff-only
# Edit environment-specific credentials only if needed; do not print .env.
nano .env
chmod 600 .env
source venv/bin/activate
python -m py_compile dashboard.py broker_connection.py logs/trade_logger.py
sudo systemctl restart hawkbot
sudo systemctl status hawkbot --no-pager
curl --max-time 90 -s -o /dev/null -w 'Dashboard HTTP: %{http_code}\n' http://127.0.0.1:5000/
curl --max-time 30 -s http://127.0.0.1:5000/api/account
```

Do not restore/reset server config or runtime histories to resolve a pull conflict.
Stop and inspect conflicts. No new dependencies are required. Tests use mock
transport only; run the full suite in an isolated checkout without live `.env` or
runtime data. Restart retains the existing bot-enabled setting for an unchanged
environment; intentionally disable it first if you do not want it trading on
restart. Never enable LIVE unless you intend real-money trading.

Balance reference: https://docs.tradier.com/docs/balances

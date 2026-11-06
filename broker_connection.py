import hashlib
import math
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests


URLS = {"sandbox": "https://sandbox.tradier.com/v1", "live": "https://api.tradier.com/v1"}


class BrokerConnectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Connection:
    environment: str
    account: str = field(repr=False)
    token: str = field(repr=False)

    @property
    def url(self):
        return URLS[self.environment]

    @property
    def key(self):
        return hashlib.sha256(f"{self.environment}:{self.account}:{self.token}".encode()).hexdigest()


def legacy_environment(environ=None):
    environ = os.environ if environ is None else environ
    url = environ.get("TRADIER_BASE_URL", URLS["sandbox"]).rstrip("/")
    return next((mode for mode, target in URLS.items() if target == url), "unknown")


def resolve_connection(config, environ=None):
    environ = os.environ if environ is None else environ
    mode = config.get("broker_environment", legacy_environment(environ))
    if mode not in URLS:
        raise BrokerConnectionError("Unknown broker environment")
    prefix = "TRADIER_SANDBOX" if mode == "sandbox" else "TRADIER_LIVE"
    token = environ.get(prefix + "_TOKEN", "")
    account = environ.get(prefix + "_ACCOUNT", "")
    if not token and not account and mode == legacy_environment(environ):
        token, account = environ.get("TRADIER_TOKEN", ""), environ.get("TRADIER_ACCOUNT", "")
    if not token or not account:
        raise BrokerConnectionError("Matching environment credentials are not configured")
    return Connection(mode, account, token)


def entry_funds_limit(bot_budget, spent_today, buying_power):
    """Largest cost a new bot entry may have: the lower of today's remaining bot
    budget and broker-reported buying power. Returns (limit, binding_reason);
    limit is None when buying power is unknown (caller must block the entry)."""
    budget_left = max(0.0, (number(bot_budget) or 0.0) - (number(spent_today) or 0.0))
    power = number(buying_power)
    if power is None:
        return None, "broker buying power unavailable"
    if power < budget_left:
        return max(0.0, power), "broker buying power"
    return budget_left, "bot budget remaining today"


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def parse_balances(balances):
    account_type = balances.get("account_type")
    details = balances.get(account_type, {})
    details = details if isinstance(details, dict) else {}
    options_power = number(details.get("option_buying_power", balances.get("option_buying_power")))
    cash_available = number(details.get("cash_available"))
    # Funds a new option buy may use: the broker's option buying power when reported
    # (margin/pdt); for cash accounts, Tradier's cash.cash_available. Otherwise unknown
    # (None) so entries fail closed rather than guessing.
    if options_power is not None:
        entry_power = options_power
    elif account_type == "cash":
        entry_power = cash_available
    else:
        entry_power = None
    return {
        "entry_buying_power": entry_power,
        "total_account_value": number(balances.get("total_equity")),
        "cash": number(balances.get("total_cash")),
        "settled_cash": number(balances.get("settled_cash", details.get("settled_cash"))),
        "options_buying_power": options_power,
        "cash_available": cash_available,
        "positions_value": number(balances.get("market_value")),
        "unrealized_pnl": number(balances.get("open_pl")),
    }


class BrokerGateway:
    def __init__(self, transport=requests, clock=time.time):
        self.transport = transport
        self.clock = clock
        self.lock = threading.RLock()
        self.accounts = {}
        self.quotes = {}

    def request(self, connection, method, path, **kwargs):
        kwargs.pop("headers", None)
        kwargs.setdefault("timeout", 10)
        kwargs["allow_redirects"] = False
        path = path.replace("{account}", connection.account)
        try:
            return getattr(self.transport, method.lower())(
                connection.url + path,
                headers={"Authorization": f"Bearer {connection.token}", "Accept": "application/json"}, **kwargs)
        except Exception:
            raise BrokerConnectionError("Broker request failed") from None

    def account(self, connection, force=False):
        with self.lock:
            now = self.clock()
            cached = self.accounts.get(connection.key, {})
            if force or now - cached.get("attempt", -1000) >= 60:
                cached = dict(cached, attempt=now)
                try:
                    response = self.request(connection, "GET", "/accounts/{account}/balances")
                    if response.status_code != 200:
                        raise BrokerConnectionError(f"Broker balance request HTTP {response.status_code}")
                    balances = response.json().get("balances")
                    if not isinstance(balances, dict) or str(balances.get("account_number", "")) != connection.account:
                        raise BrokerConnectionError("Broker account identity could not be verified")
                    cached.update(values=parse_balances(balances), success=now, error=None,
                                  pending_orders=number(balances.get("pending_orders_count")))
                except BrokerConnectionError as error:
                    cached["error"] = str(error)
                except Exception:
                    cached["error"] = "Invalid broker balance response"
                self.accounts[connection.key] = cached
            age = now - cached["success"] if "success" in cached else None
            verified = age is not None and age < 120 and not cached.get("error")
            return {
                "environment": connection.environment,
                "environment_label": ("LIVE — REAL MONEY" if connection.environment == "live" else "SANDBOX — PAPER MONEY") if verified else "UNKNOWN / CONNECTION ERROR",
                "masked_account": "****" + connection.account[-4:],
                "validated": bool(verified), "stale": not verified,
                "connection_status": "CONNECTED" if verified else "UNVERIFIED / ERROR",
                "error": cached.get("error"), "age_seconds": age,
                "last_success": datetime.fromtimestamp(cached["success"], timezone.utc).isoformat() if "success" in cached else None,
                "balances": dict(cached.get("values", {})), "pending_orders": cached.get("pending_orders"),
            }

    def observe_quote(self, connection, quote):
        self.quotes[connection.key] = {"quote": dict(quote), "success": self.clock(), "error": None}

    def quote_failed(self, connection, reason):
        cached = self.quotes.setdefault(connection.key, {})
        cached["error"] = reason

    def assert_switch_safe(self, connection):
        account = self.account(connection, force=True)
        if not account["validated"] or account["pending_orders"] != 0:
            raise BrokerConnectionError("Cannot switch: validate current account and resolve pending orders first")
        response = self.request(connection, "GET", "/accounts/{account}/positions")
        if response.status_code != 200:
            raise BrokerConnectionError("Cannot switch: current positions could not be verified")
        try:
            payload = response.json()
            positions = payload["positions"]
            if positions in (None, "null"):
                return
            rows = positions["position"]
            rows = rows if isinstance(rows, list) else [rows]
            if any(number(row.get("quantity")) != 0 for row in rows):
                raise BrokerConnectionError("Cannot switch with open broker positions")
        except BrokerConnectionError:
            raise
        except Exception:
            raise BrokerConnectionError("Cannot switch: invalid positions response") from None

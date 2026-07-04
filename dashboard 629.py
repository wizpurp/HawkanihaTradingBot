import re
from flask import Flask, request, redirect, jsonify
import requests
import json



app = Flask(__name__)

TOKEN = "0kkGC4Wj40dAv9GjOO6c7hioOiXM"
ACCOUNT = "VA52467186"
BASE_URL = "https://sandbox.tradier.com/v1"


def load_config():
    with open("config.json", "r") as f:
        config = json.load(f)

    config.setdefault("entry_rules", {
        "ema_alignment": True,
        "macd_confirmation": True,
        "vwap_confirmation": True,
        "volume_confirmation": False,
        "minimum_signals": 3,
        "allow_calls": True,
        "allow_puts": True,
        "cooldown_minutes": 5,
        "max_trades_per_day": 10
    })

    config.setdefault("bot_enabled", False)
    config.setdefault("strategy_mode", "SURFER")
    config.setdefault("scanner", {"interval_seconds": 60})
    config.setdefault("strategy", {})

    return config


def save_config(config):
    with open("config.json", "w") as f:
        json.dump(config, f, indent=4)


def headers():
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json"
    }


def fmt_money(value):
    if value is None:
        return "N/A"
    try:
        return f"${float(value):,.2f}"
    except:
        return "N/A"


def fmt_int(value):
    if value is None:
        return "N/A"
    try:
        return f"{int(float(value)):,}"
    except:
        return "N/A"


def get_market_quote(symbol):
    try:
        r = requests.get(
            f"{BASE_URL}/markets/quotes",
            params={"symbols": symbol},
            headers=headers()
        )
        if r.status_code != 200:
            return None

        data = r.json()["quotes"]
        if "quote" not in data:
            return None

        quote = data["quote"]
        if isinstance(quote, list):
            return quote[0]
        return quote
    except:
        return None


def get_position():
    try:
        r = requests.get(
            f"{BASE_URL}/accounts/{ACCOUNT}/positions",
            headers=headers()
        )
        if r.status_code != 200:
            return None

        data = r.json()
        if data.get("positions") == "null":
            return None

        pos = data["positions"]["position"]
        return pos if isinstance(pos, list) else [pos]
    except:
        return None


def get_expirations(symbol):
    try:
        r = requests.get(
            f"{BASE_URL}/markets/options/expirations",
            params={"symbol": symbol, "includeAllRoots": "true"},
            headers=headers()
        )
        if r.status_code != 200:
            return []

        dates = r.json()["expirations"]["date"]
        return dates if isinstance(dates, list) else [dates]
    except:
        return []


def get_option_chain(symbol, expiration):
    try:
        r = requests.get(
            f"{BASE_URL}/markets/options/chains",
            params={"symbol": symbol, "expiration": expiration},
            headers=headers()
        )
        if r.status_code != 200:
            return []

        options = r.json()["options"]["option"]
        return options if isinstance(options, list) else [options]
    except:
        return []


def select_atm_contract(symbol, side):
    quote = get_market_quote(symbol)
    if not quote or quote.get("last") is None:
        return None

    price = float(quote["last"])
    expirations = get_expirations(symbol)
    if not expirations:
        return None

    expiration = expirations[0]
    chain = get_option_chain(symbol, expiration)

    option_type = "call" if side == "CALL" else "put"
    matching = [o for o in chain if o.get("option_type") == option_type]

    if not matching:
        return None

    selected = min(matching, key=lambda o: abs(float(o["strike"]) - price))
    selected["expiration"] = expiration
    selected["underlying_price"] = price
    return selected


def submit_option_order(option_symbol, qty, action="buy_to_open"):
    config = load_config()
    underlying = config["symbol"]

    data = {
        "class": "option",
        "symbol": underlying,
        "option_symbol": option_symbol,
        "side": action,
        "quantity": str(qty),
        "type": "market",
        "duration": "day"
    }

    r = requests.post(
        f"{BASE_URL}/accounts/{ACCOUNT}/orders",
        headers=headers(),
        data=data
    )

    return r.status_code, r.text


def sell_all_positions():
    positions = get_position()
    if not positions:
        return False, "No open positions"

    results = []

    for pos in positions:
        symbol = pos.get("symbol")
        qty = int(float(pos.get("quantity", 1)))

        if len(symbol) > 6:
            status, text = submit_option_order(symbol, qty, "sell_to_close")
            results.append(text)
        else:
            data = {
                "class": "equity",
                "symbol": symbol,
                "side": "sell",
                "quantity": str(qty),
                "type": "market",
                "duration": "day",
                "tag": f"MANUAL_SELL_{symbol}"
            }

            r = requests.post(
                f"{BASE_URL}/accounts/{ACCOUNT}/orders",
                headers=headers(),
                data=data
            )
            results.append(r.text)

    return True, " | ".join(results)


@app.route("/api/expirations")
def api_expirations():
    config = load_config()
    symbol = request.args.get("symbol", config["symbol"]).upper()
    return jsonify({"dates": get_expirations(symbol)})


@app.route("/api/chain")
def api_chain():
    config = load_config()
    symbol = request.args.get("symbol", config["symbol"]).upper()
    expiration = request.args.get("expiration")
    option_type = request.args.get("option_type", "call")

    if not expiration:
        return jsonify({"options": []})

    chain = get_option_chain(symbol, expiration)
    filtered = []

    for opt in chain:
        if opt.get("option_type") == option_type:
            filtered.append({
                "symbol": opt.get("symbol"),
                "strike": opt.get("strike"),
                "bid": opt.get("bid"),
                "ask": opt.get("ask"),
                "last": opt.get("last"),
                "volume": opt.get("volume"),
                "open_interest": opt.get("open_interest"),
                "expiration": expiration,
                "option_type": option_type
            })

    filtered.sort(key=lambda x: float(x["strike"]))
    return jsonify({"options": filtered})


@app.route("/manual-buy-selected", methods=["POST"])
def manual_buy_selected():
    option_symbol = request.form.get("option_symbol")
    qty = int(request.form.get("manual_qty", 1))

    if option_symbol:
        submit_option_order(option_symbol, qty, "buy_to_open")

    return redirect("/")


@app.route("/manual-buy-call", methods=["POST"])
def manual_buy_call():
    config = load_config()
    contract = select_atm_contract(config["symbol"], "CALL")
    if contract:
        status, text = submit_option_order(contract["symbol"], config["contracts"], "buy_to_open")
        print("ORDER STATUS:", status)
        print("ORDER RESPONSE:", text)
    return redirect("/")


@app.route("/manual-buy-put", methods=["POST"])
def manual_buy_put():
    config = load_config()
    contract = select_atm_contract(config["symbol"], "PUT")
    if contract:
        status, text = submit_option_order(contract["symbol"], config["contracts"], "buy_to_open")
        print("ORDER STATUS:", status)
        print("ORDER RESPONSE:", text)
    return redirect("/")


@app.route("/manual-sell", methods=["POST"])
def manual_sell():
    sell_all_positions()
    return redirect("/")


@app.route("/save-settings", methods=["POST"])
def save_settings():
    config = load_config()

    symbol_choice = request.form.get("symbol_choice", "SPY")
    custom_symbol = request.form.get("custom_symbol", "").strip().upper()

    config["symbol"] = custom_symbol if custom_symbol else symbol_choice
    config["mode"] = request.form.get("mode", "sandbox")
    config["asset_type"] = request.form.get("asset_type", "option")
    config["contracts"] = int(request.form.get("contracts", 1))
    config["bot_enabled"] = request.form.get("bot_enabled") == "on"
    config["strategy_mode"] = request.form.get("strategy_mode", "SURFER")

    s = config["strategy"]
    s["ema_fast"] = int(request.form.get("ema_fast", 1))
    s["ema_medium"] = int(request.form.get("ema_medium", 5))
    s["ema_slow"] = int(request.form.get("ema_slow", 10))
    s["ma_fast"] = int(request.form.get("ma_fast", 5))
    s["ma_medium"] = int(request.form.get("ma_medium", 10))
    s["ma_slow"] = int(request.form.get("ma_slow", 20))
    s["use_macd"] = request.form.get("use_macd") == "on"
    s["use_vwap"] = request.form.get("use_vwap") == "on"
    s["use_volume"] = request.form.get("use_volume") == "on"
    s["hard_stop_percent"] = float(request.form.get("hard_stop_percent", 20))
    s["trailing_stop_percent"] = float(request.form.get("trailing_stop_percent", 15))
    s["tick_interval_seconds"] = int(request.form.get("tick_interval_seconds", 10))
    s["direction_threshold_percent"] = float(request.form.get("direction_threshold_percent", 60))

    e = config["entry_rules"]
    e["ema_alignment"] = request.form.get("ema_alignment") == "on"
    e["macd_confirmation"] = request.form.get("macd_confirmation") == "on"
    e["vwap_confirmation"] = request.form.get("vwap_confirmation") == "on"
    e["volume_confirmation"] = request.form.get("volume_confirmation") == "on"
    e["minimum_signals"] = int(request.form.get("minimum_signals", 3))
    e["allow_calls"] = request.form.get("allow_calls") == "on"
    e["allow_puts"] = request.form.get("allow_puts") == "on"
    cooldown = request.form.get("cooldown_minutes", "").strip()
    e["cooldown_minutes"] = int(cooldown) if cooldown.isdigit() else 5
    max_trades = request.form.get("max_trades_per_day", "").strip()
    e["max_trades_per_day"] = int(max_trades) if max_trades else 10

    config["scanner"]["interval_seconds"] = int(request.form.get("interval_seconds", 60))

    save_config(config)
    return redirect("/")

def get_recent_trades():
    import csv

    try:
        with open("trades.csv", "r", newline="") as f:
            reader = csv.DictReader(f)
            return list(reader)[-10:]
    except:
        return []


def get_position_pl_data(pos):
    symbol = pos.get("symbol", "")
    qty = float(pos.get("quantity", 0) or 0)
    cost_basis = float(pos.get("cost_basis", 0) or 0)

    quote = get_market_quote(symbol)

    current_price = None
    if quote:
        for key in ["last", "bid", "ask"]:
            if quote.get(key) is not None:
                try:
                    current_price = float(quote.get(key))
                    break
                except:
                    pass

    is_option = len(symbol) > 6

    if qty == 0:
        entry_price = 0
    elif is_option:
        entry_price = cost_basis / qty / 100
    else:
        entry_price = cost_basis / qty

    if current_price is None:
        current_price = entry_price

    if is_option:
        current_value = current_price * qty * 100
    else:
        current_value = current_price * qty

    pnl = current_value - cost_basis

    if cost_basis != 0:
        pnl_percent = (pnl / cost_basis) * 100
    else:
        pnl_percent = 0

    return {
        "symbol": symbol,
        "qty": qty,
        "entry_price": entry_price,
        "current_price": current_price,
        "cost_basis": cost_basis,
        "current_value": current_value,
        "pnl": pnl,
        "pnl_percent": pnl_percent,
        "is_option": is_option
    }


@app.route("/")
def dashboard():
    config = load_config()

    mode = config["mode"]
    symbol = config["symbol"]
    asset = config["asset_type"]
    contracts = config["contracts"]
    bot_enabled = config["bot_enabled"]
    strategy_mode = config["strategy_mode"]

    s = config["strategy"]
    e = config["entry_rules"]

    quote = get_market_quote(symbol)
    positions = get_position()
    call = select_atm_contract(symbol, "CALL")
    put = select_atm_contract(symbol, "PUT")

    def checked(value):
        return "checked" if value else ""

    def selected(value, current):
        return "selected" if value == current else ""

    html = f"""
<html>
<head>
<title>Trading Bot Dashboard</title>
<style>
body {{
    font-family: Arial;
    margin: 30px;
    background: #202124;
    color: white;
}}
h1 {{ color: #00ff88; }}
.card {{
    border: 1px solid #555;
    padding: 15px;
    margin-bottom: 18px;
    border-radius: 10px;
    background: #2a2a2a;
}}
.grid {{
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 10px;
}}
.item {{
    background: #1b1b1b;
    padding: 10px;
    border-radius: 8px;
}}
.label {{ color: #aaa; font-size: 13px; }}
.value {{ font-size: 18px; font-weight: bold; }}
input, select {{
    padding: 7px;
    margin: 4px;
    border-radius: 5px;
    border: none;
}}
button {{
    padding: 10px 14px;
    margin: 4px;
    border: none;
    border-radius: 8px;
    background: #00ff88;
    font-weight: bold;
    cursor: pointer;
}}
.red {{ background: #ff5555; }}
.yellow {{ background: #ffd166; }}
.good {{ color: #00ff88; }}
.bad {{ color: #ff6666; }}
</style>
</head>
<body>

<h1>Trading Bot Dashboard</h1>

<div class="card">
<h2>Status</h2>
Bot: <span class="{ "good" if bot_enabled else "bad" }">{ "ENABLED" if bot_enabled else "DISABLED" }</span><br>
Mode: {mode}<br>
Strategy Mode: {strategy_mode}<br>
Ticker: {symbol}
</div>

<div class="card">
<h2>Manual Contract Picker</h2>

<form method="POST" action="/manual-buy-selected">

Expiration:
<select id="expiration_select"></select>

Option Type:
<select id="option_type_select">
<option value="call">CALL</option>
<option value="put">PUT</option>
</select>

Strike:
<select id="strike_select"></select>

Quantity:
<input type="number" name="manual_qty" value="1" min="1">

<input type="hidden" id="option_symbol_input" name="option_symbol">

<br><br>

Selected Contract:
<div id="selected_contract">Loading...</div>

Bid: <span id="bid">N/A</span>
Ask: <span id="ask">N/A</span>
Last: <span id="last">N/A</span>
Volume: <span id="volume">N/A</span>
Open Interest: <span id="open_interest">N/A</span>

<br><br>

<button type="submit">BUY SELECTED CONTRACT</button>

</form>
</div>

<div class="card">
<h2>Manual Controls</h2>
<form method="POST" action="/manual-buy-call" style="display:inline;">
<button type="submit">Auto Buy ATM CALL</button>
</form>

<form method="POST" action="/manual-buy-put" style="display:inline;">
<button type="submit" class="yellow">Auto Buy ATM PUT</button>
</form>

<form method="POST" action="/manual-sell" style="display:inline;">
<button type="submit" class="red">Sell All Positions</button>
</form>
</div>

<div class="card">
<h2>Live Market</h2>
"""

    if quote:
        html += f"""
<div class="grid">
<div class="item"><div class="label">Symbol</div><div class="value">{quote.get("symbol")}</div></div>
<div class="item"><div class="label">Last</div><div class="value">{fmt_money(quote.get("last"))}</div></div>
<div class="item"><div class="label">Bid</div><div class="value">{fmt_money(quote.get("bid"))}</div></div>
<div class="item"><div class="label">Ask</div><div class="value">{fmt_money(quote.get("ask"))}</div></div>
<div class="item"><div class="label">Volume</div><div class="value">{fmt_int(quote.get("volume"))}</div></div>
<div class="item"><div class="label">Avg Volume</div><div class="value">{fmt_int(quote.get("average_volume"))}</div></div>
</div>
"""
    else:
        html += "Market quote unavailable."

    html += "</div>"

    html += """
<div class="card">
<h2>Selected ATM Options</h2>
<div class="grid">
"""

    for title, opt in [("CALL", call), ("PUT", put)]:
        if opt:
            spread = None
            if opt.get("ask") is not None and opt.get("bid") is not None:
                spread = float(opt.get("ask")) - float(opt.get("bid"))

            html += f"""
<div class="item">
<div class="label">{title}</div>
<div class="value">{opt.get("symbol")}</div>
Strike: {opt.get("strike")}<br>
Exp: {opt.get("expiration")}<br>
Bid: {fmt_money(opt.get("bid"))}<br>
Ask: {fmt_money(opt.get("ask"))}<br>
Last: {fmt_money(opt.get("last"))}<br>
Spread: {fmt_money(spread)}<br>
Volume: {fmt_int(opt.get("volume"))}<br>
Open Interest: {fmt_int(opt.get("open_interest"))}
</div>
"""
        else:
            html += f"<div class='item'>{title}: No contract found</div>"

    html += f"""
</div>
</div>

<div class="card">
<h2>Strategy Summary</h2>
EMA Stack: {s.get("ema_fast")} / {s.get("ema_medium")} / {s.get("ema_slow")}<br>
MA Stack: {s.get("ma_fast")} / {s.get("ma_medium")} / {s.get("ma_slow")}<br>
MACD: {s.get("use_macd")}<br>
VWAP: {s.get("use_vwap")}<br>
Volume: {s.get("use_volume")}<br>
Hard Stop: {s.get("hard_stop_percent")}%<br>
Trailing Stop: {s.get("trailing_stop_percent")}%<br>
Direction Tick: {s.get("tick_interval_seconds")} sec<br>
Direction Threshold: {s.get("direction_threshold_percent")}%<br>
</div>

<div class="card">
<h2>Entry Rules</h2>
EMA Alignment: {e.get("ema_alignment")}<br>
MACD Confirmation: {e.get("macd_confirmation")}<br>
VWAP Confirmation: {e.get("vwap_confirmation")}<br>
Volume Confirmation: {e.get("volume_confirmation")}<br>
Minimum Signals: {e.get("minimum_signals")}<br>
Allow Calls: {e.get("allow_calls")}<br>
Allow Puts: {e.get("allow_puts")}<br>
Cooldown Minutes: {e.get("cooldown_minutes")}<br>
Max Trades Per Day: {e.get("max_trades_per_day")}
</div>

<div class="card">
<h2>Current Position</h2>

"""

    html += f"<pre>{positions}</pre>"

    if positions:
        for pos in positions:
            pl = get_position_pl_data(pos)
            pnl_color = "#00ff88" if pl["pnl"] >= 0 else "#ff5555"
            html += f"""
<div class="item">
<div class="label">Symbol</div>
<div class="value">{pl["symbol"]}</div>
Qty: {pl["qty"]}<br>
Entry: {fmt_money(pl["entry_price"])}<br>
Current: {fmt_money(pl["current_price"])}<br>
Cost Basis: {fmt_money(pl["cost_basis"])}<br>
Current Value: {fmt_money(pl["current_value"])}<br>
<span style="color:{pnl_color}; font-weight:bold;">
P/L: {fmt_money(pl["pnl"])}<br>
P/L %: {pl["pnl_percent"]:.2f}%
</span><br>
Status: OPEN
</div>
<br>
"""
    else:
        html += "No Position"

    html += """
</div>
"""

    recent = get_recent_trades()

    html += """
<div class="card">
<h2>Recent Trades</h2>
"""

    if recent:
        for trade in reversed(recent):
            html += f"""
<b>{trade.get("Action", "")}</b><br>
{trade.get("Symbol", "")}<br>
Qty: {trade.get("Qty", "")}<br>
Price: ${trade.get("Price", "")}<br>
PnL: {trade.get("PnL", "")}<br>
Time: {trade.get("Time", "")}<br><br>
"""
    else:
        html += "No trades yet."

    html += """
</div>
"""

    html += f"""
<div class="card">
<h2>Settings</h2>

<form method="POST" action="/save-settings">

<h3>General</h3>

Bot Enabled:
<input type="checkbox" name="bot_enabled" {checked(bot_enabled)}><br>

Mode:
<select name="mode">
<option value="sandbox" {selected("sandbox", mode)}>Sandbox</option>
<option value="live" {selected("live", mode)}>Live</option>
</select><br>

Ticker:
<select name="symbol_choice">
<option value="SPY" {selected("SPY", symbol)}>SPY</option>
<option value="NVDA" {selected("NVDA", symbol)}>NVDA</option>
<option value="GOOG" {selected("GOOG", symbol)}>GOOG</option>
<option value="GOOGL" {selected("GOOGL", symbol)}>GOOGL</option>
<option value="AAPL" {selected("AAPL", symbol)}>AAPL</option>
<option value="TSLA" {selected("TSLA", symbol)}>TSLA</option>
</select>

Custom:
<input name="custom_symbol" placeholder="Type ticker here"><br>

Asset Type:
<select name="asset_type">
<option value="option" {selected("option", asset)}>Option</option>
<option value="stock" {selected("stock", asset)}>Stock</option>
</select><br>

Contracts:
<input type="number" name="contracts" value="{contracts}" min="1"><br>

Strategy Mode:
<select name="strategy_mode">
<option value="SURFER" {selected("SURFER", strategy_mode)}>SURFER</option>
<option value="TSUNAMI" {selected("TSUNAMI", strategy_mode)}>TSUNAMI</option>
</select><br>

<h3>Indicators</h3>

EMA Fast:
<input type="number" name="ema_fast" value="{s.get("ema_fast")}"><br>

EMA Medium:
<input type="number" name="ema_medium" value="{s.get("ema_medium")}"><br>

EMA Slow:
<input type="number" name="ema_slow" value="{s.get("ema_slow")}"><br>

MA Fast:
<input type="number" name="ma_fast" value="{s.get("ma_fast")}"><br>

MA Medium:
<input type="number" name="ma_medium" value="{s.get("ma_medium")}"><br>

MA Slow:
<input type="number" name="ma_slow" value="{s.get("ma_slow")}"><br>

Use MACD:
<input type="checkbox" name="use_macd" {checked(s.get("use_macd"))}><br>

Use VWAP:
<input type="checkbox" name="use_vwap" {checked(s.get("use_vwap"))}><br>

Use Volume:
<input type="checkbox" name="use_volume" {checked(s.get("use_volume"))}><br>

<h3>Entry Rules</h3>

EMA Alignment:
<input type="checkbox" name="ema_alignment" {checked(e.get("ema_alignment"))}><br>

MACD Confirmation:
<input type="checkbox" name="macd_confirmation" {checked(e.get("macd_confirmation"))}><br>

VWAP Confirmation:
<input type="checkbox" name="vwap_confirmation" {checked(e.get("vwap_confirmation"))}><br>

Volume Confirmation:
<input type="checkbox" name="volume_confirmation" {checked(e.get("volume_confirmation"))}><br>

Minimum Signals:
<input type="number" name="minimum_signals" value="{e.get("minimum_signals")}"><br>

Allow Calls:
<input type="checkbox" name="allow_calls" {checked(e.get("allow_calls"))}><br>

Allow Puts:
<input type="checkbox" name="allow_puts" {checked(e.get("allow_puts"))}><br>

Cooldown Minutes:
<input type="number" name="cooldown_minutes" value="{e.get("cooldown_minutes")}"><br>

Max Trades Per Day:
<input type="number" name="max_trades_per_day" value="{e.get("max_trades_per_day")}"><br>

<h3>Risk</h3>

Hard Stop %:
<input type="number" step="0.1" name="hard_stop_percent" value="{s.get("hard_stop_percent")}"><br>

Trailing Stop %:
<input type="number" step="0.1" name="trailing_stop_percent" value="{s.get("trailing_stop_percent")}"><br>

<h3>Opening Direction</h3>

Tick Interval Seconds:
<input type="number" name="tick_interval_seconds" value="{s.get("tick_interval_seconds")}"><br>

Direction Threshold %:
<input type="number" step="0.1" name="direction_threshold_percent" value="{s.get("direction_threshold_percent")}"><br>

Bot Scan Every Seconds:
<input type="number" name="interval_seconds" value="{config.get("scanner", {}).get("interval_seconds", 60)}"><br><br>

<button type="submit">Save Settings</button>

</form>
</div>

<script>
const CURRENT_SYMBOL = "{symbol}";

async function loadExpirations() {{
    const res = await fetch(`/api/expirations?symbol=${{CURRENT_SYMBOL}}`);
    const data = await res.json();

    const expSelect = document.getElementById("expiration_select");
    expSelect.innerHTML = "";

    data.dates.forEach(date => {{
        const opt = document.createElement("option");
        opt.value = date;
        opt.textContent = date;
        expSelect.appendChild(opt);
    }});

    await loadChain();
}}

async function loadChain() {{
    const exp = document.getElementById("expiration_select").value;
    const type = document.getElementById("option_type_select").value;

    if (!exp) return;

    const res = await fetch(`/api/chain?symbol=${{CURRENT_SYMBOL}}&expiration=${{exp}}&option_type=${{type}}`);
    const data = await res.json();

    const strikeSelect = document.getElementById("strike_select");
    strikeSelect.innerHTML = "";

    data.options.forEach(o => {{
        const opt = document.createElement("option");
        opt.value = JSON.stringify(o);
        opt.textContent = `${{o.strike}} - ${{o.symbol}}`;
        strikeSelect.appendChild(opt);
    }});

    updateSelectedContract();
}}

function updateSelectedContract() {{
    const strikeSelect = document.getElementById("strike_select");

    if (!strikeSelect.value) return;

    const o = JSON.parse(strikeSelect.value);

    document.getElementById("selected_contract").textContent = o.symbol;
    document.getElementById("option_symbol_input").value = o.symbol;
    document.getElementById("bid").textContent = o.bid ?? "N/A";
    document.getElementById("ask").textContent = o.ask ?? "N/A";
    document.getElementById("last").textContent = o.last ?? "N/A";
    document.getElementById("volume").textContent = o.volume ?? "N/A";
    document.getElementById("open_interest").textContent = o.open_interest ?? "N/A";
}}

document.addEventListener("DOMContentLoaded", async () => {{
    await loadExpirations();

    document.getElementById("expiration_select").addEventListener("change", loadChain);
    document.getElementById("option_type_select").addEventListener("change", loadChain);
    document.getElementById("strike_select").addEventListener("change", updateSelectedContract);
}});
</script>

</body>
</html>
"""

    return html


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)
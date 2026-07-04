import json

with open("config.json", "r") as f:
    config = json.load(f)

print("Bot Enabled:", config["bot_enabled"])
print("Mode:", config["strategy_mode"])

print("EMA Fast:",
      config["strategy"]["ema_fast"])

print("EMA Medium:",
      config["strategy"]["ema_medium"])

print("EMA Slow:",
      config["strategy"]["ema_slow"])
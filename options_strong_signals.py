#!/usr/bin/env python3
"""Seven-coin direction-only alerts using closed Binance spot candles.

These are directional CALL/PUT ideas only. They are NOT option contracts,
option premiums, or instructions to place an order.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

COINS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "ZEC": "ZECUSDT", "BNB": "BNBUSDT", "SOL": "SOLUSDT", "DOGE": "DOGEUSDT", "XRP": "XRPUSDT"}
INTERVALS = ("5m", "15m")
BINANCE = "https://api.binance.com/api/v3/klines"
RSI_PERIOD = 14


def get_klines(symbol, interval, limit=80):
    query = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "limit": limit})
    req = urllib.request.Request(BINANCE + "?" + query, headers={"User-Agent": "market-signal-bot/1.0"})
    with urllib.request.urlopen(req, timeout=15) as response:
        rows = json.loads(response.read().decode("utf-8"))
    # Exclude the still-forming candle; only completed candles are evaluated.
    return rows[:-1]


def ema(values, period):
    alpha = 2 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = value * alpha + result * (1 - alpha)
    return result


def rsi(values, period=RSI_PERIOD):
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(x, 0) for x in changes[-period:]]
    losses = [max(-x, 0) for x in changes[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0 if avg_gain else 50.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def analyse(symbol):
    c5 = get_klines(symbol, "5m")
    c15 = get_klines(symbol, "15m")
    if len(c5) < 30 or len(c15) < 30:
        raise RuntimeError("Not enough completed candle data")

    close5 = [float(row[4]) for row in c5]
    close15 = [float(row[4]) for row in c15]
    rsi5 = rsi(close5)
    e9_5, e21_5 = ema(close5[-40:], 9), ema(close5[-40:], 21)
    e9_15, e21_15 = ema(close15[-60:], 9), ema(close15[-60:], 21)

    # Last completed 15m candle must itself confirm direction, not just EMA alignment.
    last15 = c15[-1]
    open15, high15, low15, close15_last = map(float, (last15[1], last15[2], last15[3], last15[4]))
    prior15_close = float(c15[-2][4])
    bullish15 = close15_last > open15 and close15_last > prior15_close
    bearish15 = close15_last < open15 and close15_last < prior15_close

    last5 = c5[-1]
    close5_last = float(last5[4])
    prior5_close = float(c5[-2][4])
    bullish5 = close5_last > prior5_close and close5_last > e9_5 > e21_5
    bearish5 = close5_last < prior5_close and close5_last < e9_5 < e21_5

    direction = None
    if bullish15 and e9_15 > e21_15 and bullish5 and 55 <= rsi5 <= 70:
        direction = "STRONG BUY CALL"
    elif bearish15 and e9_15 < e21_15 and bearish5 and 30 <= rsi5 <= 45:
        direction = "STRONG BUY PUT"

    return {
        "direction": direction,
        "price": close5_last,
        "rsi": rsi5,
        "ema9": e9_5,
        "ema21": e21_5,
        "candle15_open": open15,
        "candle15_close": close15_last,
        "candle15_close_time": int(last15[6]),
        "candle15_confirmed": bullish15 or bearish15,
    }


def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAMBOTTOKAN") or os.environ.get("TELEGRAM_BOT_TOKAN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Telegram bot token/chat ID secrets are missing")
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=20) as response:
        result = json.loads(response.read().decode())
    if not result.get("ok"):
        raise RuntimeError("Telegram API rejected the message")


def main():
    # Scheduled at minutes 2, 17, 32 and 47: check shortly after each 15m close.
    now_ms = int(time.time() * 1000)
    now_minute = datetime.now(timezone.utc).minute
    if now_minute not in (0, 1, 2, 15, 16, 17, 30, 31, 32, 45, 46, 47):
        print("Not in the 15m confirmation check window; no alert.")
        return

    sent = 0
    for coin, symbol in COINS.items():
        try:
            data = analyse(symbol)
            age_ms = now_ms - data["candle15_close_time"]
            # Avoid late/stale alerts and repeated alerts for the same 15m close.
            if age_ms < 0 or age_ms > 6 * 60 * 1000:
                print(f"{coin}: last completed 15m candle is outside the fresh confirmation window.")
                continue
            if not data["direction"]:
                print(f"{coin}: WAIT — no confirmed strong setup; no Telegram alert.")
                continue

            direction = data["direction"]
            side = "bullish" if "CALL" in direction else "bearish"
            message = (
                "🚨 CRYPTO DIRECTION SIGNAL (SIGNAL ONLY)\n"
                f"Coin: {coin}\n"
                f"🟢 {direction}\n"
                f"Underlying price: {data['price']:,.2f} USDT\n"
                f"15m candle: CLOSED and {side} confirmed\n"
                f"RSI(5m): {data['rsi']:.1f}\n"
                f"EMA9(5m): {data['ema9']:.2f} | EMA21(5m): {data['ema21']:.2f}\n"
                "Option premium/strike/expiry: UNAVAILABLE — not verified from CoinSwitch.\n"
                "Direction signal only; ₹500 option affordability is NOT verified.\n"
                "No order placed. Check CoinSwitch option chain manually before any decision."
            )
            send_telegram(message)
            sent += 1
            print(f"{coin}: sent {direction}")
        except Exception as exc:
            print(f"{coin}: data/alert error: {exc}", file=sys.stderr)
    print(f"Completed. Telegram alerts sent: {sent}")


if __name__ == "__main__":
    main()

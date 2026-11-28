#!/usr/bin/env python3
"""Experimental multi-timeframe crypto signal detector.

This is deliberately separate from options_strong_signals.py.
It reads completed Binance spot candles only and prints test candidates.
It does NOT fetch option contracts, send Telegram alerts, or place orders.
"""
import json
import time
import urllib.parse
import urllib.request

COINS = ("BTC", "ETH", "BNB", "ZEC")
BASE = "https://api.binance.com/api/v3/klines"
FALLBACK = "https://data-api.binance.vision/api/v3/klines"


def fetch(symbol, interval, limit=100):
    params = urllib.parse.urlencode({"symbol": symbol + "USDT", "interval": interval, "limit": limit})
    errors = []
    for base in (BASE, FALLBACK):
        try:
            req = urllib.request.Request(base + "?" + params, headers={"User-Agent": "market-dashboard-mtf-test/1.0"})
            with urllib.request.urlopen(req, timeout=15) as response:
                rows = json.loads(response.read().decode())
            if not isinstance(rows, list) or len(rows) < 35:
                raise RuntimeError("not enough candles")
            # Binance's last row is the still-forming candle; never use it.
            return rows[:-1], base.split("//")[1].split("/")[0]
        except Exception as exc:
            errors.append(type(exc).__name__ + ": " + str(exc)[:100])
    raise RuntimeError(" / ".join(errors))


def ema(values, period):
    alpha = 2.0 / (period + 1)
    value = values[0]
    for x in values[1:]:
        value = alpha * x + (1 - alpha) * value
    return value


def rsi(values, period=14):
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0 if avg_g else 50.0
    return 100 - 100 / (1 + avg_g / avg_l)


def frame(rows):
    closes = [float(x[4]) for x in rows]
    highs = [float(x[2]) for x in rows]
    lows = [float(x[3]) for x in rows]
    opens = [float(x[1]) for x in rows]
    return {
        "close": closes[-1], "previous_close": closes[-2],
        "ema9": ema(closes[-30:], 9), "ema21": ema(closes[-50:], 21),
        "previous_ema21": ema(closes[:-1][-50:], 21),
        "rsi": rsi(closes),
        "high": highs[-1], "low": lows[-1], "open": opens[-1],
        "previous_high": highs[-2], "previous_low": lows[-2],
        "candle_time": int(rows[-1][0]),
    }


def detect(coin):
    one, source = fetch(coin, "1m")
    five, source5 = fetch(coin, "5m")
    fifteen, source15 = fetch(coin, "15m")
    m1, m5, m15 = frame(one), frame(five), frame(fifteen)

    # 15m = direction confirmation, 5m = actual entry trigger,
    # 1m = early momentum check. These are test-only gates.
    call = (
        m15["close"] > m15["ema9"] > m15["ema21"] >= m15["previous_ema21"]
        and m5["close"] > m5["ema9"] > m5["ema21"]
        and m5["close"] > m5["previous_high"]
        and m5["rsi"] >= 52
        and m1["ema9"] > m1["ema21"] and m1["rsi"] >= 52
    )
    put = (
        m15["close"] < m15["ema9"] < m15["ema21"] <= m15["previous_ema21"]
        and m5["close"] < m5["ema9"] < m5["ema21"]
        and m5["close"] < m5["previous_low"]
        and m5["rsi"] <= 48
        and m1["ema9"] < m1["ema21"] and m1["rsi"] <= 48
    )
    direction = "BUY CALL" if call else "BUY PUT" if put else "NO CONFIRMED ENTRY"
    return {
        "coin": coin, "result": direction, "price": round(m5["close"], 6),
        "rsi_1m": round(m1["rsi"], 1), "rsi_5m": round(m5["rsi"], 1),
        "rsi_15m": round(m15["rsi"], 1),
        "ema_1m": "UP" if m1["ema9"] > m1["ema21"] else "DOWN",
        "ema_5m": "UP" if m5["ema9"] > m5["ema21"] else "DOWN",
        "ema_15m": "UP" if m15["ema9"] > m15["ema21"] else "DOWN",
        "candle_1m": m1["candle_time"], "candle_5m": m5["candle_time"],
        "candle_15m": m15["candle_time"],
        "sources": [source, source5, source15],
    }


def main():
    print("EXPERIMENTAL 1m + 5m + 15m scanner | no orders | no Telegram")
    errors = 0
    for coin in COINS:
        try:
            print(json.dumps(detect(coin), ensure_ascii=False))
        except Exception as exc:
            errors += 1
            print(json.dumps({"coin": coin, "result": "DATA UNAVAILABLE", "error": str(exc)}, ensure_ascii=False))
    print("Test completed; data errors:", errors)


if __name__ == "__main__":
    main()

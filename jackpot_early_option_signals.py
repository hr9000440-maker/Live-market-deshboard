#!/usr/bin/env python3
"""BTC/ETH early 5m Jackpot option alerts. Alert-only; never places orders."""
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone

from options_strong_signals import OPTION_API, choose_expiry, ema, get_json, rsi, telegram

COINS = ("BTC", "ETH")
STATE_PATH = "state/jackpot_early_signals.json"
BINANCE = "https://api.binance.com/api/v3/klines"
GATE = "https://api.gateio.ws/api/v4/spot/candlesticks"


def five_minute_candles(coin):
    symbol = coin + "USDT"
    errors = []
    # KuCoin public spot candles first, then Binance and Gate.io fallbacks.
    sources = [
        ("KuCoin", "https://api.kucoin.com/api/v1/market/candles?" + urllib.parse.urlencode({"symbol": coin + "-USDT", "type": "5min"})),
        ("Binance", BINANCE + "?" + urllib.parse.urlencode({"symbol": symbol, "interval": "5m", "limit": 80})),
        ("Binance data-api", "https://data-api.binance.vision/api/v3/klines?" + urllib.parse.urlencode({"symbol": symbol, "interval": "5m", "limit": 80})),
        ("Gate.io", GATE + "?" + urllib.parse.urlencode({"currency_pair": coin + "_USDT", "interval": "5m", "limit": 80})),
    ]
    for name, url in sources:
        try:
            payload = get_json(url, timeout=15)
            if name == "KuCoin":
                if payload.get("code") != "200000" or not isinstance(payload.get("data"), list):
                    raise RuntimeError("KuCoin response unavailable")
                raw = payload["data"]
                rows = []
                for x in raw:
                    if len(x) < 6:
                        continue
                    ts = int(float(x[0])) * 1000
                    op, close, high, low, vol = map(float, (x[1], x[2], x[3], x[4], x[5]))
                    rows.append([ts, str(op), str(high), str(low), str(close), str(vol), ts + 299999])
                rows.sort(key=lambda x: x[0])
            elif name == "Gate.io":
                rows = [[int(float(x[0]) * 1000), x[5], x[3], x[4], x[2], x[1], int(float(x[0]) * 1000 + 299999)] for x in payload]
                rows.sort(key=lambda x: int(x[0]))
            else:
                rows = payload
            rows = [x for x in rows if isinstance(x, list) and len(x) >= 7 and all(float(x[i]) > 0 for i in (0, 1, 2, 3, 4))]
            if len(rows) >= 35:
                return rows, name
            errors.append(name + ": candles कम")
        except Exception as exc:
            errors.append(name + ": " + str(exc)[:100])
    raise RuntimeError("5m market data unavailable: " + " | ".join(errors))



def best_zero_hero_option(coin, direction, underlying, payload):
    """Choose a liquid, low-premium OTM contract; avoid ultra-far lottery strikes."""
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise RuntimeError("Options gateway response में rows नहीं हैं")
    expiry = choose_expiry(rows)
    wanted = "C" if "CALL" in direction else "P"
    source = str(payload.get("source") or "").lower()
    quote_is_usd = any(x in source for x in ("paradex", "bybit", "binance"))
    candidates = []
    for row in rows:
        name = str(row.get("instrument_name") or "")
        parts = name.split("-")
        row_expiry = str(row.get("expiry") or (parts[1] if len(parts) > 1 else ""))
        if not name or row_expiry != expiry:
            continue
        typ = str(row.get("option_type") or "").upper()
        is_side = (typ in (wanted, "CALL" if wanted == "C" else "PUT")
                   or name.endswith("-" + wanted)
                   or name.endswith("-" + ("CALL" if wanted == "C" else "PUT")))
        if not is_side:
            continue
        try:
            strike = float(row.get("strike") or (parts[2] if len(parts) > 2 else 0))
            bid, ask = float(row.get("bid_price") or 0), float(row.get("ask_price") or 0)
            oi, vol = float(row.get("open_interest") or 0), float(row.get("volume") or 0)
        except (TypeError, ValueError):
            continue
        if strike <= 0 or bid <= 0 or ask < bid or ask <= 0 or (oi <= 0 and vol <= 0):
            continue
        otm_pct = ((strike - underlying) / underlying * 100) if wanted == "C" else ((underlying - strike) / underlying * 100)
        if not 0.5 <= otm_pct <= 3.0:
            continue
        mid = (bid + ask) / 2
        spread = (ask - bid) / mid if mid > 0 else 999
        if spread > 0.10:
            continue
        bid_usd = bid if quote_is_usd else bid * underlying
        ask_usd = ask if quote_is_usd else ask * underlying
        liquidity = min(oi + vol, 1000000.0)
        score = (ask_usd / underlying) + (abs(otm_pct - 1.5) * 0.0008) + (spread * 0.001) - (min(liquidity, 10000.0) * 1e-10)
        candidates.append({"name": name, "strike": strike, "expiry": expiry, "bid": bid_usd,
                           "ask": ask_usd, "entry": ask_usd, "spread": spread,
                           "otm_pct": otm_pct, "score": score})
    if not candidates:
        raise RuntimeError("0.5%-3% OTM में 10% तक spread और live liquidity वाला Zero Hero contract नहीं मिला")
    return min(candidates, key=lambda x: x["score"])

def early_signal(coin):
    rows, source = five_minute_candles(coin)
    now_ms = int(time.time() * 1000)
    latest_open = int(float(rows[-1][0]))
    if now_ms - latest_open > 11 * 60 * 1000 or latest_open > now_ms + 60000:
        raise RuntimeError("5m candle data पुराना है; signal रोका गया")
    closed = rows[:-1]
    if len(closed) < 30:
        raise RuntimeError("5m confirmation के लिए candles कम हैं")
    last = closed[-1]
    op, high, low, close, volume = (float(last[i]) for i in (1, 2, 3, 4, 5))
    candle_range = high - low
    if candle_range <= 0 or volume <= 0:
        return None
    closes = [float(x[4]) for x in closed]
    highs = [float(x[2]) for x in closed]
    lows = [float(x[3]) for x in closed]
    volumes = [float(x[5]) for x in closed]
    e9, e21 = ema(closes[-30:], 9), ema(closes[-50:], 21)
    rv = rsi(closes, 14)
    avg_vol = sum(volumes[-21:-1]) / 20
    vol_ratio = volume / avg_vol if avg_vol > 0 else 0
    body = abs(close - op) / candle_range
    location = (close - low) / candle_range
    prior_high, prior_low = max(highs[-4:-1]), min(lows[-4:-1])
    bull = close > prior_high and close > op and close > e9 and e9 >= e21 * 0.997 and 50 <= rv <= 74 and body >= 0.45 and location >= 0.70 and vol_ratio >= 1.20
    bear = close < prior_low and close < op and close < e9 and e9 <= e21 * 1.003 and 26 <= rv <= 50 and body >= 0.45 and location <= 0.30 and vol_ratio >= 1.20
    if bull:
        direction = "STRONG BUY CALL"
    elif bear:
        direction = "STRONG BUY PUT"
    else:
        return None
    return {"direction": direction, "price": close, "rsi": rv, "ema9": e9, "ema21": e21,
            "candle_time": int(float(last[0])), "source": source, "volume_ratio": vol_ratio}


def main():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            raise RuntimeError("Early Jackpot state format गलत है")
    except FileNotFoundError:
        state = {}
    except (OSError, ValueError) as exc:
        print("State read failed; refusing to overwrite: " + str(exc), file=sys.stderr)
        raise SystemExit(1)

    sent = errors = 0
    for coin in COINS:
        try:
            sig = early_signal(coin)
            if not sig:
                print(coin + ": 5m early conditions not met")
                continue
            payload = get_json(OPTION_API.format(coin=coin), timeout=25)
            underlying = float(payload.get("underlying") or 0)
            if underlying <= 0:
                raise RuntimeError("Options underlying unavailable")
            option = best_zero_hero_option(coin, sig["direction"], underlying, payload)
            key = coin + "|" + option["name"] + "|" + sig["direction"]
            stamp = str(sig["candle_time"])
            if state.get(key) == stamp:
                print(coin + ": duplicate 5m early alert suppressed")
                continue
            entry = float(option["entry"])
            sl, tp1, tp2, tp3 = entry * 0.75, entry * 2, entry * 5, entry * 10
            side = "BUY CALL" if "CALL" in sig["direction"] else "BUY PUT"
            msg = (
                "⚡ JACKPOT EARLY ALERT " + coin + " — 5m FIRST CONFIRMATION\n"
                + side + "\nContract: " + option["name"] + "\n"
                + f"Underlying: {underlying:,.2f} USD | Suggested LIMIT BUY: {entry:.4f} USD\n"
                + f"Bid: {option['bid']:.4f} | Ask: {option['ask']:.4f} | Spread: {option['spread']*100:.2f}%\n"
                + f"🛑 SL (-25%): {sl:.4f}\n🎯 TP1 (2x): {tp1:.4f}\n"
                + f"🎯 TP2 (5x): {tp2:.4f}\n🚀 TP3 (10x): {tp3:.4f}\n"
                + f"RSI14: {sig['rsi']:.1f} | EMA9: {sig['ema9']:.2f} | EMA21: {sig['ema21']:.2f}\n"
                + f"Volume: {sig['volume_ratio']:.2f}x | 5m candle source: {sig['source']}\n"
                + "यह 5m बंद कैंडल की शुरुआती पुष्टि है, exact पहला tick नहीं। 10x लक्ष्य अत्यधिक जोखिम वाला है; premium शून्य तक जा सकता है। Signal only — कोई order अपने-आप place नहीं होगा।"
            )
            telegram(msg)
            state[key] = stamp
            sent += 1
            print(coin + ": early alert sent " + option["name"])
        except Exception as exc:
            errors += 1
            print(coin + ": EARLY JACKPOT ERROR: " + str(exc), file=sys.stderr)

    os.makedirs("state", exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, STATE_PATH)
    print(f"Early Jackpot completed. Alerts sent: {sent}; coin errors: {errors}")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

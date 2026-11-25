#!/usr/bin/env python3
"""Server-side Telegram delivery for confirmed BTC/ETH/ZEC/DOGE option signals.

Signal rules, option quote filters, and budget gate mirror index.html.
Only completed 15m candles are used. No order is ever placed.
"""
import json, os, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

COINS = ("BTC", "ETH", "ZEC", "DOGE")
OPTION_API = "https://live-market-deshboard.onrender.com/api/options/{coin}"
BINANCE = "https://api.binance.com/api/v3/klines"
GATE = "https://api.gateio.ws/api/v4/spot/candlesticks"
USD_INR_BUFFER = 95.0
STATE_PATH = "state/option_telegram_signals.json"


def get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "live-market-dashboard/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def ema(values, period):
    alpha = 2.0 / (period + 1)
    value = values[0]
    for item in values[1:]:
        value = item * alpha + value * (1 - alpha)
    return value


def rsi(values, period=14):
    if len(values) < period + 1:
        raise RuntimeError("RSI के लिए candles कम हैं")
    gains = losses = 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        if change > 0:
            gains += change
        else:
            losses -= change
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0)) / period
    if avg_gain == 0 and avg_loss == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def candles(coin):
    symbol = coin + "USDT"
    errors = []
    for url, gate in (
        (BINANCE + "?" + urllib.parse.urlencode({"symbol": symbol, "interval": "15m", "limit": 60}), False),
        ("https://data-api.binance.vision/api/v3/klines?" + urllib.parse.urlencode({"symbol": symbol, "interval": "15m", "limit": 60}), False),
        (GATE + "?" + urllib.parse.urlencode({"currency_pair": coin + "_USDT", "interval": "15m", "limit": 60}), True),
    ):
        try:
            rows = get_json(url, timeout=15)
            if gate:
                rows = [[int(float(x[0]) * 1000), x[5], x[3], x[4], x[2], x[1], int(float(x[0]) * 1000 + 899999)] for x in rows]
                rows.sort(key=lambda x: int(x[0]))
            rows = [x for x in rows if isinstance(x, list) and len(x) >= 7 and all(float(x[i]) > 0 for i in (0, 1, 2, 3, 4))]
            if len(rows) >= 31:
                return rows, "Gate.io Spot" if gate else "Binance Spot"
            errors.append("candles कम")
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError(coin + " 15m market data unavailable: " + " | ".join(errors))


def confirmed_signal(coin):
    rows, source = candles(coin)
    latest_open = int(float(rows[-1][0]))
    age = int(time.time() * 1000) - latest_open
    if age < -60000 or age > 30 * 60 * 1000:
        raise RuntimeError("15m candle data पुराना है; सुरक्षित रूप से signal रोका गया")
    closed = rows[:-1]
    closes = [float(x[4]) for x in closed]
    close = closes[-1]
    e9, e21 = ema(closes[-30:], 9), ema(closes[-50:], 21)
    rv = rsi(closes, 14)
    direction = None
    if close > e9 > e21 and 45 <= rv <= 75:
        direction = "STRONG BUY CALL"
    elif close < e9 < e21 and 25 <= rv <= 55:
        direction = "STRONG BUY PUT"
    return {"direction": direction, "price": close, "rsi": rv, "ema9": e9, "ema21": e21,
            "candle_time": int(float(closed[-1][0])), "source": source}


def expiry_ms(value):
    value = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d%b%y", "%d%b%Y"):
        try:
            return int(datetime.strptime(value.upper(), fmt).replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except ValueError:
        pass
    try:
        n = int(value)
        return n if n > 10**12 else n * 1000
    except ValueError:
        return 0


def choose_expiry(rows):
    expiries = sorted({str(x.get("expiry") or x.get("instrument_name", "").split("-")[1]) for x in rows
                       if x.get("expiry") or len(x.get("instrument_name", "").split("-")) > 1})
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    today_ms = int(datetime(today.year, today.month, today.day, tzinfo=timezone.utc).timestamp() * 1000)
    for value in expiries:
        if expiry_ms(value) == today_ms:
            return value
    future = [x for x in expiries if expiry_ms(x) >= today_ms]
    return min(future, key=expiry_ms) if future else (expiries[0] if expiries else "")


def best_option(coin, signal, underlying, payload):
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise RuntimeError("Options gateway response में rows नहीं हैं")
    expiry = choose_expiry(rows)
    wanted_side = "C" if "CALL" in signal else "P"
    candidates = []
    for row in rows:
        name = str(row.get("instrument_name") or "")
        parts = name.split("-")
        if not name or not expiry or str(row.get("expiry") or (parts[1] if len(parts) > 1 else "")) != expiry:
            continue
        option_type = str(row.get("option_type") or "").upper()
        is_wanted = (option_type in (wanted_side, "CALL" if wanted_side == "C" else "PUT")
                     or name.endswith("-" + wanted_side)
                     or name.endswith("-" + ("CALL" if wanted_side == "C" else "PUT")))
        if not is_wanted:
            continue
        bid, ask, mark = (float(row.get(k) or 0) for k in ("bid_price", "ask_price", "mark_price"))
        oi, vol = float(row.get("open_interest") or 0), float(row.get("volume") or 0)
        strike = float(row.get("strike") or (parts[2] if len(parts) > 2 else 0) or 0)
        min_amount = float(row.get("min_trade_amount") or row.get("min_order_size") or row.get("order_size_increment") or 0)
        if not (bid > 0 and ask >= bid and mark > 0 and strike > 0 and (oi > 0 or vol > 0)):
            continue
        mid = (bid + ask) / 2.0
        spread = (ask - bid) / mid if mid > 0 else 999
        if spread > 0.10:
            continue
        # Deribit BTC/ETH premiums are coin-denominated. Paradex ZEC and
        # Bybit/Binance DOGE option premiums are already USD/USDT-denominated.
        source = str(payload.get("source") or "").lower()
        quote_is_usd = any(x in source for x in ("paradex", "bybit", "binance"))
        bid_usd = bid if quote_is_usd else bid * underlying
        ask_usd = ask if quote_is_usd else ask * underlying
        limit_usd = ask_usd
        # Alert-only scanner: no affordability gate and no order submission.
        estimated_inr = limit_usd * min_amount * USD_INR_BUFFER if min_amount > 0 else None
        candidates.append({"name": name, "strike": strike, "expiry": expiry, "bid": bid_usd,
                           "ask": ask_usd, "entry": limit_usd, "spread": spread,
                           "sl": limit_usd * 0.75, "tp1": limit_usd * 1.25, "tp2": limit_usd * 1.50,
                           "tp3": limit_usd * 1.75, "cost": estimated_inr})
    if not candidates:
        raise RuntimeError("इस expiry में 2% तक spread, live quote और OI/volume वाला eligible " + wanted_side + " contract नहीं मिला")
    return min(candidates, key=lambda x: (abs(x["strike"] - underlying), x["spread"]))


def telegram(message):
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAMBOTTOKAN") or
             os.getenv("TELEGRAM_BOT_TOKAN") or os.getenv("TELEGRAM_BOT_TOKAN"))
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Telegram token/chat ID repository secrets missing")
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request("https://api.telegram.org/bot" + token + "/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=20) as response:
        result = json.loads(response.read().decode())
    if not result.get("ok"):
        raise RuntimeError("Telegram API error: " + str(result.get("description", "unknown error")))


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def main():
    state = load_state()
    sent = 0
    errors = 0
    for coin in COINS:
        try:
            signal = confirmed_signal(coin)
            if not signal["direction"]:
                print(coin + ": no confirmed strong setup; no Telegram alert")
                continue
            payload = get_json(OPTION_API.format(coin=coin), timeout=25)
            underlying = float(payload.get("underlying") or 0)
            if underlying <= 0:
                raise RuntimeError("Options gateway underlying price unavailable")
            option = best_option(coin, signal["direction"], underlying, payload)
            key = coin + "|" + option["name"] + "|" + signal["direction"]
            stamp = str(signal["candle_time"])
            if state.get(key) == stamp:
                print(coin + ": duplicate signal already sent for this closed candle")
                continue
            side = "BUY CALL" if "CALL" in signal["direction"] else "BUY PUT"
            cost_line = (f"Estimated minimum cost: ₹{option['cost']:.0f}" if option["cost"] is not None
                         else "Estimated minimum cost: unavailable (lot metadata missing)")
            message = (
                "🚨 STRONG OPTION TRADE SIGNAL\n"
                f"Coin: {coin}\n{side}\nContract: {option['name']}\n"
                f"Strike: {option['strike']:,.2f} | Expiry: {option['expiry']}\n"
                f"Underlying: {underlying:,.2f} USD\n"
                f"Bid: {option['bid']:.4f} USD | Ask: {option['ask']:.4f} USD\n"
                f"Suggested LIMIT BUY: {option['entry']:.4f} USD\n"
                f"🛑 SL (-25%): {option['sl']:.4f} USD\n"
                f"🎯 TP1 (+25%): {option['tp1']:.4f} USD\n"
                f"🎯 TP2 (+50%): {option['tp2']:.4f} USD\n"
                f"Spread: {option['spread']*100:.2f}% (max 10%) | {cost_line}\n"
                f"RSI 14: {signal['rsi']:.1f} | EMA9: {signal['ema9']:.2f} | EMA21: {signal['ema21']:.2f}\n"
                f"15m closed-candle source: {signal['source']}\n"
                "LIMIT price is a suggestion at current ASK; quote may change. Signal only — कोई order अपने-आप place नहीं होगा।"
            )
            telegram(message)
            state[key] = stamp
            save_state(state)
            sent += 1
            print(coin + ": Telegram sent " + signal["direction"] + " " + option["name"])
        except Exception as exc:
            errors += 1
            print(coin + ": ERROR: " + str(exc), file=sys.stderr)
    print(f"Completed. Telegram alerts sent: {sent}; coin errors: {errors}")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

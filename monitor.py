import os, json, urllib.request, urllib.parse, datetime, sys

COINS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "ZECUSDT"]
STATE_PATH = ".signal-state/state.json"

def request_json(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 market-signal-monitor", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read())

def get_klines(symbol):
    errors = []
    # Binance public spot candles, using the official public mirrors.
    base = "https://api.binance.com/api/v3/klines"
    query = urllib.parse.urlencode({"symbol": symbol, "interval": "15m", "limit": 100})
    for host in ("api.binance.com", "api1.binance.com", "api2.binance.com",
                 "api3.binance.com", "data-api.binance.vision"):
        try:
            payload = request_json(f"https://{host}/api/v3/klines?{query}")
            if isinstance(payload, list) and len(payload) >= 30:
                print(f"{symbol}: market data source Binance ({host})")
                return payload
            raise RuntimeError(f"Unexpected Binance response: {str(payload)[:180]}")
        except Exception as exc:
            errors.append(f"Binance {host}: {exc}")

    # Independent public-data fallback. Bybit returns newest candles first.
    try:
        bybit_symbol = symbol
        url = "https://api.bybit.com/v5/market/kline?" + urllib.parse.urlencode({
            "category": "spot", "symbol": bybit_symbol, "interval": "15", "limit": 100
        })
        payload = request_json(url)
        if payload.get("retCode") == 0:
            rows = payload.get("result", {}).get("list", [])
            if len(rows) >= 30:
                rows = list(reversed(rows))
                # Convert Bybit [start, open, high, low, close, volume, turnover]
                # to Binance kline-compatible indices used below.
                converted = []
                for row in rows:
                    converted.append([int(row[0]), row[1], row[2], row[3], row[4],
                                      row[5], int(row[0]) + 900000])
                print(f"{symbol}: market data source Bybit public spot fallback")
                return converted
            raise RuntimeError(f"Insufficient Bybit candles: {len(rows)}")
        raise RuntimeError(f"Bybit API {payload.get('retCode')}: {payload.get('retMsg')}")
    except Exception as exc:
        errors.append(f"Bybit: {exc}")

    raise RuntimeError("All public candle sources failed: " + " | ".join(errors[-3:]))

def ema(values, period):
    k = 2 / (period + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(value * k + out[-1] * (1 - k))
    return out

def rsi(values, period=14):
    gains, losses = [], []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100
    return 100 - 100 / (1 + avg_gain / avg_loss)

def send(msg):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print("Telegram secret status: TELEGRAM_BOT_TOKEN=" +
              ("present" if token else "MISSING") + ", TELEGRAM_CHAT_ID=" +
              ("present" if chat else "MISSING"))
        print("Telegram alert not sent because a required secret is missing.")
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        headers={"User-Agent": "market-signal-monitor"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read())
        if result.get("ok"):
            print("Telegram delivery: SUCCESS")
            return True
        print("Telegram delivery: FAILED - " + str(result.get("description", "unknown Telegram API error")))
    except Exception as exc:
        print("Telegram delivery: FAILED - " + str(exc))
    return False

state = {}
try:
    with open(STATE_PATH) as file:
        state = json.load(file)
except Exception:
    pass

now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
data_errors = 0

for symbol in COINS:
    try:
        klines = get_klines(symbol)
        if not isinstance(klines, list) or len(klines) < 30:
            raise RuntimeError("Unexpected or insufficient candle data")

        closes = [float(row[4]) for row in klines]
        highs = [float(row[2]) for row in klines]
        lows = [float(row[3]) for row in klines]
        times = [int(row[0]) for row in klines]

        # Exclude the still-forming 15-minute candle.
        closes, highs, lows, times = closes[:-1], highs[:-1], lows[:-1], times[:-1]
        e9, e21, rv = ema(closes, 9), ema(closes, 21), rsi(closes)
        price, candle_ts = closes[-1], times[-1]

        # Early-movement signals: do not wait for EMA9/EMA21 full alignment.
        # Use the last CLOSED 15m candle, price vs EMA9, RSI momentum,
        # and either EMA trend alignment or a fresh EMA9/EMA21 breakout.
        direction = "WAIT"
        prev_price = closes[-2]
        prev_e9 = e9[-2]
        prev_e21 = e21[-2]
        bullish_early = (
            price > e9[-1] and rv >= 45 and rv <= 78 and
            (e9[-1] >= e21[-1] or (price > e21[-1] and prev_price <= prev_e9))
        )
        bearish_early = (
            price < e9[-1] and rv >= 22 and rv <= 55 and
            (e9[-1] <= e21[-1] or (price < e21[-1] and prev_price >= prev_e9))
        )
        if bullish_early:
            direction = "BUY / LONG"
        elif bearish_early:
            direction = "SELL / SHORT"

        true_ranges = []
        for i in range(max(1, len(closes) - 14), len(closes)):
            true_ranges.append(max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1])
            ))
        atr = sum(true_ranges) / len(true_ranges) if true_ranges else 0
        sl = (price - 1.2 * atr if direction == "BUY / LONG" else
              price + 1.2 * atr if direction == "SELL / SHORT" else None)
        risk = abs(price - sl) if sl is not None else None
        tp1 = (price + risk if direction == "BUY / LONG" else
               price - risk if direction == "SELL / SHORT" else None)
        tp2 = (price + 2 * risk if direction == "BUY / LONG" else
               price - 2 * risk if direction == "SELL / SHORT" else None)
        tp3 = (price + 3 * risk if direction == "BUY / LONG" else
               price - 3 * risk if direction == "SELL / SHORT" else None)

        previous = state.get(symbol, {})
        is_new_signal = direction != "WAIT" and (
            previous.get("direction") != direction or previous.get("candle") != candle_ts
        )

        # Do not mark a signal as delivered unless Telegram confirms success.
        delivered = True
        if is_new_signal:
            candle_time = datetime.datetime.fromtimestamp(
                candle_ts / 1000, datetime.timezone.utc
            ).strftime("%Y-%m-%d %H:%M UTC")
            message = (
                f"🚦 TRADE SIGNAL — {symbol}\n{direction}\n"
                f"Closed 15m candle: {candle_time}\n"
                f"Entry: {price:.8g}\nStop Loss: {sl:.8g}\n"
                f"TP1: {tp1:.8g}\nTP2: {tp2:.8g}\nTP3: {tp3:.8g}\n"
                f"EMA9: {e9[-1]:.8g} | EMA21: {e21[-1]:.8g} | RSI14: {rv:.1f}\n"
                f"Signal only; no automatic order placement.\nChecked: {now}"
            )
            delivered = send(message)

        if not is_new_signal or delivered:
            state[symbol] = {
                "direction": direction, "candle": candle_ts, "price": price,
                "ema9": e9[-1], "ema21": e21[-1], "rsi": rv,
                "entry": price, "sl": sl, "tp1": tp1, "tp2": tp2,
                "tp3": tp3, "checked": now
            }
        else:
            print(f"{symbol}: signal delivery failed; will retry on next scan")

        print(f"{symbol}: {direction} price={price} RSI={rv:.1f}")

    except Exception as exc:
        data_errors += 1
        print(f"{symbol}: DATA ERROR: {exc}")

os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
with open(STATE_PATH, "w") as file:
    json.dump(state, file, indent=2)

if data_errors:
    print(f"WARNING: {data_errors} coin(s) had data errors; next scheduled scan will retry.")
    sys.exit(1)

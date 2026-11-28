#!/usr/bin/env python3
"""Server-side Telegram delivery for confirmed BTC/ETH/BNB/ZEC/DOGE option signals.

Signal rules, option quote filters, and budget gate mirror index.html.
Completed 5m candles trigger entries; completed 15m candles filter the broader trend. No order is ever placed.
"""
import json, os, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

COINS = ("BTC", "ETH", "BNB", "ZEC", "DOGE")
OPTION_API = "https://live-market-deshboard.onrender.com/api/options/{coin}"
BINANCE = "https://api.binance.com/api/v3/klines"
GATE = "https://api.gateio.ws/api/v4/spot/candlesticks"
USD_INR_BUFFER = 95.0
STATE_PATH = "state/option_telegram_signals.json"


def get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "live-market-dashboard/1.0", "Accept": "application/json"})
    errors = []
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            errors.append("HTTP " + str(exc.code) + ": " + detail[:120])
            if exc.code not in (408, 425, 429) and exc.code < 500:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            errors.append(type(exc).__name__ + ": " + str(exc)[:120])
        if attempt < 2:
            time.sleep(2 ** attempt)
    raise RuntimeError("Public data request failed after retries: " + " | ".join(errors))


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
    """Read 15m SPOT candles only. Options contracts/premiums still come from OPTION_API."""
    symbol = coin + "USDT"
    errors = []

    # KuCoin public SPOT candles: independent fallback when Binance returns 401/451.
    try:
        kucoin_symbol = coin + "-USDT"
        url = "https://api.kucoin.com/api/v1/market/candles?" + urllib.parse.urlencode({
            "symbol": kucoin_symbol, "type": "15min"
        })
        payload = get_json(url, timeout=15)
        data = payload.get("data") if isinstance(payload, dict) else None
        if payload.get("code") != "200000" or not isinstance(data, list):
            raise RuntimeError("KuCoin response unavailable: " + str(payload.get("msg", payload.get("code", "unknown"))))
        rows = []
        for item in data:
            if not isinstance(item, list) or len(item) < 6:
                continue
            ts = int(float(item[0])) * 1000
            op, close, high, low, volume = (float(item[i]) for i in (1, 2, 3, 4, 5))
            if min(op, close, high, low) <= 0 or volume < 0:
                continue
            rows.append([ts, str(op), str(high), str(low), str(close), str(volume), ts + 899999])
        rows.sort(key=lambda x: int(x[0]))
        if len(rows) >= 31:
            return rows, "KuCoin Spot"
        errors.append("KuCoin: 15m candles कम")
    except Exception as exc:
        errors.append("KuCoin: " + str(exc)[:160])

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
            errors.append(str(exc)[:160])
    raise RuntimeError(coin + " 15m SPOT market data unavailable: " + " | ".join(errors))


def macd(values, fast=12, slow=26, signal=9):
    line = []
    for i in range(len(values)):
        sample = values[:i + 1]
        line.append(ema(sample, fast) - ema(sample, slow))
    signal_line = ema(line[-signal * 3:], signal)
    return line[-1], signal_line, line[-1] - signal_line


def trend_strength(highs, lows, closes, period=14):
    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(closes)):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(max(up, 0.0) if up > down else 0.0)
        minus_dm.append(max(down, 0.0) if down > up else 0.0)
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    if len(trs) < period:
        return 0.0, 0.0
    dx_values = []
    for end in range(period, len(trs) + 1):
        tr_sum = sum(trs[end - period:end])
        if tr_sum <= 0:
            dx_values.append(0.0)
            continue
        pdi = 100.0 * sum(plus_dm[end - period:end]) / tr_sum
        mdi = 100.0 * sum(minus_dm[end - period:end]) / tr_sum
        denom = pdi + mdi
        dx_values.append(100.0 * abs(pdi - mdi) / denom if denom else 0.0)
    adx = sum(dx_values[-period:]) / min(period, len(dx_values))
    atr = sum(trs[-period:]) / period
    return adx, atr


def candles_5m(coin):
    """Fetch closed 5m spot candles for earlier entries; never place orders."""
    symbol = coin + "USDT"
    errors = []
    urls = (
        ("https://api.binance.com/api/v3/klines?" + urllib.parse.urlencode({"symbol": symbol, "interval": "5m", "limit": 60}), False),
        ("https://data-api.binance.vision/api/v3/klines?" + urllib.parse.urlencode({"symbol": symbol, "interval": "5m", "limit": 60}), False),
        ("https://api.gateio.ws/api/v4/spot/candlesticks?" + urllib.parse.urlencode({"currency_pair": coin + "_USDT", "interval": "5m", "limit": 60}), True),
    )
    for url, gate in urls:
        try:
            rows = get_json(url, timeout=15)
            if gate:
                rows = [[int(float(x[0]) * 1000), x[5], x[3], x[4], x[2], x[1], int(float(x[0]) * 1000 + 299999)] for x in rows]
                rows.sort(key=lambda x: int(x[0]))
            rows = [x for x in rows if isinstance(x, list) and len(x) >= 7
                    and all(float(x[i]) > 0 for i in (0, 1, 2, 3, 4))]
            if len(rows) >= 31:
                return rows, "Gate.io Spot" if gate else "Binance Spot"
            errors.append("5m candles कम")
        except Exception as exc:
            errors.append(str(exc)[:140])
    raise RuntimeError(coin + " 5m market data unavailable: " + " | ".join(errors))



def candles_1h(coin):
    """Fetch closed 1h spot candles; a failed feed must suppress, not weaken, the signal."""
    symbol = coin + "USDT"
    urls = (
        "https://api.binance.com/api/v3/klines?" + urllib.parse.urlencode({"symbol": symbol, "interval": "1h", "limit": 60}),
        "https://data-api.binance.vision/api/v3/klines?" + urllib.parse.urlencode({"symbol": symbol, "interval": "1h", "limit": 60}),
    )
    errors = []
    for url in urls:
        try:
            rows = get_json(url, timeout=15)
            rows = [x for x in rows if isinstance(x, list) and len(x) >= 7
                    and all(float(x[i]) > 0 for i in (0, 1, 2, 3, 4))]
            if len(rows) < 31:
                errors.append("1h candles कम")
                continue
            now_ms = int(time.time() * 1000)
            closed = [x for x in rows if int(float(x[0])) + 3600000 <= now_ms]
            if len(closed) >= 31:
                return closed, "Binance Spot 1h"
            errors.append("1h closed candles कम")
        except Exception as exc:
            errors.append(str(exc)[:140])
    raise RuntimeError(coin + " 1h trend data unavailable: " + " | ".join(errors))


def candles_1m(coin):
    """Fetch public 1m spot candles for fresh impulse detection; options quotes remain unchanged."""
    symbol = coin + "USDT"
    errors = []
    urls = (
        ("https://api.binance.com/api/v3/klines?" + urllib.parse.urlencode({"symbol": symbol, "interval": "1m", "limit": 100}), False),
        ("https://data-api.binance.vision/api/v3/klines?" + urllib.parse.urlencode({"symbol": symbol, "interval": "1m", "limit": 100}), False),
    )
    for url, gate in urls:
        try:
            rows = get_json(url, timeout=15)
            rows = [x for x in rows if isinstance(x, list) and len(x) >= 7
                    and all(float(x[i]) > 0 for i in (0, 1, 2, 3, 4))]
            if len(rows) >= 31:
                return rows, "Gate.io Spot" if gate else "Binance Spot"
            errors.append("1m candles कम")
        except Exception as exc:
            errors.append(str(exc)[:120])
    raise RuntimeError(coin + " 1m market data unavailable: " + " | ".join(errors))


def confirmed_signal(coin):
    # 1m detects a fresh impulse; a completed 5m candle must confirm it;
    # the completed 15m EMA21 trend remains the final safety filter.
    trend_rows, trend_source = candles(coin)
    hour_rows, hour_source = candles_1h(coin)
    rows, source = candles_5m(coin)
    try:
        one_rows, one_source = candles_1m(coin)
    except Exception as exc:
        # 1m is an optional early-entry enhancement; never disable the existing 5m path.
        one_rows, one_source = [], "unavailable"
        print(coin + ": 1m early feed unavailable; retaining original 5m signal path: " + str(exc)[:120])
    latest_open = int(float(rows[-1][0]))
    age = int(time.time() * 1000) - latest_open
    if age < -60000 or age > 15 * 60 * 1000:
        raise RuntimeError("5m candle data पुराना है; सुरक्षित रूप से signal रोका गया")
    closed = rows[:-1]
    trend_closed = trend_rows[:-1]
    now_ms = int(time.time() * 1000)
    one_closed = [x for x in one_rows if int(float(x[0])) + 60000 <= now_ms]
    if len(closed) < 31 or len(trend_closed) < 31:
        raise RuntimeError("5m/15m EMA-RSI calculation के लिए candles कम हैं")

    closes = [float(x[4]) for x in closed]
    highs = [float(x[2]) for x in closed]
    lows = [float(x[3]) for x in closed]
    volumes = [float(x[5]) for x in closed]
    close, previous_close = closes[-1], closes[-2]
    e9, e21 = ema(closes[-30:], 9), ema(closes[-50:], 21)
    previous_e9 = ema(closes[:-1][-30:], 9)
    rv = rsi(closes, 14)
    op, high, low, volume = (float(closed[-1][i]) for i in (1, 2, 3, 5))
    candle_range = max(high - low, 1e-12)
    body_ratio = abs(close - op) / candle_range
    close_location = (close - low) / candle_range
    avg_volume = sum(volumes[-21:-1]) / 20.0
    volume_ratio = volume / avg_volume if avg_volume > 0 else 0.0
    macd_line, macd_signal, macd_hist = macd(closes)
    adx_value, atr_value = trend_strength(highs, lows, closes)
    atr_pct = (atr_value / close * 100.0) if close else 0.0

    trend_closes = [float(x[4]) for x in trend_closed]
    trend_e21 = ema(trend_closes[-50:], 21)
    previous_trend_e21 = ema(trend_closes[:-1][-50:], 21)
    trend_close = trend_closes[-1]
    trend_bull_15m = trend_close >= trend_e21 and trend_e21 >= previous_trend_e21
    trend_bear_15m = trend_close <= trend_e21 and trend_e21 <= previous_trend_e21
    hour_closes = [float(x[4]) for x in hour_rows]
    hour_e21 = ema(hour_closes[-50:], 21)
    previous_hour_e21 = ema(hour_closes[:-1][-50:], 21)
    hour_close = hour_closes[-1]
    trend_bull_1h = hour_close > hour_e21 and hour_e21 > previous_hour_e21
    trend_bear_1h = hour_close < hour_e21 and hour_e21 < previous_hour_e21
    # 1h is context, not a hard veto: requiring 1h alignment delayed sharp reversals.
    # Keep 15m directional confirmation and the independent strict quality gate below.
    trend_bull = trend_bull_15m
    trend_bear = trend_bear_15m

    # Keep the original 5m breakout path as first priority.
    bullish = close > e9 > e21 and 42 <= rv <= 78 and close_location >= 0.52 and trend_bull
    bearish = close < e9 < e21 and 22 <= rv <= 58 and close_location <= 0.48 and trend_bear
    direction = "STRONG BUY CALL" if bullish else "STRONG BUY PUT" if bearish else None
    signal_time = int(float(closed[-1][0]))
    signal_source = source + " 5m / " + trend_source + " 15m / " + hour_source + " (1h context)"

    # Earlier-entry fallback: a fresh 1m impulse must be followed by a completed
    # 5m candle with matching direction and EMA9 slope. 15m trend still must agree;
    # 1h remains visible context and cannot by itself block a confirmed reversal.
    if not direction and len(one_closed) >= 30:
        fresh = [x for x in one_closed[-5:]
                 if now_ms - (int(float(x[0])) + 60000) <= 5 * 60 * 1000]
        for one in reversed(fresh):
            idx = one_closed.index(one)
            if idx < 25 or int(float(closed[-1][0])) + 300000 < int(float(one[0])) + 60000:
                continue
            one_closes = [float(x[4]) for x in one_closed[:idx + 1]]
            one_highs = [float(x[2]) for x in one_closed[:idx + 1]]
            one_lows = [float(x[3]) for x in one_closed[:idx + 1]]
            one_volumes = [float(x[5]) for x in one_closed[:idx + 1]]
            one_op, one_high, one_low, one_close, one_volume = (float(one[i]) for i in (1, 2, 3, 4, 5))
            one_range = max(one_high - one_low, 1e-12)
            one_body = abs(one_close - one_op) / one_range
            one_location = (one_close - one_low) / one_range
            one_e9 = ema(one_closes[-30:], 9)
            one_e21 = ema(one_closes[-50:], 21)
            one_prev_e9 = ema(one_closes[:-1][-30:], 9)
            one_rsi = rsi(one_closes, 14)
            one_move = abs(one_close - one_closes[-2]) / one_closes[-2] * 100 if one_closes[-2] else 0
            prior_high = max(one_highs[-4:-1])
            prior_low = min(one_lows[-4:-1])
            one_bull = (one_close > one_op and one_body >= 0.55 and one_location >= 0.60
                        and (one_close > prior_high or one_move >= 0.08)
                        and one_e9 > one_e21 and one_e9 >= one_prev_e9 and 42 <= one_rsi <= 75)
            one_bear = (one_close < one_op and one_body >= 0.55 and one_location <= 0.40
                        and (one_close < prior_low or one_move >= 0.08)
                        and one_e9 < one_e21 and one_e9 <= one_prev_e9 and 22 <= one_rsi <= 58)
            if one_bull:
                if close > op and close > e9 and e9 >= previous_e9 and rv >= 42 and close_location >= 0.52 and trend_bull:
                    direction = "STRONG BUY CALL"
            elif one_bear:
                if close < op and close < e9 and e9 <= previous_e9 and rv <= 58 and close_location <= 0.48 and trend_bear:
                    direction = "STRONG BUY PUT"
            if direction:
                signal_time = int(float(one[0]))
                signal_source = "1m early impulse / 5m context / " + trend_source + " 15m"
                print(f"{coin}: 1m early impulse confirmed by 5m + 15m filters | {direction} | 1m candle={signal_time}")
                break

    # Strict quality gate: a setup is not a trade signal unless participation,
    # trend strength, candle body and MACD all confirm the same direction.
    if direction:
        aligned_macd = (macd_hist > 0 if "CALL" in direction else macd_hist < 0)
        if adx_value < 20 or volume_ratio < 1.20 or body_ratio < 0.55 or not aligned_macd:
            print(f"{coin}: candidate rejected by strict quality gate | ADX={adx_value:.1f} volume={volume_ratio:.2f} body={body_ratio:.2f} MACD={macd_hist:.6g}")
            direction = None

    return {"direction": direction, "price": close, "rsi": rv, "ema9": e9, "ema21": e21,
            "volume_ratio": volume_ratio, "body_ratio": body_ratio,
            "macd": macd_line, "macd_signal": macd_signal, "macd_hist": macd_hist,
            "adx": adx_value, "atr": atr_value, "atr_pct": atr_pct,
            "candle_time": signal_time, "source": signal_source}

def expiry_ms(value):
    value = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d%b%y", "%d%b%Y"):
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
    wanted_side = "C" if "CALL" in signal else "P"
    # Do not let an illiquid nearest expiry suppress a valid later-expiry contract.
    # Keep every existing quote, OI/volume and <=10% spread gate unchanged.
    now = datetime.now(timezone.utc)
    today_ms = int(datetime(now.year, now.month, now.day, tzinfo=timezone.utc).timestamp() * 1000)
    expiry_rank = {}
    for row in rows:
        name = str(row.get("instrument_name") or "")
        parts = name.split("-")
        value = str(row.get("expiry") or (parts[1] if len(parts) > 1 else ""))
        stamp = expiry_ms(value)
        if value and stamp >= today_ms:
            expiry_rank[value] = stamp
    if not expiry_rank:
        raise RuntimeError("कोई current/future option expiry उपलब्ध नहीं")
    candidates = []
    for row in rows:
        name = str(row.get("instrument_name") or "")
        parts = name.split("-")
        expiry = str(row.get("expiry") or (parts[1] if len(parts) > 1 else ""))
        if not name or expiry not in expiry_rank:
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
        if not (bid > 0 and ask >= bid and mark > 0 and strike > 0 and oi > 0 and vol > 0):
            continue
        mid = (bid + ask) / 2.0
        spread = (ask - bid) / mid if mid > 0 else 999
        if spread > 0.02:
            continue
        # Deribit BTC/ETH premiums are coin-denominated. Paradex ZEC and
        # Bybit/Binance DOGE option premiums are already USD/USDT-denominated.
        source = str(payload.get("source") or "").lower()
        quote_is_usd = any(x in source for x in ("paradex", "bybit", "binance"))
        bid_usd = bid if quote_is_usd else bid * underlying
        ask_usd = ask if quote_is_usd else ask * underlying
        limit_usd = ask_usd
        # Alert-only scanner: no affordability gate and no order submission.
        # CoinSwitch ETH options screen shows 1 lot = 0.1 ETH. The upstream
        # Deribit min_trade_amount is not the CoinSwitch lot multiplier, so
        # use the visible CoinSwitch lot size for the ETH INR estimate only.
        # This affects the displayed estimate, never signal eligibility/delivery.
        cost_quantity = 0.1 if coin == "ETH" else min_amount
        estimated_inr = limit_usd * cost_quantity * USD_INR_BUFFER if cost_quantity > 0 else None
        candidates.append({"name": name, "strike": strike, "expiry": expiry, "expiry_rank": expiry_rank[expiry], "bid": bid_usd,
                           "ask": ask_usd, "entry": limit_usd, "spread": spread,
                           "sl": limit_usd * 0.75, "tp1": limit_usd * 1.25, "tp2": limit_usd * 1.50,
                           "tp3": limit_usd * 1.75, "cost": estimated_inr})
    if not candidates:
        raise RuntimeError("किसी current/future expiry में 10% तक spread, live quote और OI/volume वाला eligible " + wanted_side + " contract नहीं मिला")
    return min(candidates, key=lambda x: (x["expiry_rank"], abs(x["strike"] - underlying), x["spread"]))


def telegram(message):
    # Try configured token aliases and retry transient network / server failures.
    names = ("TELEGRAM_BOT_TOKEN", "TELEGRAMBOTTOKAN", "TELEGRAM_BOT_TOKAN", "TELEGRAM_BOT_FATHER")
    tokens = list(dict.fromkeys(os.getenv(name, "").strip() for name in names if os.getenv(name, "").strip()))
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not tokens or not chat_id:
        raise RuntimeError("Telegram token/chat ID repository secrets missing")
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"}).encode()
    errors = []
    for token in tokens:
        for attempt in range(3):
            req = urllib.request.Request("https://api.telegram.org/bot" + token + "/sendMessage", data=data)
            try:
                with urllib.request.urlopen(req, timeout=25) as response:
                    result = json.loads(response.read().decode())
                if result.get("ok"):
                    return
                detail = str(result.get("description", "unknown Telegram API error"))
                errors.append(detail)
                if "retry" in detail.lower() or "timeout" in detail.lower():
                    time.sleep(2 ** attempt)
                    continue
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")
                errors.append("HTTP " + str(exc.code) + ": " + detail[:200])
                if exc.code == 401:
                    break
                if exc.code == 429 or exc.code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                break
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                errors.append(type(exc).__name__ + ": " + str(exc)[:160])
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                break
    raise RuntimeError("Telegram delivery failed after retries: " + " | ".join(errors))


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
    skipped = 0
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
            # Suppress repeat entries in the same direction for 20 minutes,
            # even if the option contract/strike changes during one market move.
            direction_lock_key = "__last_direction__|" + coin
            previous_lock = state.get(direction_lock_key, {})
            if (previous_lock.get("direction") == signal["direction"]
                    and time.time() - float(previous_lock.get("sent_at", 0)) < 20 * 60):
                print(coin + ": same-direction repeat suppressed within 20-minute move lock")
                continue
            side = "BUY CALL" if "CALL" in signal["direction"] else "BUY PUT"
            cost_line = (f"Estimated premium value (not margin): ₹{option['cost']:.0f}" if option["cost"] is not None
                         else "Estimated premium value: unavailable (lot metadata missing)")
            quote_source = str(payload.get("source") or "unknown")
            warnings = []
            if signal["adx"] < 20:
                warnings.append("ADX 20 से कम: trend कमजोर")
            if signal["volume_ratio"] < 1:
                warnings.append("Volume average से कम: participation कमजोर")
            if ("CALL" in signal["direction"] and signal["macd_hist"] <= 0) or ("PUT" in signal["direction"] and signal["macd_hist"] >= 0):
                warnings.append("MACD momentum signal की दिशा में confirm नहीं")
            confirmation = "CAUTION — " + "; ".join(warnings) if warnings else "GOOD — ADX/MACD/Volume aligned"
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
                f"🏁 Final target (+75%): {option['tp3']:.4f} USD\n"
                f"Spread: {option['spread']*100:.2f}% (max 10%) | {cost_line}\n"
                f"Option quote source: {quote_source} (CoinSwitch price not live-verified)\n"
                f"RSI 14: {signal['rsi']:.1f} | EMA9: {signal['ema9']:.2f} | EMA21: {signal['ema21']:.2f}\n"
                f"Quality: volume {signal['volume_ratio']:.2f}x average | candle body {signal['body_ratio']*100:.0f}%\n"
                f"MACD: {signal['macd']:.4f} | Signal: {signal['macd_signal']:.4f} | Histogram: {signal['macd_hist']:.4f}\\n"
                f"ADX 14: {signal['adx']:.1f} (trend strength) | ATR 14: {signal['atr']:.4f} ({signal['atr_pct']:.2f}% volatility)\\n"
                f"Confirmation: {confirmation}\\n"
                f"15m closed-candle source: {signal['source']}\n"
                "LIMIT price is a suggestion at current ASK; quote may change. Signal only — कोई order अपने-आप place नहीं होगा।"
            )
            telegram(message)
            # Persist this exact contract for server-side premium / TP / SL monitoring.
            positions_path = "state/option_positions.json"
            try:
                with open(positions_path, "r", encoding="utf-8") as pf:
                    positions = json.load(pf)
                if not isinstance(positions, list):
                    positions = []
            except (OSError, ValueError):
                positions = []
            # Track every distinct confirmed-candle signal separately.
            # The Telegram dedupe key remains contract+direction, but re-signals
            # on later candles must never inherit an older entry/SL/TP record.
            position_id = key + "|" + stamp
            if not any(p.get("key") == position_id for p in positions):
                positions.append({
                    "key": position_id, "signal_key": key, "signal_candle_time": stamp,
                    "coin": coin, "contract": option["name"],
                    "signal_type": "NORMAL", "market_signal": signal["direction"],
                    "market_candle_time": signal["candle_time"],
                    "signal_rsi": signal["rsi"], "signal_ema9": signal["ema9"],
                    "signal_ema21": signal["ema21"],
                    "signal_volume_ratio": signal["volume_ratio"],
                    "signal_body_ratio": signal["body_ratio"],
                    "signal_macd": signal["macd"],
                    "signal_macd_signal": signal["macd_signal"],
                    "signal_macd_hist": signal["macd_hist"],
                    "signal_adx": signal["adx"],
                    "signal_atr": signal["atr"],
                    "signal_atr_pct": signal["atr_pct"],
                    "signal_confirmation": confirmation,
                    "market_data_source": signal["source"],
                    "source": quote_source, "entry": option["entry"],
                    "sl": option["sl"], "tp1": option["tp1"],
                    "tp2": option["tp2"], "tp3": option["tp3"],
                    "peak": option["entry"], "stage": 0,
                    "status": "ACTIVE", "created_at": datetime.now(timezone.utc).isoformat()
                })
                os.makedirs("state", exist_ok=True)
                with open(positions_path, "w", encoding="utf-8") as pf:
                    json.dump(positions, pf, ensure_ascii=False, indent=2)
                    pf.write("\n")
            state[key] = stamp
            state[direction_lock_key] = {"direction": signal["direction"], "sent_at": time.time(), "contract": option["name"]}
            save_state(state)
            sent += 1
            print(coin + ": Telegram sent " + signal["direction"] + " " + option["name"])
        except Exception as exc:
            message = str(exc)
            # A listed options market can have no executable live quote for the
            # current direction/expiry. This is a safe skip, not a scanner crash:
            # never fabricate a bid/ask or block valid signals for other coins.
            if "eligible" in message and "contract नहीं मिला" in message:
                skipped += 1
                print(coin + ": SKIPPED — no eligible live option quote; no alert sent: " + message)
                continue
            errors += 1
            print(coin + ": ERROR: " + message, file=sys.stderr)
    print(f"Completed. Telegram alerts sent: {sent}; safely skipped: {skipped}; coin errors: {errors}")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

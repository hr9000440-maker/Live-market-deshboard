#!/usr/bin/env python3
"""Independent four-coin Zero Hero scanner. Alert-only; never places orders.

Uses fresh completed 1m impulse candles for earlier movement capture; scans run on the GitHub schedule. Option ask premium
must be BTC ₹0-100 or ETH/BNB/ZEC ₹0-30 equivalent, with live bid/ask, OI/volume and <=10% spread.
"""
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone

from options_strong_signals import OPTION_API, candles, expiry_ms, ema, get_json, rsi, telegram


def macd(values, fast=12, slow=26, signal=9):
    line = []
    for i in range(len(values)):
        sample = values[:i + 1]
        line.append(ema(sample, fast) - ema(sample, slow))
    signal_line = ema(line[-signal * 3:], signal)
    return line[-1], signal_line, line[-1] - signal_line


def adx_value(highs, lows, closes, period=14):
    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(closes)):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(max(up, 0.0) if up > down else 0.0)
        minus_dm.append(max(down, 0.0) if down > up else 0.0)
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    if len(trs) < period:
        return 0.0
    dx = []
    for end in range(period, len(trs) + 1):
        tr_sum = sum(trs[end-period:end])
        if tr_sum <= 0:
            dx.append(0.0)
            continue
        pdi = 100.0 * sum(plus_dm[end-period:end]) / tr_sum
        mdi = 100.0 * sum(minus_dm[end-period:end]) / tr_sum
        dx.append(100.0 * abs(pdi-mdi) / (pdi+mdi) if pdi+mdi else 0.0)
    return sum(dx[-period:]) / min(period, len(dx))


def supertrend_direction(highs, lows, closes, period=10, multiplier=3.0):
    if len(closes) < period + 2:
        return "UNKNOWN"
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]),
               abs(lows[i]-closes[i-1])) for i in range(1, len(closes))]
    atrs = [None] * len(closes)
    for i in range(period, len(closes)):
        atrs[i] = sum(trs[i-period:i]) / period
    final_upper = final_lower = None
    trend = 1
    for i in range(period, len(closes)):
        mid = (highs[i] + lows[i]) / 2.0
        basic_upper = mid + multiplier * atrs[i]
        basic_lower = mid - multiplier * atrs[i]
        if final_upper is None:
            final_upper, final_lower = basic_upper, basic_lower
        else:
            prev_close = closes[i-1]
            final_upper = basic_upper if basic_upper < final_upper or prev_close > final_upper else final_upper
            final_lower = basic_lower if basic_lower > final_lower or prev_close < final_lower else final_lower
        if trend == 1 and closes[i] < final_lower:
            trend = -1
        elif trend == -1 and closes[i] > final_upper:
            trend = 1
    return "BULLISH" if trend == 1 else "BEARISH"


COINS = ("BTC", "ETH", "BNB", "ZEC")
STATE_PATH = "state/jackpot_early_signals.json"
BINANCE = "https://api.binance.com/api/v3/klines"
GATE = "https://api.gateio.ws/api/v4/spot/candlesticks"
USD_INR = 95.0
MIN_PREMIUM_INR = 0.0
MAX_PREMIUM_INR_BY_COIN = {"BTC": 200.0, "ETH": 200.0, "BNB": 200.0, "ZEC": 200.0}
MAX_SPREAD = 0.10


def five_minute_candles(coin):
    """Fetch 1m candles for early movement capture; keep function name for compatibility."""
    symbol = coin + "USDT"
    errors = []
    sources = [
        ("KuCoin", "https://api.kucoin.com/api/v1/market/candles?" + urllib.parse.urlencode({"symbol": coin + "-USDT", "type": "1min"})),
        ("Binance", BINANCE + "?" + urllib.parse.urlencode({"symbol": symbol, "interval": "1m", "limit": 100})),
        ("Binance data-api", "https://data-api.binance.vision/api/v3/klines?" + urllib.parse.urlencode({"symbol": symbol, "interval": "1m", "limit": 100})),
        ("Gate.io", GATE + "?" + urllib.parse.urlencode({"currency_pair": coin + "_USDT", "interval": "1m", "limit": 100})),
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
                    rows.append([ts, str(op), str(high), str(low), str(close), str(vol), ts + 59999])
                rows.sort(key=lambda x: x[0])
            elif name == "Gate.io":
                rows = [[int(float(x[0]) * 1000), x[5], x[3], x[4], x[2], x[1], int(float(x[0]) * 1000 + 59999)] for x in payload]
                rows.sort(key=lambda x: int(x[0]))
            else:
                rows = payload
            rows = [x for x in rows if isinstance(x, list) and len(x) >= 7 and all(float(x[i]) > 0 for i in (0, 1, 2, 3, 4))]
            if len(rows) >= 35:
                return rows, name
            errors.append(name + ": candles कम")
        except Exception as exc:
            errors.append(name + ": " + str(exc)[:100])
    raise RuntimeError("1m market data unavailable: " + " | ".join(errors))


def one_minute_early_signal(coin):
    """Detect a fresh 1m impulse early; this is not an entry by itself."""
    rows, source = five_minute_candles(coin)
    now_ms = int(time.time() * 1000)
    closed = [x for x in rows if int(float(x[0])) + 60000 <= now_ms]
    if len(closed) < 30:
        raise RuntimeError("1m closed candles कम हैं")
    # Inspect up to the latest five completed minutes. Ignore older triggers.
    fresh = [x for x in closed[-5:] if now_ms - (int(float(x[0])) + 60000) <= 5 * 60 * 1000]
    if not fresh:
        raise RuntimeError("1m trigger candle ताज़ा नहीं है")
    closes_all = [float(x[4]) for x in closed]
    highs_all = [float(x[2]) for x in closed]
    lows_all = [float(x[3]) for x in closed]
    volumes_all = [float(x[5]) for x in closed]
    chosen = None
    diagnostics = ""
    for last in reversed(fresh):
        idx = closed.index(last)
        if idx < 25:
            continue
        op, high, low, close, volume = (float(last[i]) for i in (1, 2, 3, 4, 5))
        candle_range = high - low
        if candle_range <= 0 or volume <= 0:
            continue
        closes = closes_all[:idx + 1]
        highs = highs_all[:idx + 1]
        lows = lows_all[:idx + 1]
        volumes = volumes_all[:idx + 1]
        e9, e21 = ema(closes[-30:], 9), ema(closes[-50:], 21)
        rv = rsi(closes, 14)
        avg_vol = sum(volumes[-21:-1]) / 20.0
        vol_ratio = volume / avg_vol if avg_vol > 0 else 0.0
        body = abs(close - op) / candle_range
        location = (close - low) / candle_range
        prior_high, prior_low = max(highs[-4:-1]), min(lows[-4:-1])
        true_ranges = []
        for j in range(max(1, len(closes) - 14), len(closes)):
            prev_close = closes[j - 1]
            hj, lj = highs[j], lows[j]
            true_ranges.append(max(hj - lj, abs(hj - prev_close), abs(lj - prev_close)))
        # Scale 1m ATR to a practical 5m-equivalent movement estimate.
        atr = (sum(true_ranges) / max(1, len(true_ranges))) * 2.2
        move_pct = abs(close - closes[-2]) / closes[-2] * 100
        impulse = body >= 0.55 and move_pct >= 0.08
        bull = close > op and location >= 0.60 and (close > prior_high or impulse)
        bear = close < op and location <= 0.40 and (close < prior_low or impulse)
        if bull or bear:
            direction = "STRONG BUY CALL" if bull else "STRONG BUY PUT"
            chosen = {"direction": direction, "price": close, "rsi": rv, "ema9": e9,
                      "ema21": e21, "candle_time": int(float(last[0])),
                      "source": source, "volume_ratio": vol_ratio, "body_ratio": body,
                      "atr": atr, "move_pct": move_pct,
                      "swing_low": min(lows[-6:]), "swing_high": max(highs[-6:])}
            break
        diagnostics = (f"{coin}: no 1m trigger | source={source} | close={close:.6f} "
                       f"| body={body:.2f} | move={move_pct:.3f}% | volume={vol_ratio:.2f}x | RSI={rv:.1f}")
    if chosen:
        print(f"{coin}: fresh 1m movement captured | source={source} | {chosen['direction']} "
              f"| move={chosen['move_pct']:.3f}% | candle={chosen['candle_time']}")
        return chosen
    print(diagnostics or f"{coin}: no fresh 1m trigger | source={source}")
    return None



def five_minute_signal(coin):
    """Use the most recent fresh completed 5m breakout candle as the entry trigger.

    Inspect the last three completed candles so a delayed GitHub Actions start
    does not silently discard a still-fresh breakout. All original breakout
    thresholds remain unchanged.
    """
    symbol = coin + "USDT"
    errors = []
    rows = None
    source = ""
    for name, url in [
        ("Binance", BINANCE + "?" + urllib.parse.urlencode({"symbol": symbol, "interval": "5m", "limit": 100})),
        ("Binance data-api", "https://data-api.binance.vision/api/v3/klines?" + urllib.parse.urlencode({"symbol": symbol, "interval": "5m", "limit": 100})),
        ("KuCoin", "https://api.kucoin.com/api/v1/market/candles?" + urllib.parse.urlencode({"symbol": coin + "-USDT", "type": "5min"})),
        ("Gate.io", GATE + "?" + urllib.parse.urlencode({"currency_pair": coin + "_USDT", "interval": "5m", "limit": 100})),
    ]:
        try:
            payload = get_json(url, timeout=15)
            if name == "KuCoin":
                if payload.get("code") != "200000" or not isinstance(payload.get("data"), list):
                    raise RuntimeError("KuCoin response unavailable")
                rows = []
                for x in payload["data"]:
                    ts = int(float(x[0])) * 1000
                    op, close, high, low, vol = map(float, (x[1], x[2], x[3], x[4], x[5]))
                    rows.append([ts, str(op), str(high), str(low), str(close), str(vol), ts + 299999])
                rows.sort(key=lambda x: x[0])
            elif name == "Gate.io":
                rows = [[int(float(x[0]) * 1000), x[5], x[3], x[4], x[2], x[1], int(float(x[0]) * 1000 + 299999)] for x in payload]
                rows.sort(key=lambda x: int(x[0]))
            else:
                rows = payload
            rows = [x for x in rows if isinstance(x, list) and len(x) >= 7]
            if len(rows) >= 35:
                source = name
                break
            errors.append(name + ": candles कम")
        except Exception as exc:
            errors.append(name + ": " + str(exc)[:100])
    if not rows or len(rows) < 35:
        raise RuntimeError("5m market data unavailable: " + " | ".join(errors))

    now_ms = int(time.time() * 1000)
    closed = [x for x in rows if int(float(x[0])) + 300000 <= now_ms]
    if len(closed) < 30:
        raise RuntimeError("5m closed candles कम हैं")

    closes = [float(x[4]) for x in closed]
    highs = [float(x[2]) for x in closed]
    lows = [float(x[3]) for x in closed]
    volumes = [float(x[5]) for x in closed]

    # Check up to three latest completed bars, but never use a stale trigger.
    for idx in range(len(closed) - 1, max(28, len(closed) - 4), -1):
        last = closed[idx]
        candle_close_ms = int(float(last[0])) + 300000
        if now_ms - candle_close_ms > 10 * 60 * 1000:
            continue
        op, high, low, close, volume = (float(last[i]) for i in (1, 2, 3, 4, 5))
        candle_range = high - low
        if candle_range <= 0 or volume <= 0:
            continue
        body = abs(close - op) / candle_range
        location = (close - low) / candle_range
        prior_high, prior_low = max(highs[idx - 3:idx]), min(lows[idx - 3:idx])
        bull = close > op and body >= 0.50 and location >= 0.65 and close > prior_high
        bear = close < op and body >= 0.50 and location <= 0.35 and close < prior_low
        if not (bull or bear):
            continue

        true_ranges = []
        for j in range(max(1, idx - 13), idx + 1):
            prev_close = closes[j - 1]
            true_ranges.append(max(highs[j] - lows[j], abs(highs[j] - prev_close), abs(lows[j] - prev_close)))
        atr = sum(true_ranges) / max(1, len(true_ranges))
        avg_vol = sum(volumes[max(0, idx - 20):idx]) / max(1, len(volumes[max(0, idx - 20):idx]))
        direction = "STRONG BUY CALL" if bull else "STRONG BUY PUT"
        sample_closes = closes[:idx + 1]
        sample_highs = highs[:idx + 1]
        sample_lows = lows[:idx + 1]
        macd_line, macd_signal, macd_hist = macd(sample_closes)
        adx = adx_value(sample_highs, sample_lows, sample_closes)
        st_direction = supertrend_direction(sample_highs, sample_lows, sample_closes)
        result = {
            "direction": direction, "price": close, "rsi": rsi(sample_closes, 14),
            "ema9": ema(sample_closes[-30:], 9),
            "ema21": ema(sample_closes[-50:], 21),
            "candle_time": int(float(last[0])), "source": source,
            "volume_ratio": volume / avg_vol if avg_vol > 0 else 0.0,
            "body_ratio": body, "atr": atr,
            "macd": macd_line, "macd_signal": macd_signal, "macd_hist": macd_hist,
            "adx": adx, "supertrend": st_direction,
            "move_pct": abs(close - closes[idx - 1]) / closes[idx - 1] * 100,
            "swing_low": min(lows[max(0, idx - 5):idx + 1]),
            "swing_high": max(highs[max(0, idx - 5):idx + 1])
        }
        print(f"{coin}: fresh 5m entry trigger found | source={source} | {direction} | candle={result['candle_time']}")
        return result

    last = closed[-1]
    candle_range = float(last[2]) - float(last[3])
    body = abs(float(last[4]) - float(last[1])) / candle_range if candle_range > 0 else 0.0
    location = (float(last[4]) - float(last[3])) / candle_range if candle_range > 0 else 0.0
    print(f"{coin}: no fresh 5m entry trigger | source={source} | body={body:.2f} | close-location={location:.2f}")
    return None

def confirm_15m(coin, direction):
    rows, source = candles(coin)
    now_ms = int(time.time() * 1000)
    latest_open = int(float(rows[-1][0]))
    if now_ms - latest_open > 30 * 60 * 1000 or latest_open > now_ms + 60000:
        raise RuntimeError("15m confirmation candle पुरानी है")
    closed = rows[:-1]
    if len(closed) < 30:
        raise RuntimeError("15m confirmation candles कम हैं")
    closes = [float(x[4]) for x in closed]
    close, previous_close = closes[-1], closes[-2]
    e9 = ema(closes[-30:], 9)
    e21 = ema(closes[-50:], 21)
    rv = rsi(closes, 14)
    highs = [float(x[2]) for x in closed]
    lows = [float(x[3]) for x in closed]
    macd_line, macd_signal, macd_hist = macd(closes)
    adx = adx_value(highs, lows, closes)
    st_direction = supertrend_direction(highs, lows, closes)
    # Preserve existing 15m confirmation and add Supertrend direction confirmation.
    if "CALL" in direction:
        ok = close > e9 and close >= previous_close and rv >= 40 and st_direction == "BULLISH"
    else:
        ok = close < e9 and close <= previous_close and rv <= 60 and st_direction == "BEARISH"
    if not ok:
        if coin == "ETH":
            checks = {
                "close_vs_ema9": close > e9 if "CALL" in direction else close < e9,
                "close_vs_previous": close >= previous_close if "CALL" in direction else close <= previous_close,
                "rsi_gate": rv >= 40 if "CALL" in direction else rv <= 60,
            }
            failed = [name for name, passed in checks.items() if not passed]
            print(
                f"ETH: 15m confirmation rejected {direction} | "
                f"close={close:.6f} | EMA9={e9:.6f} | previous_close={previous_close:.6f} "
                f"| RSI14={rv:.1f} | failed={','.join(failed) or 'unknown'} | source={source}"
            )
        return None
    if coin == "ETH":
        print(
            f"ETH: 15m confirmation passed {direction} | "
            f"close={close:.6f} | EMA9={e9:.6f} | RSI14={rv:.1f} | source={source}"
        )
    return {"close": close, "ema9": e9, "ema21": e21, "rsi": rv, "source": source,
            "macd": macd_line, "macd_signal": macd_signal, "macd_hist": macd_hist,
            "adx": adx, "supertrend": st_direction}


def best_zero_hero_option(coin, direction, underlying, payload):
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise RuntimeError("Options gateway response में rows नहीं हैं")
    wanted = "C" if "CALL" in direction else "P"
    source = str(payload.get("source") or "").lower()
    quote_is_usd = any(x in source for x in ("paradex", "bybit", "binance"))
    # Try every current/future expiry in order. Previously only the nearest
    # expiry was checked, so an illiquid/overpriced front expiry could suppress
    # a valid contract in the next expiry even when all other filters passed.
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_ms = int(today.timestamp() * 1000)
    expiries = sorted({
        str(row.get("expiry") or str(row.get("instrument_name") or "").split("-")[1])
        for row in rows
        if row.get("expiry") or len(str(row.get("instrument_name") or "").split("-")) > 1
    }, key=expiry_ms)
    expiries = [value for value in expiries if expiry_ms(value) >= today_ms]
    # ETH-only diagnostics: count the exact filter that rejects contracts.
    # This is logging only; it does not change eligibility or any coin's signals.
    eth_rejects = {"expiry": 0, "side": 0, "quote_or_liquidity": 0, "otm": 0, "spread": 0, "premium": 0}
    for expiry in expiries:
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
                if coin == "ETH": eth_rejects["side"] += 1
                continue
            try:
                strike = float(row.get("strike") or (parts[2] if len(parts) > 2 else 0))
                bid = float(row.get("bid_price") or 0)
                ask = float(row.get("ask_price") or 0)
                oi = float(row.get("open_interest") or 0)
                vol = float(row.get("volume") or 0)
            except (TypeError, ValueError):
                continue
            if strike <= 0 or bid <= 0 or ask < bid or ask <= 0 or (oi <= 0 and vol <= 0):
                if coin == "ETH": eth_rejects["quote_or_liquidity"] += 1
                continue
            otm_pct = ((strike - underlying) / underlying * 100) if wanted == "C" else ((underlying - strike) / underlying * 100)
            if not 0.5 <= otm_pct <= 3.0:
                if coin == "ETH": eth_rejects["otm"] += 1
                continue
            mid = (bid + ask) / 2.0
            spread = (ask - bid) / mid if mid > 0 else 999
            if spread > MAX_SPREAD:
                if coin == "ETH": eth_rejects["spread"] += 1
                continue
            bid_usd = bid if quote_is_usd else bid * underlying
            ask_usd = ask if quote_is_usd else ask * underlying
            ask_inr = ask_usd * USD_INR
            max_premium_inr = MAX_PREMIUM_INR_BY_COIN.get(coin, 30.0)
            if not MIN_PREMIUM_INR < ask_inr <= max_premium_inr:
                if coin == "ETH": eth_rejects["premium"] += 1
                continue
            liquidity = min(oi + vol, 1000000.0)
            score = abs(otm_pct - 1.5) * 0.001 + spread * 0.01 - min(liquidity, 10000.0) * 1e-9
            greeks = row.get("greeks") if isinstance(row.get("greeks"), dict) else {}
            delta = float(row.get("delta") or greeks.get("delta") or 0)
            gamma = float(row.get("gamma") or greeks.get("gamma") or 0)
            candidates.append({"name": name, "strike": strike, "expiry": expiry,
                               "bid": bid_usd, "ask": ask_usd, "entry": ask_usd,
                               "premium_inr": ask_inr, "spread": spread, "score": score,
                               "delta": delta, "gamma": gamma})
        if candidates:
            # Keep the earliest expiry with at least one contract passing every
            # existing premium, OTM, liquidity and spread condition.
            return min(candidates, key=lambda x: x["score"])
    detail = " | ETH filter counts: " + ", ".join(f"{k}={v}" for k, v in eth_rejects.items()) if coin == "ETH" else ""
    max_premium_inr = MAX_PREMIUM_INR_BY_COIN.get(coin, 30.0)
    raise RuntimeError(f"live {wanted} contract ₹0–₹{max_premium_inr:.0f} premium, <=10% spread और OI/volume के साथ नहीं मिला" + detail)


def monitor_positions(coin, payload, underlying, sig, state):
    positions = state.setdefault("__positions__", [])
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    changed = False
    for pos in positions:
        if pos.get("coin") != coin or pos.get("status") != "ACTIVE":
            continue
        quote = next((x for x in rows if str(x.get("instrument_name") or "") == pos.get("contract")), None)
        if not quote:
            continue
        try:
            bid = float(quote.get("bid_price") or 0)
            ask = float(quote.get("ask_price") or 0)
        except (TypeError, ValueError):
            continue
        if bid <= 0 or ask < bid:
            continue
        market = float(underlying)
        atr = float(sig["atr"]) if sig and float(sig.get("atr") or 0) > 0 else float(pos["atr"])
        call = pos["direction"] == "CALL"
        old_best = float(pos["best_underlying"])
        old_stop = float(pos["trail_underlying"])
        best = max(old_best, market) if call else min(old_best, market)
        candidate = best - 1.2 * atr if call else best + 1.2 * atr
        stop = max(old_stop, candidate) if call else min(old_stop, candidate)
        pos.update({"best_underlying": best, "trail_underlying": stop, "atr": atr, "last_bid": bid})
        targets = pos["targets"]
        stage = int(pos.get("target_stage", 0))
        while stage < len(targets) and (market >= targets[stage] if call else market <= targets[stage]):
            telegram("🚀 ZERO HERO TARGET HIT — " + coin + "\\n" + pos["contract"] + f"\\nUnderlying: {market:.4f} USD | Target {stage + 1} reached\\nOption bid: {bid:.6f} USD\\nNext target remains movement-based. Signal only.")
            stage += 1
        pos["target_stage"] = stage
        if stage >= len(targets):
            targets.append(best + (5.0 * atr if call else -5.0 * atr))
        stopped = market <= stop if call else market >= stop
        if stopped:
            pos["status"] = "CLOSED"
            telegram("🛑 ZERO HERO TRAILING SL EXIT — " + coin + "\\n" + pos["contract"] + f"\\nUnderlying: {market:.4f} USD | Trailing SL: {stop:.4f} USD\\nOption bid: {bid:.6f} USD\\nMarket-structure exit alert. Signal only; no order placed.")
        elif abs(stop - old_stop) > 1e-12:
            telegram("🔒 ZERO HERO TRAILING SL UPDATED — " + coin + "\\n" + pos["contract"] + f"\\nNew underlying SL: {stop:.4f} USD | Favorable extreme: {best:.4f} | ATR: {atr:.4f}\\nOption bid: {bid:.6f} USD\\nSL only tightens; never widens. Signal only.")
        changed = True
    return changed

def main():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            raw_state = f.read()
        try:
            state = json.loads(raw_state)
        except json.JSONDecodeError as parse_error:
            # Recover only the known legacy corruption: literal backslash-n
            # text appended after an otherwise complete JSON document.
            cleaned = raw_state.rstrip()
            while cleaned.endswith("\\n"):
                cleaned = cleaned[:-2].rstrip()
            state = json.loads(cleaned)
            print("Recovered legacy state file trailing literal \\n")
        if not isinstance(state, dict):
            raise RuntimeError("Zero Hero state format गलत है")
    except FileNotFoundError:
        state = {}
    except (OSError, ValueError) as exc:
        print("State read failed; refusing to overwrite: " + str(exc), file=sys.stderr)
        raise SystemExit(1)

    sent = errors = 0
    for coin in COINS:
        # Reset per-coin values so a prior coin's signal cannot leak into this coin.
        sig = None
        underlying = 0.0
        try:
            try:
                early = one_minute_early_signal(coin)
            except Exception as early_error:
                early = None
                print(coin + ": 1m early detector unavailable; continuing to 5m entry check: " + str(early_error)[:120])
            sig = five_minute_signal(coin)
            if early:
                print(f"{coin}: 1m early movement {early['direction']} detected; waiting for 5m entry + 15m confirmation")
            payload = get_json(OPTION_API.format(coin=coin), timeout=25)
            underlying = float(payload.get("underlying") or 0)
            if underlying <= 0:
                raise RuntimeError("Options underlying unavailable")
            monitor_positions(coin, payload, underlying, sig, state)
            if not sig:
                print(coin + ": no 5m entry trigger; 1m is early warning only; active positions monitored")
                continue
            confirm = confirm_15m(coin, sig["direction"])
            if not confirm:
                print(coin + ": 5m trigger found but 15m confirmation failed; no entry")
                continue
            option = best_zero_hero_option(coin, sig["direction"], underlying, payload)
            key = coin + "|" + option["name"] + "|" + sig["direction"]
            stamp = str(sig["candle_time"])
            if state.get(key) == stamp:
                print(coin + ": duplicate 5m signal suppressed")
                continue
            entry = float(option["entry"])
            delta, gamma = float(option.get("delta") or 0), float(option.get("gamma") or 0)
            is_call = "CALL" in sig["direction"]
            stop_underlying = min(sig["swing_low"], sig["price"] - 1.2 * sig["atr"]) if is_call else max(sig["swing_high"], sig["price"] + 1.2 * sig["atr"])
            def projected_premium(underlying_move):
                return max(0.01, entry + delta * underlying_move + 0.5 * gamma * underlying_move * underlying_move)
            stop_move = stop_underlying - sig["price"]
            if delta != 0:
                sl = projected_premium(stop_move)
                sl_line = f"🛑 Dynamic option SL: {sl:.6f} USD — 5m swing + ATR + option Greeks\\n"
            else:
                sl = None
                sl_line = f"🛑 Underlying invalidation SL: {stop_underlying:.4f} USD — option premium SL not estimated because Greeks are unavailable\\n"
            sign = 1 if is_call else -1
            target_underlyings = [sig["price"] + sign * sig["atr"] * multiple for multiple in (1.5, 3.0, 5.0)]
            if delta != 0:
                target_values = [projected_premium(x - sig["price"]) for x in target_underlyings]
                tp_pcts = [(x / entry - 1) * 100 for x in target_values]
                target_text = (f"🎯 TP1: {target_values[0]:.6f} USD ({tp_pcts[0]:+.1f}%)\\n"
                               + f"🎯 TP2: {target_values[1]:.6f} USD ({tp_pcts[1]:+.1f}%)\\n"
                               + f"🚀 TP3: {target_values[2]:.6f} USD ({tp_pcts[2]:+.1f}%) — no fixed % cap\\n")
                target_method = "Option targets estimated with available delta/gamma"
            else:
                target_text = ("Underlying ATR targets (Greeks unavailable): "
                               + " / ".join(f"{x:.2f}" for x in target_underlyings) + " USD\\n")
                target_method = "Option premium target not estimated without Greeks"
            side = "BUY CALL" if is_call else "BUY PUT"
            msg = (
                "⚡ ZERO HERO JACKPOT — " + coin + " (1m EARLY → 5m ENTRY → 15m CONFIRM)\\n"
                + side + "\\nContract: " + option["name"] + "\\n"
                + f"Underlying: {underlying:,.2f} USD | Suggested LIMIT BUY: {entry:.6f} USD (≈ ₹{option['premium_inr']:.2f})\\n"
                + f"Bid: {option['bid']:.6f} | Ask: {option['ask']:.6f} | Spread: {option['spread']*100:.2f}%\\n"
                + sl_line + target_text
                + f"5m RSI14: {sig['rsi']:.1f} | EMA9: {sig['ema9']:.2f} | EMA21: {sig['ema21']:.2f}\\n"
                + f"15m confirmation: RSI {confirm['rsi']:.1f} | Close {confirm['close']:.4f} vs EMA9 {confirm['ema9']:.4f} | Supertrend {confirm['supertrend']}\\n"
                + f"5m move: {sig['move_pct']:.3f}% | ATR: {sig['atr']:.4f} | ADX: {sig['adx']:.1f} | Supertrend: {sig['supertrend']}\\n"
                + f"MACD: {sig['macd']:.5f} | Signal: {sig['macd_signal']:.5f} | Histogram: {sig['macd_hist']:.5f} | Volume: {sig['volume_ratio']:.2f}x\\n"
                + f"Market data: {sig['source']} | Option source: {payload.get('source', 'unknown')}\\n"
                + target_method + f". Targets and trailing SL follow underlying movement; SL only tightens. {coin} premium ₹0–₹{MAX_PREMIUM_INR_BY_COIN.get(coin, 30.0):.0f} filter and <=10% spread retained. Signal only — no automatic order."
            )
            telegram(msg)
            positions = state.setdefault("__positions__", [])
            position_id = key + "|" + stamp
            if not any(p.get("key") == position_id for p in positions):
                positions.append({
                    "key": position_id, "coin": coin, "contract": option["name"],
                    "direction": "CALL" if is_call else "PUT",
                    "entry_underlying": sig["price"], "best_underlying": sig["price"],
                    "atr": sig["atr"], "trail_underlying": stop_underlying,
                    "targets": [sig["price"] + sign * sig["atr"] * m for m in (1.5, 3.0, 5.0)],
                    "target_stage": 0, "entry_premium": entry, "status": "ACTIVE"
                })
            state[key] = stamp
            sent += 1
            print(coin + ": Zero Hero alert sent " + option["name"])
        except Exception as exc:
            reason = str(exc)
            # Missing eligible contracts are safe skips; never weaken option filters.
            if sig and underlying > 0 and ("contract" in reason.lower() or "eligible" in reason.lower()):
                movement_key = "MOVEMENT|" + coin + "|" + sig["direction"]
                movement_stamp = str(sig["candle_time"])
                try:
                    if state.get(movement_key) != movement_stamp:
                        is_call = "CALL" in sig["direction"]
                        sign = 1 if is_call else -1
                        stop = min(sig["swing_low"], sig["price"] - 1.2 * sig["atr"]) if is_call else max(sig["swing_high"], sig["price"] + 1.2 * sig["atr"])
                        tps = [sig["price"] + sign * sig["atr"] * m for m in (1.5, 3.0, 5.0)]
                        movement_msg = (
                            "⚡ ZERO HERO MARKET MOVEMENT — " + coin + "\\n"
                            + sig["direction"] + " (1m early movement)\\n"
                            + f"Underlying reference: {underlying:.4f} USD\\n"
                            + f"Underlying SL / invalidation: {stop:.4f} USD\\n"
                            + "Underlying targets: " + " / ".join(f"{x:.4f}" for x in tps) + " USD\\n"
                            + f"1m move: {sig['move_pct']:.3f}% | RSI14: {sig['rsi']:.1f} | ATR: {sig['atr']:.4f}\\n"
                            + f"⚠️ NO OPTION ENTRY: कोई contract ₹0–₹{MAX_PREMIUM_INR_BY_COIN.get(coin, 30.0):.0f} premium, <=10% spread और liquidity filter पास नहीं कर पाया। यह underlying movement alert है, option buy recommendation नहीं।"
                        )
                        telegram(movement_msg)
                        state[movement_key] = movement_stamp
                        print(coin + ": movement alert sent; strict option filters blocked entry")
                    else:
                        print(coin + ": repeated movement alert suppressed; eligible option unavailable")
                except Exception as send_exc:
                    errors += 1
                    print(coin + ": movement Telegram delivery failed: " + str(send_exc), file=sys.stderr)
                continue
            errors += 1
            print(coin + ": ZERO HERO ERROR: " + reason, file=sys.stderr)

    os.makedirs("state", exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, STATE_PATH)
    print(f"Zero Hero completed. Alerts sent: {sent}; coin errors: {errors}")
    # Per-coin data and Telegram errors remain visible in the Actions log.


if __name__ == "__main__":
    main()

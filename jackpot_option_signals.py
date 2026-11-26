#!/usr/bin/env python3
"""Independent, strict BTC/ETH 15m jackpot option alerts. Alert-only; never places orders."""
import json
import os
import sys
from datetime import datetime, timezone

from options_strong_signals import (
    OPTION_API, best_option, candles, ema, get_json, rsi, telegram
)

JACKPOT_COINS = ("BTC", "ETH")
STATE_PATH = "state/jackpot_option_signals.json"
SL_MULTIPLIER = 0.75
TP1_MULTIPLIER = 2.0
TP2_MULTIPLIER = 5.0
TP3_MULTIPLIER = 10.0


def save_jackpot_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def strict_jackpot_signal(coin):
    """Catch the first confirmed 15m ignition candle, with a stricter fallback."""
    rows, source = candles(coin)
    latest_open = int(float(rows[-1][0]))
    age = int(datetime.now(timezone.utc).timestamp() * 1000) - latest_open
    if age < -60000 or age > 30 * 60 * 1000:
        raise RuntimeError("15m candle data पुराना है; Jackpot signal सुरक्षित रूप से रोका गया")

    closed = rows[:-1]
    if len(closed) < 30:
        raise RuntimeError("Jackpot confirmation के लिए पर्याप्त closed 15m candles नहीं हैं")

    last = closed[-1]
    previous = closed[-2]
    previous2 = closed[-3]
    op, high, low, close, volume = (float(last[i]) for i in (1, 2, 3, 4, 5))
    candle_range = high - low
    if candle_range <= 0 or volume <= 0:
        return None

    closes = [float(x[4]) for x in closed]
    highs = [float(x[2]) for x in closed]
    lows = [float(x[3]) for x in closed]
    volumes = [float(x[5]) for x in closed]
    e9 = ema(closes[-30:], 9)
    e21 = ema(closes[-50:], 21)
    rv = rsi(closes, 14)
    spread_pct = abs(e9 - e21) / close if close > 0 else 0.0
    avg_volume = sum(volumes[-21:-1]) / 20.0
    volume_ratio = volume / avg_volume if avg_volume > 0 else 0.0
    body_ratio = abs(close - op) / candle_range
    close_location = (close - low) / candle_range
    prev_close = float(previous[4])
    prev2_close = float(previous2[4])

    # EARLY IGNITION: first completed candle breaking a short 3-candle base.
    # EMA9 may be just crossing EMA21; this avoids waiting for a multi-candle run.
    early_high = max(highs[-4:-1])
    early_low = min(lows[-4:-1])
    early_bull = (
        close > early_high
        and close > op
        and close > e9
        and e9 >= e21 * 0.999
        and 52 <= rv <= 70
        and body_ratio >= 0.55
        and close_location >= 0.78
        and close > prev_close
        and volume_ratio >= 1.30
    )
    early_bear = (
        close < early_low
        and close < op
        and close < e9
        and e9 <= e21 * 1.001
        and 30 <= rv <= 48
        and body_ratio >= 0.55
        and close_location <= 0.22
        and close < prev_close
        and volume_ratio >= 1.30
    )

    # CONFIRMED BREAKOUT: stronger follow-through if the first ignition filter
    # did not trigger. Existing normal signal logic is completely separate.
    prior_high = max(highs[-6:-1])
    prior_low = min(lows[-6:-1])
    confirmed_bull = (
        close > e9 > e21
        and 58 <= rv <= 72
        and spread_pct >= 0.002
        and close > op
        and body_ratio >= 0.60
        and close_location >= 0.80
        and close > prior_high
        and close > prev_close > prev2_close
        and volume_ratio >= 1.50
    )
    confirmed_bear = (
        close < e9 < e21
        and 28 <= rv <= 42
        and spread_pct >= 0.002
        and close < op
        and body_ratio >= 0.60
        and close_location <= 0.20
        and close < prior_low
        and close < prev_close < prev2_close
        and volume_ratio >= 1.50
    )

    if early_bull:
        direction, phase = "STRONG BUY CALL", "EARLY IGNITION"
    elif early_bear:
        direction, phase = "STRONG BUY PUT", "EARLY IGNITION"
    elif confirmed_bull:
        direction, phase = "STRONG BUY CALL", "CONFIRMED BREAKOUT"
    elif confirmed_bear:
        direction, phase = "STRONG BUY PUT", "CONFIRMED BREAKOUT"
    else:
        return None

    return {
        "direction": direction,
        "phase": phase,
        "price": close,
        "rsi": rv,
        "ema9": e9,
        "ema21": e21,
        "candle_time": int(float(last[0])),
        "source": source,
        "volume_ratio": volume_ratio,
        "spread_pct": spread_pct * 100,
        "body_ratio": body_ratio,
    }

def main():
    state = {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            raise RuntimeError("Jackpot duplicate-state file format गलत है")
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        print("Jackpot state read failed; refusing to overwrite: " + str(exc), file=sys.stderr)
        raise SystemExit(1)

    sent = 0
    errors = 0
    for coin in JACKPOT_COINS:
        try:
            signal = strict_jackpot_signal(coin)
            if not signal:
                print(coin + ": strict Jackpot conditions not met")
                continue

            payload = get_json(OPTION_API.format(coin=coin), timeout=25)
            underlying = float(payload.get("underlying") or 0)
            if underlying <= 0:
                raise RuntimeError("Options gateway underlying price unavailable")

            option = best_option(coin, signal["direction"], underlying, payload)
            key = "JACKPOT|" + coin + "|" + option["name"] + "|" + signal["direction"]
            stamp = str(signal["candle_time"])
            if state.get(key) == stamp:
                print(coin + ": Jackpot duplicate already sent for this closed candle")
                continue

            # Jackpot targets are option-premium multiples, not guaranteed outcomes.
            option["sl"] = option["entry"] * SL_MULTIPLIER
            option["tp1"] = option["entry"] * TP1_MULTIPLIER
            option["tp2"] = option["entry"] * TP2_MULTIPLIER
            option["tp3"] = option["entry"] * TP3_MULTIPLIER
            quote_source = str(payload.get("source") or "unknown")
            side = "BUY CALL" if "CALL" in signal["direction"] else "BUY PUT"
            message = (
                "💎 JACKPOT " + coin + " — " + signal["phase"] + " (15m)\n"
                + side + "\n"
                + "Contract: " + option["name"] + "\n"
                + f"Strike: {option['strike']:,.2f} | Expiry: {option['expiry']}\n"
                + f"Underlying: {underlying:,.2f} USD\n"
                + f"Bid: {option['bid']:.4f} USD | Ask: {option['ask']:.4f} USD\n"
                + f"Suggested LIMIT BUY: {option['entry']:.4f} USD\n"
                + f"🛑 Initial SL (-25%): {option['sl']:.4f} USD\n"
                + f"🎯 TP1 (2x premium): {option['tp1']:.4f} USD\n"
                + f"🎯 TP2 (5x premium): {option['tp2']:.4f} USD\n"
                + f"🚀 JACKPOT TP3 (10x premium): {option['tp3']:.4f} USD\n"
                + f"Spread: {option['spread']*100:.2f}% (max 10%)\n"
                + f"RSI14: {signal['rsi']:.1f} | EMA9: {signal['ema9']:.2f} | EMA21: {signal['ema21']:.2f}\n"
                + f"Volume: {signal['volume_ratio']:.2f}x 20-candle average | EMA gap: {signal['spread_pct']:.2f}%\n"
                + f"15m closed-candle source: {signal['source']} | Option source: {quote_source}\n"
                + "10x target बहुत high-risk और non-guaranteed है; premium शून्य तक जा सकता है। "
                + "Signal only — कोई order अपने-आप place नहीं होगा।"
            )

            telegram(message)

            # Share the existing premium monitor ledger; unique JACKPOT key keeps
            # this record separate from normal signals without changing their rules.
            positions_path = "state/option_positions.json"
            try:
                with open(positions_path, "r", encoding="utf-8") as pf:
                    positions = json.load(pf)
                if not isinstance(positions, list):
                    raise RuntimeError("Option positions ledger format गलत है")
            except FileNotFoundError:
                positions = []
            except json.JSONDecodeError as exc:
                raise RuntimeError("Option positions ledger invalid JSON; not overwritten: " + str(exc))

            if not any(p.get("key") == key for p in positions):
                positions.append({
                    "key": key,
                    "signal_type": "JACKPOT",
                    "coin": coin,
                    "contract": option["name"],
                    "source": quote_source,
                    "entry": option["entry"],
                    "sl": option["sl"],
                    "tp1": option["tp1"],
                    "tp2": option["tp2"],
                    "tp3": option["tp3"],
                    "peak": option["entry"],
                    "stage": 0,
                    "status": "ACTIVE",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
                os.makedirs("state", exist_ok=True)
                with open(positions_path, "w", encoding="utf-8") as pf:
                    json.dump(positions, pf, ensure_ascii=False, indent=2)
                    pf.write("\n")

            state[key] = stamp
            save_jackpot_state(state)
            sent += 1
            print(coin + ": JACKPOT alert sent " + option["name"])
        except Exception as exc:
            errors += 1
            print(coin + ": JACKPOT ERROR: " + str(exc), file=sys.stderr)

    print(f"Jackpot completed. Alerts sent: {sent}; coin errors: {errors}")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

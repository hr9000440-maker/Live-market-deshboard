#!/usr/bin/env python3
"""Independent Gamma Blast early detector. Alert-only; never places orders.

Uses 1m underlying breakout + 5m directional bias, live option bid/ask,
OI/volume and premium expansion. Snapshots are persisted between scheduled runs
so OI/volume/premium changes can be measured instead of guessed.
"""
import json, os, time
from datetime import datetime, timezone
from options_strong_signals import OPTION_API, get_json, ema, rsi, telegram

COINS = ("BTC", "ETH", "BNB", "ZEC")
STATE = "state/gamma_blast_snapshots.json"
USD_INR = 95.0
MAX_SPREAD = 0.08
MIN_PREMIUM_MOVE = 0.04
MIN_VOLUME_RATIO = 1.20
MAX_OTM_PCT = 2.5
MIN_OTM_PCT = 0.2
SNAPSHOT_LIMIT = 12

def klines(coin, interval, limit=80):
    symbol = coin + "USDT"
    urls = [
        f"https://api.kucoin.com/api/v1/market/candles?symbol={coin}-USDT&type={interval}",
        f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}",
        f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}",
    ]
    errors = []
    for i, url in enumerate(urls):
        try:
            p = get_json(url, timeout=12)
            raw = p.get("data") if i == 0 else p
            if i == 0 and (p.get("code") != "200000" or not isinstance(raw, list)):
                raise RuntimeError("KuCoin candle response unavailable")
            rows = []
            for x in raw:
                if not isinstance(x, list) or len(x) < 6: continue
                if i == 0:
                    ts = int(float(x[0])) * 1000
                    op, close, high, low, vol = map(float, (x[1], x[2], x[3], x[4], x[5]))
                else:
                    ts = int(x[0])
                    op, high, low, close, vol = map(float, (x[1], x[2], x[3], x[4], x[5]))
                if min(op, close, high, low) <= 0 or vol < 0: continue
                duration = 60000 if interval == "1min" else 300000
                rows.append([ts, op, high, low, close, vol, ts + duration - 1])
            rows.sort(key=lambda x: x[0])
            if len(rows) >= 30 and int(time.time()*1000) - rows[-1][0] < (5 if interval == "1min" else 15)*60000:
                return rows, "KuCoin" if i == 0 else "Binance"
            errors.append("stale/insufficient candles")
        except Exception as e:
            errors.append(str(e)[:90])
    raise RuntimeError(interval + " candles unavailable: " + " | ".join(errors[-3:]))

def market_trigger(coin):
    m1, src1 = klines(coin, "1min")
    m5, src5 = klines(coin, "5min")
    # Ignore the currently forming candle; act on the latest completed candles.
    a, b = m1[:-1], m5[:-1]
    if len(a) < 25 or len(b) < 25: return None
    last = a[-1]
    op, high, low, close, vol = last[1:6]
    prior_high = max(x[2] for x in a[-6:-1])
    prior_low = min(x[3] for x in a[-6:-1])
    avg_vol = sum(x[5] for x in a[-21:-1]) / 20
    vol_ratio = vol / avg_vol if avg_vol else 0
    e9 = ema([x[4] for x in b[-30:]], 9)
    e21 = ema([x[4] for x in b[-50:]], 21)
    e9_prev = ema([x[4] for x in b[-31:-1]], 9)
    rv = rsi([x[4] for x in b], 14)
    bull = close > prior_high and close > op and close > e9 and e9 >= e9_prev and e9 >= e21 * 0.997 and vol_ratio >= MIN_VOLUME_RATIO
    bear = close < prior_low and close < op and close < e9 and e9 <= e9_prev and e9 <= e21 * 1.003 and vol_ratio >= MIN_VOLUME_RATIO
    if not bull and not bear: return None
    return {
        "side": "C" if bull else "P", "direction": "BUY CALL" if bull else "BUY PUT",
        "price": close, "rsi": rv, "ema9": e9, "ema21": e21,
        "volume_ratio": vol_ratio, "candle_time": int(last[0]),
        "sources": src1 + "/" + src5
    }

def pick_contract(payload, side, underlying):
    rows = payload.get("rows") or []
    expiries = sorted({str(x.get("expiry") or "") for x in rows if x.get("expiry")})
    if not expiries: raise RuntimeError("listed expiry missing")
    expiry = expiries[0]
    candidates = []
    for x in rows:
        name = str(x.get("instrument_name") or "")
        if str(x.get("expiry") or "") != expiry: continue
        typ = str(x.get("option_type") or "").upper()
        if typ not in (side, "CALL" if side == "C" else "PUT") and not name.endswith("-"+side): continue
        try:
            strike = float(x.get("strike") or 0)
            bid, ask = float(x.get("bid_price") or 0), float(x.get("ask_price") or 0)
            mark = float(x.get("mark_price") or 0)
            oi, volume = float(x.get("open_interest") or 0), float(x.get("volume") or 0)
        except (TypeError, ValueError): continue
        if not name or strike <= 0 or bid <= 0 or ask < bid or ask <= 0 or mark <= 0 or oi <= 0 or volume <= 0: continue
        otm = ((strike-underlying)/underlying*100) if side == "C" else ((underlying-strike)/underlying*100)
        if not MIN_OTM_PCT <= otm <= MAX_OTM_PCT: continue
        mid = (bid+ask)/2
        spread = (ask-bid)/mid if mid else 999
        if spread > MAX_SPREAD: continue
        is_usd = any(k in str(payload.get("source","")).lower() for k in ("paradex","bybit","binance"))
        factor = 1 if is_usd else underlying
        candidates.append({
            "name": name, "strike": strike, "expiry": expiry,
            "bid": bid*factor, "ask": ask*factor, "mark": mark*factor,
            "oi": oi, "volume": volume, "spread": spread, "otm": otm
        })
    if not candidates: raise RuntimeError("No liquid near-OTM contract passed bid/ask, OI, volume and spread filters")
    return min(candidates, key=lambda x: (abs(x["otm"]-1.0), x["spread"]))

def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            p=json.load(f)
        return p if isinstance(p,dict) else {"snapshots":{}, "alerts":{}}
    except FileNotFoundError: return {"snapshots":{}, "alerts":{}}
    except Exception as e: raise RuntimeError("Refusing to overwrite unreadable Gamma state: "+str(e))

def save_state(s):
    os.makedirs("state", exist_ok=True)
    tmp=STATE+".tmp"
    with open(tmp,"w",encoding="utf-8") as f: json.dump(s,f,indent=2,sort_keys=True)
    os.replace(tmp,STATE)

def main():
    state=load_state()
    state.setdefault("snapshots",{}); state.setdefault("alerts",{})
    now=int(time.time()*1000)
    sent=0
    for coin in COINS:
        try:
            trig=market_trigger(coin)
            payload=get_json(OPTION_API.format(coin=coin),timeout=20)
            underlying=float(payload.get("underlying") or 0)
            if underlying<=0: raise RuntimeError("underlying quote missing")
            side=trig["side"] if trig else None
            # Snapshot the most liquid call and put every run, even without a trigger.
            for s in ("C","P"):
                opt=pick_contract(payload,s,underlying)
                key=coin+"|"+opt["name"]
                hist=state["snapshots"].setdefault(key,[])
                prev=hist[-1] if hist else None
                snap={"ts":now,"mark":opt["mark"],"bid":opt["bid"],"ask":opt["ask"],
                      "oi":opt["oi"],"volume":opt["volume"],"spread":opt["spread"]}
                hist.append(snap); state["snapshots"][key]=hist[-SNAPSHOT_LIMIT:]
                if not trig or s != side or not prev: continue
                age=(now-int(prev.get("ts",0)))/60000
                if age<=0 or age>20: continue
                prem_change=opt["mark"]/float(prev.get("mark") or opt["mark"])-1
                vol_delta=max(0,opt["volume"]-float(prev.get("volume") or 0))
                oi_delta=(opt["oi"]/float(prev.get("oi") or opt["oi"])-1) if float(prev.get("oi") or 0)>0 else 0
                if prem_change < MIN_PREMIUM_MOVE: continue
                if opt["spread"] > MAX_SPREAD or opt["oi"]<=0 or opt["volume"]<=0: continue
                # Rising traded volume confirms participation; OI is reported and monitored,
                # but not forced to rise because exchange OI snapshots may lag.
                if vol_delta <= 0: continue
                alert_key=key+"|"+trig["direction"]
                candle_stamp=str(trig["candle_time"])
                if state["alerts"].get(alert_key)==candle_stamp: continue
                entry=opt["ask"]
                message=(
                    "⚡ GAMMA BLAST — EARLY MOVEMENT ALERT\n"
                    f"Coin: {coin} | {trig['direction']}\nContract: {opt['name']}\n"
                    f"Underlying: {underlying:.2f} USD | Entry reference (ASK): {entry:.4f} USD\n"
                    f"Bid: {opt['bid']:.4f} | Ask: {opt['ask']:.4f} | Spread: {opt['spread']*100:.2f}%\n"
                    f"Premium momentum: +{prem_change*100:.1f}% in {age:.1f} min | OTM: {opt['otm']:.2f}%\n"
                    f"OI: {opt['oi']:.0f} | OI change since snapshot: {oi_delta*100:+.2f}%\n"
                    f"24h option volume: {opt['volume']:.4g} | volume change: +{vol_delta:.4g}\n"
                    f"1m breakout volume: {trig['volume_ratio']:.2f}x | 5m RSI: {trig['rsi']:.1f}\n"
                    f"🛑 Indicative SL (-25%): {entry*.75:.4f}\n"
                    f"🎯 TP1 (+25%): {entry*1.25:.4f} | TP2 (+50%): {entry*1.50:.4f} | TP3 (+75%): {entry*1.75:.4f}\n"
                    f"Sources: {trig['sources']} + {payload.get('source','unknown')}\n"
                    "Early-momentum alert only; quotes/OI may lag, targets are not guaranteed. No order is placed."
                )
                telegram(message)
                state["alerts"][alert_key]=candle_stamp
                sent+=1
            print(coin+": snapshot saved"+(" / trigger "+trig["direction"] if trig else " / no 1m breakout"))
        except Exception as e:
            print(coin+": GAMMA BLAST ERROR: "+str(e)[:220])
    save_state(state)
    print("Gamma Blast run complete; alerts sent:",sent)

if __name__=="__main__": main()

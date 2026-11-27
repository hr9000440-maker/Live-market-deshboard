#!/usr/bin/env python3
"""Independent Gamma Blast early detector. Alert-only; never places orders.
Uses the existing public options gateway and Binance 1m spot candles.
A first run only seeds a baseline; later runs compare the same option contract.
"""
import json, os, sys, time
from datetime import datetime, timezone
from options_strong_signals import OPTION_API, get_json, telegram

COINS = ("BTC", "ETH", "BNB", "ZEC")
STATE_PATH = "state/gamma_blast_state.json"
SL_PCT = 0.25
MAX_SPREAD_PCT = 10.0
MIN_PREMIUM_JUMP_PCT = 8.0
MIN_UNDERLYING_MOVE_PCT = 0.18
MIN_VOLUME_RATIO = 1.20

def num(row, *keys):
    for key in keys:
        value = row.get(key)
        try:
            if value not in (None, ""):
                return float(value)
        except (TypeError, ValueError):
            pass
    return 0.0

def contract_type(row, name):
    typ = str(row.get("option_type") or row.get("type") or "").upper()
    if typ in ("C", "CALL"): return "CALL"
    if typ in ("P", "PUT"): return "PUT"
    upper = name.upper()
    if upper.endswith("-C") or "CALL" in upper: return "CALL"
    if upper.endswith("-P") or "PUT" in upper: return "PUT"
    return ""

def underlying_move(coin):
    # Try multiple public spot feeds: Binance may return HTTP 451 in some runner regions.
    sources = [
        ("KuCoin", "https://api.kucoin.com/api/v1/market/candles?symbol="+coin+"-USDT&type=1min"),
        ("Binance", "https://api.binance.com/api/v3/klines?symbol="+coin+"USDT&interval=1m&limit=8"),
        ("Binance data-api", "https://data-api.binance.vision/api/v3/klines?symbol="+coin+"USDT&interval=1m&limit=8"),
        ("Gate.io", "https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair="+coin+"_USDT&interval=1m&limit=8"),
    ]
    errors = []
    for source, url in sources:
        try:
            payload = get_json(url, timeout=15)
            if source == "KuCoin":
                if payload.get("code") != "200000" or not isinstance(payload.get("data"), list):
                    raise RuntimeError("KuCoin candles unavailable")
                rows = sorted(payload["data"], key=lambda x: int(float(x[0])))
                closes = [float(x[2]) for x in rows if len(x) >= 3]
            elif source == "Gate.io":
                rows = sorted(payload, key=lambda x: int(float(x[0])))
                closes = [float(x[2]) for x in rows if len(x) >= 3]
            else:
                if not isinstance(payload, list):
                    raise RuntimeError("candle response is not a list")
                closes = [float(x[4]) for x in payload if isinstance(x, list) and len(x) >= 5]
            if len(closes) < 6 or closes[-2] <= 0 or closes[-6] <= 0:
                raise RuntimeError("not enough valid 1m candles")
            # Compare latest completed 1m close with close five minutes earlier.
            latest, base = closes[-2], closes[-6]
            print(coin + ": 1m data source=" + source)
            return latest, (latest/base-1.0)*100.0
        except Exception as exc:
            errors.append(source + ": " + str(exc)[:100])
    raise RuntimeError("1m underlying candles unavailable: " + " | ".join(errors))

def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            state=json.load(f)
        if not isinstance(state, dict): raise ValueError("state is not object")
        return state
    except FileNotFoundError:
        return {}
    except Exception as exc:
        raise RuntimeError("Gamma state read failed; refusing overwrite: "+str(exc))

def save_state(state):
    os.makedirs("state", exist_ok=True)
    tmp=STATE_PATH+".tmp"
    with open(tmp,"w",encoding="utf-8") as f: json.dump(state,f,ensure_ascii=False,indent=2)
    os.replace(tmp,STATE_PATH)

def main():
    state=load_state()
    now=int(time.time())
    updates={}
    alerts=0
    errors=0
    for coin in COINS:
        try:
            payload=get_json(OPTION_API.format(coin=coin),timeout=25)
            rows=payload.get("rows")
            if not isinstance(rows,list) or not rows: raise RuntimeError("option gateway rows missing")
            spot, spot_move=underlying_move(coin)
            candidates=[]
            for row in rows:
                name=str(row.get("instrument_name") or row.get("symbol") or "")
                side=contract_type(row,name)
                bid=num(row,"bid_price","best_bid","bid")
                ask=num(row,"ask_price","best_ask","ask")
                mark=num(row,"mark_price","mark","last_price","last")
                if not name or not side or bid<=0 or ask<bid or mark<=0: continue
                mid=(bid+ask)/2
                spread=(ask-bid)/mid*100 if mid else 999
                if spread>MAX_SPREAD_PCT: continue
                expiry=str(row.get("expiry") or (name.split("-")[1] if len(name.split("-"))>2 else ""))
                strike=num(row,"strike")
                if not strike:
                    try: strike=float(name.split("-")[-2])
                    except Exception: continue
                # Avoid illiquid stale contracts and prefer near-ATM options.
                oi=num(row,"open_interest","oi")
                vol=num(row,"volume","volume_24h","volume24h")
                if oi<=0 and vol<=0: continue
                greeks=row.get("greeks") if isinstance(row.get("greeks"),dict) else {}
                delta=num(row,"delta") or num(greeks,"delta")
                gamma=num(row,"gamma") or num(greeks,"gamma")
                iv=num(row,"mark_iv","markIV","implied_volatility","iv") or num(greeks,"iv")
                key=coin+"|"+name
                prev=state.get(key,{})
                old_mark=num(prev,"mark")
                premium_change=(mark/old_mark-1)*100 if old_mark>0 else None
                old_oi=num(prev,"oi")
                old_vol=num(prev,"vol")
                oi_change=((oi-old_oi)/old_oi*100) if old_oi>0 and oi>0 else None
                vol_ratio=(vol/old_vol) if old_vol>0 and vol>0 else None
                candidates.append((abs(strike-spot), {
                    "key":key,"name":name,"side":side,"strike":strike,"expiry":expiry,
                    "bid":bid,"ask":ask,"mark":mark,"spread":spread,"oi":oi,"vol":vol,
                    "delta":delta,"gamma":gamma,"iv":iv,"premium_change":premium_change,
                    "oi_change":oi_change,"vol_ratio":vol_ratio,"old_mark":old_mark
                }))
            if not candidates: raise RuntimeError("no liquid CALL/PUT option quotes passed spread/OI/volume checks")
            # Evaluate the nearest-ATM CALL and PUT separately.
            chosen=[]
            for side in ("CALL","PUT"):
                same=[x[1] for x in candidates if x[1]["side"]==side]
                if same: chosen.append(min(same,key=lambda x:abs(x["strike"]-spot)))
            if not chosen: raise RuntimeError("near-ATM CALL/PUT quotes unavailable")
            for q in chosen:
                updates[q["key"]]={"mark":q["mark"],"bid":q["bid"],"ask":q["ask"],"oi":q["oi"],"vol":q["vol"],"time":now}
                change=q["premium_change"]
                # Directional early expansion: option premium jump plus spot momentum.
                direction_ok=(spot_move>MIN_UNDERLYING_MOVE_PCT if q["side"]=="CALL" else spot_move < -MIN_UNDERLYING_MOVE_PCT)
                premium_ok=(change is not None and change>=MIN_PREMIUM_JUMP_PCT)
                volume_ok=(q["vol_ratio"] is not None and q["vol_ratio"]>=MIN_VOLUME_RATIO)
                oi_ok=(q["oi_change"] is not None and q["oi_change"]>0)
                # At least premium expansion + spot direction, with volume/OI as extra confirmation.
                if premium_ok and direction_ok and (volume_ok or oi_ok):
                    stamp=str(int(now/300))
                    dedupe="sent|"+q["key"]
                    if state.get(dedupe)==stamp: continue
                    entry=q["ask"]
                    sl=entry*(1-SL_PCT)
                    # Retain existing main scanner targets: +25%, +50%, +75% premium.
                    tp1, tp2, tp3=entry*1.25,entry*1.50,entry*1.75
                    msg=(
                        "⚡ GAMMA BLAST EARLY ALERT — "+coin+"\n"
                        +("STRONG BUY CALL" if q["side"]=="CALL" else "STRONG BUY PUT")+"\n"
                        +"Contract: "+q["name"]+"\n"
                        +f"Underlying: {spot:.2f} USD | 5m move: {spot_move:+.2f}%\n"
                        +f"Premium: {q['mark']:.4f} | Previous: {q['old_mark']:.4f} | Change: {change:+.2f}%\n"
                        +f"Bid: {q['bid']:.4f} | Ask/Entry: {entry:.4f} | Spread: {q['spread']:.2f}%\n"
                        +f"OI: {q['oi']:.2f} | OI change: {q['oi_change'] if q['oi_change'] is not None else 'N/A'}\n"
                        +f"Volume: {q['vol']:.2f} | Volume ratio: {q['vol_ratio'] if q['vol_ratio'] is not None else 'N/A'}\n"
                        +f"Delta: {q['delta'] if q['delta'] else 'N/A'} | Gamma: {q['gamma'] if q['gamma'] else 'N/A'} | IV: {q['iv'] if q['iv'] else 'N/A'}\n"
                        +f"SL (-25%): {sl:.4f} | TP1: {tp1:.4f} | TP2: {tp2:.4f} | TP3: {tp3:.4f}\n"
                        +"Quote source: "+str(payload.get("source") or "existing public options gateway")+"\n"
                        +"Independent early detector; alert only, no automatic order. Premium feed is not verified as CoinSwitch."
                    )
                    telegram(msg)
                    state[dedupe]=stamp
                    alerts+=1
            print(coin+": checked "+str(len(chosen))+" near-ATM contracts")
        except Exception as exc:
            reason = str(exc)
            # A coin with no liquid/live option quote is a normal no-signal
            # outcome, not a broken workflow. Keep strict liquidity filters.
            if "no liquid CALL/PUT option quotes passed spread/OI/volume checks" in reason:
                print(coin + ": no eligible liquid option quote; safely skipped")
                continue
            errors+=1
            print(coin+": ERROR: "+reason,file=sys.stderr)
    state.update(updates)
    save_state(state)
    print("Gamma Blast complete; alerts="+str(alerts)+" errors="+str(errors))
    if errors: raise SystemExit(1)

if __name__=="__main__": main()

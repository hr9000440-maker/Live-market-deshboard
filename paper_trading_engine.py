#!/usr/bin/env python3
"""No-money paper trading engine. Bybit public linear candles with Binance fallback."""
import json, os, time, math, urllib.request, datetime
STATE_PATH="paper_trades.json"
COINS=["BTC","ETH","BNB","ZEC"]
BUDGET_INR=500
USD_INR=95
MAX_OPEN_PER_COIN=1

def get_json(url):
    req=urllib.request.Request(url,headers={"User-Agent":"LiveMarketPaperTrader/1.0"})
    with urllib.request.urlopen(req,timeout=15) as r: return json.loads(r.read())

def candles(symbol, interval, limit=80):
    # Futures-only paper data: Bybit linear primary, Binance USD-M fallback.
    # Spot feeds are deliberately excluded so spot candles cannot masquerade as futures.
    interval_ms = {"5m": 300000, "15m": 900000}.get(interval)
    bybit_interval = {"5m": "5", "15m": "15"}.get(interval)
    if not interval_ms or not bybit_interval:
        raise RuntimeError("Unsupported candle interval: " + str(interval))
    now_ms = int(time.time() * 1000)
    errors = []

    sources = (
        ("Bybit linear", "bybit"),
        ("Binance USD-M", "binance"),
    )
    for source_name, source in sources:
        try:
            if source == "bybit":
                payload = get_json(
                    f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}USDT&interval={bybit_interval}&limit={limit}"
                )
                if not isinstance(payload, dict) or payload.get("retCode") != 0:
                    raise RuntimeError("Bybit linear response: " + str(payload.get("retMsg", "invalid response")))
                raw = payload.get("result", {}).get("list", [])
                if not isinstance(raw, list):
                    raise RuntimeError("Bybit linear candles missing")
                raw = list(reversed(raw))
            else:
                raw = get_json(
                    f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}USDT&interval={interval}&limit={limit}"
                )
                if not isinstance(raw, list):
                    raise RuntimeError("Binance USD-M candles missing")

            rows = []
            seen = set()
            for item in raw:
                if not isinstance(item, (list, tuple)) or len(item) < 6:
                    raise RuntimeError(source_name + ": malformed candle row")
                ts = int(float(item[0]))
                op, high, low, close, volume = (float(item[i]) for i in range(1, 6))
                values = (op, high, low, close, volume)
                if not all(math.isfinite(v) for v in values):
                    raise RuntimeError(source_name + ": non-finite candle value")
                if min(op, high, low, close) <= 0 or volume < 0:
                    raise RuntimeError(source_name + ": zero/negative OHLC or volume")
                if high < max(op, close, low) or low > min(op, close, high):
                    raise RuntimeError(source_name + ": invalid OHLC range")
                if ts in seen:
                    raise RuntimeError(source_name + ": duplicate candle timestamp")
                seen.add(ts)
                # Keep completed candles only; do not blindly drop the newest row.
                if ts + interval_ms <= now_ms:
                    rows.append([ts, op, high, low, close, volume])

            rows.sort(key=lambda row: row[0])
            if len(rows) < 35:
                raise RuntimeError(source_name + ": insufficient completed candles")
            age_ms = now_ms - (rows[-1][0] + interval_ms)
            if age_ms < -1000 or age_ms > interval_ms * 2:
                raise RuntimeError(source_name + ": latest completed candle is stale")
            candles.last_source = source_name
            candles.last_candle_time = rows[-1][0]
            candles.last_candle_age_ms = max(0, age_ms)
            return rows
        except Exception as e:
            errors.append(source_name + ": " + str(e)[:140])

    raise RuntimeError("Bybit linear and Binance USD-M unavailable: " + " | ".join(errors)[:360])

def ema(v,n):
    a=v[0]; k=2/(n+1)
    for x in v[1:]: a=x*k+a*(1-k)
    return a

def rsi(v,n=14):
    changes=[v[i]-v[i-1] for i in range(len(v)-n,len(v))]
    up=sum(max(x,0) for x in changes)/n
    dn=sum(max(-x,0) for x in changes)/n
    return 100 if dn==0 else 100-100/(1+up/dn)

def analyze(coin,tf):
    rows=candles(coin,tf); c=[float(x[4]) for x in rows]
    o=[float(x[1]) for x in rows]; h=[float(x[2]) for x in rows]; l=[float(x[3]) for x in rows]
    e9=ema(c[-30:],9); e21=ema(c[-50:],21); rv=rsi(c)
    tr=[max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1])) for i in range(len(rows)-14,len(rows))]
    atr=sum(tr)/len(tr); side="WAIT"
    if c[-1]>e9>e21 and 45<=rv<=75 and c[-1]>o[-1]: side="BUY"
    elif c[-1]<e9<e21 and 25<=rv<=55 and c[-1]<o[-1]: side="SELL"
    return {"side":side,"entry":c[-1],"sl":c[-1]-(1.2*atr) if side=="BUY" else c[-1]+(1.2*atr) if side=="SELL" else None,
            "tp1":c[-1]+atr if side=="BUY" else c[-1]-atr if side=="SELL" else None,
            "tp2":c[-1]+2*atr if side=="BUY" else c[-1]-2*atr if side=="SELL" else None,
            "tp3":c[-1]+3*atr if side=="BUY" else c[-1]-3*atr if side=="SELL" else None,
            "rsi":rv,"ema9":e9,"ema21":e21,"candle":int(rows[-1][0]),"atr":atr}

def load():
    default = {"mode": "PAPER ONLY", "budget_inr": BUDGET_INR, "open_trades": [],
               "closed_trades": [], "last_update": None, "errors": {}}
    if not os.path.exists(STATE_PATH):
        return default
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    except Exception as e:
        # Never silently replace a damaged ledger with an empty one.
        raise RuntimeError(STATE_PATH + " is unreadable; refusing to reset paper history") from e
    if not isinstance(state, dict):
        raise RuntimeError(STATE_PATH + " must contain a JSON object; refusing to reset paper history")
    for key in ("open_trades", "closed_trades"):
        if key in state and not isinstance(state[key], list):
            raise RuntimeError(STATE_PATH + " has invalid " + key + "; refusing to reset paper history")
    if "errors" in state and not isinstance(state["errors"], dict):
        raise RuntimeError(STATE_PATH + " has invalid errors map; refusing to reset paper history")
    for key, value in default.items():
        state.setdefault(key, value)
    return state

def save(s):
    s["last_update"]=datetime.datetime.now(datetime.timezone.utc).isoformat()
    with open(STATE_PATH,"w") as f: json.dump(s,f,indent=2)

def close_trade(state,t,price,reason,now):
    qty=float(t["qty"])
    pnl=(price-float(t["entry"]))*qty*(1 if t["side"]=="BUY" else -1)*USD_INR
    t.update({"status":"CLOSED","exit":price,"exit_reason":reason,"closed_at":now,"pnl_inr":round(pnl,2)})
    state["closed_trades"].insert(0,t)
    state["closed_trades"]=state["closed_trades"][:300]
    state["open_trades"].remove(t)

def main():
    state=load(); original=json.dumps(state,sort_keys=True); now=datetime.datetime.now(datetime.timezone.utc).isoformat()
    data_ok=0
    for coin in COINS:
      try:
        # Check exits using latest completed 5m candle.
        live=candles(coin,"5m",40)[-1]
        lo,hi,cl=float(live[3]),float(live[2]),float(live[4])
        for t in list(state["open_trades"]):
          if t["coin"]!=coin: continue
          if t["side"]=="BUY":
            if lo<=t["sl"]: close_trade(state,t,t["sl"],"STOP LOSS",now); continue
            if not t.get("tp1_hit") and hi>=t["tp1"]:
              t["tp1_hit"]=True; t["tp1_hit_at"]=now
            if hi>=t.get("tp3",t["tp2"]): close_trade(state,t,t.get("tp3",t["tp2"]),"TARGET 3",now); continue
          else:
            if hi>=t["sl"]: close_trade(state,t,t["sl"],"STOP LOSS",now); continue
            if not t.get("tp1_hit") and lo<=t["tp1"]:
              t["tp1_hit"]=True; t["tp1_hit_at"]=now
            if lo<=t.get("tp3",t["tp2"]): close_trade(state,t,t.get("tp3",t["tp2"]),"TARGET 3",now); continue
        s15=analyze(coin,"15m")
        data_ok+=1
        # Open paper trades from a confirmed closed 15m signal; no 5m confirmation gate.
        if s15["side"] not in ("BUY","SELL"):
          state["errors"].pop(coin,None); continue
        if any(t["coin"]==coin for t in state["open_trades"]): continue
        stamp=f'{coin}:{s15["side"]}:{s15["candle"]}'
        if state.get("last_signal",{}).get(coin)==stamp: continue
        entry=s15["entry"]; qty=(BUDGET_INR/USD_INR)/entry
        trade={"id":stamp,"coin":coin,"side":s15["side"],"timeframe":"15m CONFIRMED","data_source":getattr(candles,"last_source","unknown"),"entry":entry,
          "sl":s15["sl"],"tp1":s15["tp1"],"tp2":s15["tp2"],"tp3":s15["tp3"],"qty":qty,"budget_inr":BUDGET_INR,
          "rsi":round(s15["rsi"],2),"ema9":s15["ema9"],"ema21":s15["ema21"],
          "opened_at":now,"status":"OPEN","tp1_hit":False,"tp2_hit":False,"tp3_hit":False,"signal_candle":s15["candle"]}
        state["open_trades"].insert(0,trade); state.setdefault("last_signal",{})[coin]=stamp
        state["errors"].pop(coin,None)
        print("PAPER OPEN",trade)
      except Exception as e:
        state.setdefault("errors",{})[coin]=str(e)[:240]
        print("DATA ERROR",coin,e)
    state["open_trades"]=state["open_trades"][:50]
    state["closed_trades"]=state.get("closed_trades",[])[:300]
    if json.dumps(state,sort_keys=True)!=original:
      save(state)
    print("PAPER SUMMARY",len(state["open_trades"]),"open",len(state["closed_trades"]),"closed;",data_ok,"/",len(COINS),"coins with usable 5m+15m data")
    if state.get("errors"):
      print("FATAL: market data unavailable for",len(state["errors"]),"coin(s); marking this Actions run as failed instead of reporting a false success.")
      raise SystemExit(1)

if __name__=="__main__": main()

#!/usr/bin/env python3
"""No-money paper trading engine. Public Binance USD-M futures candles only."""
import json, os, time, urllib.request, datetime
STATE_PATH="paper_trades.json"
COINS=["BTC","ETH","BNB","ZEC"]
BUDGET_INR=500
USD_INR=95
MAX_OPEN_PER_COIN=1

def get_json(url):
    req=urllib.request.Request(url,headers={"User-Agent":"LiveMarketPaperTrader/1.0"})
    with urllib.request.urlopen(req,timeout=15) as r: return json.loads(r.read())

def candles(symbol, interval, limit=80):
    # Primary: Bybit public linear candles. Fallback: Binance USD-M.
    errors=[]
    try:
        payload=get_json(f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}USDT&interval={interval}&limit={limit}")
        if payload.get("retCode") != 0:
            raise RuntimeError(f"Bybit {payload.get('retCode')}: {payload.get('retMsg')}")
        items=payload.get("result",{}).get("list",[])
        data=[[int(x[0]),x[1],x[2],x[3],x[4],x[5]] for x in reversed(items)]
        if len(data)<35: raise RuntimeError("Bybit: insufficient candle data")
        candles.last_source="Bybit linear"
        return data[:-1]
    except Exception as e:
        errors.append(str(e))
    try:
        data=get_json(f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}USDT&interval={interval}&limit={limit}")
        if not isinstance(data,list) or len(data)<35: raise RuntimeError("Binance: insufficient candle data")
        candles.last_source="Binance USD-M"
        return data[:-1]
    except Exception as e:
        errors.append(str(e))
    raise RuntimeError("All market-data sources failed: " + " | ".join(errors)[:180])

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
            "rsi":rv,"ema9":e9,"ema21":e21,"candle":int(rows[-1][0]),"atr":atr}

def load():
    try:
        with open(STATE_PATH) as f: return json.load(f)
    except Exception: return {"mode":"PAPER ONLY","budget_inr":BUDGET_INR,"open_trades":[],"closed_trades":[],"last_update":None,"errors":{}}

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
            if hi>=t["tp2"]: close_trade(state,t,t["tp2"],"TARGET 2",now); continue
          else:
            if hi>=t["sl"]: close_trade(state,t,t["sl"],"STOP LOSS",now); continue
            if not t.get("tp1_hit") and lo<=t["tp1"]:
              t["tp1_hit"]=True; t["tp1_hit_at"]=now
            if lo<=t["tp2"]: close_trade(state,t,t["tp2"],"TARGET 2",now); continue
        s5=analyze(coin,"5m"); s15=analyze(coin,"15m")
        if s5["side"] not in ("BUY","SELL") or s5["side"]!=s15["side"]:
          state["errors"].pop(coin,None); continue
        if any(t["coin"]==coin for t in state["open_trades"]): continue
        stamp=f'{coin}:{s15["side"]}:{s15["candle"]}'
        if state.get("last_signal",{}).get(coin)==stamp: continue
        entry=s15["entry"]; qty=(BUDGET_INR/USD_INR)/entry
        trade={"id":stamp,"coin":coin,"side":s15["side"],"timeframe":"5m + 15m confirmed","data_source":getattr(candles,"last_source","unknown"),"entry":entry,
          "sl":s15["sl"],"tp1":s15["tp1"],"tp2":s15["tp2"],"qty":qty,"budget_inr":BUDGET_INR,
          "rsi":round(s15["rsi"],2),"ema9":s15["ema9"],"ema21":s15["ema21"],
          "opened_at":now,"status":"OPEN","tp1_hit":False,"signal_candle":s15["candle"]}
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
    print("PAPER SUMMARY",len(state["open_trades"]),"open",len(state["closed_trades"]),"closed")

if __name__=="__main__": main()

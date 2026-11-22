#!/usr/bin/env python3
import json, os, urllib.request
COINS = ["BTC", "ETH", "BNB", "ZEC"]
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
STATE_FILE = "signal_state.json"
try:
    STATE=json.load(open(STATE_FILE))
except Exception:
    STATE={}

def save_state():
    with open(STATE_FILE,"w") as f: json.dump(STATE,f)

def candles(symbol, interval):
    urls=[
      f"https://api.binance.com/api/v3/klines?symbol={symbol}USDT&interval={interval}&limit=60",
      f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}USDT&interval={interval}&limit=60",
      f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}USDT&interval={interval}&limit=60"]
    errors=[]
    for url in urls:
      try:
        req=urllib.request.Request(url,headers={"User-Agent":"market-alerts/1.0"})
        with urllib.request.urlopen(req,timeout=12) as r: data=json.loads(r.read())
        if not isinstance(data,list) or len(data)<35: raise ValueError("not enough candles")
        return data[:-1]
      except Exception as e: errors.append(str(e))
    raise RuntimeError("all feeds failed: "+" | ".join(errors))

def ema(values,period):
    k=2/(period+1); out=values[0]
    for v in values[1:]: out=v*k+out*(1-k)
    return out

def rsi(values,period=14):
    gains=[]; losses=[]
    for a,b in zip(values[-period-1:-1],values[-period:]):
      d=b-a; gains.append(max(d,0)); losses.append(max(-d,0))
    ag=sum(gains)/period; al=sum(losses)/period
    return 100 if al==0 else 100-100/(1+ag/al)

BUDGET_INR = 500
USD_INR_BUFFER = 95
EXCHANGE_RULES = None

def minimum_order_cost_inr(coin, entry):
    global EXCHANGE_RULES
    if EXCHANGE_RULES is None:
        req=urllib.request.Request("https://fapi.binance.com/fapi/v1/exchangeInfo",
                                  headers={"User-Agent":"market-alerts/1.0"})
        with urllib.request.urlopen(req,timeout=15) as r:
            payload=json.loads(r.read())
        EXCHANGE_RULES={x["symbol"]:x for x in payload.get("symbols",[])}
    info=EXCHANGE_RULES.get(coin+"USDT")
    if not info:
        raise RuntimeError(coin+"USDT futures rules unavailable; alert blocked")
    filters=info.get("filters",[])
    lot=next((x for x in filters if x.get("filterType")=="LOT_SIZE"),None)
    if not lot or float(lot.get("minQty",0))<=0:
        raise RuntimeError(coin+"USDT minimum quantity unavailable; alert blocked")
    min_notional=0.0
    for f in filters:
        if f.get("filterType") in ("MIN_NOTIONAL","NOTIONAL"):
            min_notional=max(min_notional,float(f.get("notional",f.get("minNotional",0)) or 0))
    minimum_usd=max(float(lot["minQty"])*float(entry),min_notional)
    return minimum_usd*USD_INR_BUFFER

def analyze(coin,interval):
    rows=candles(coin,interval); c=[float(x[4]) for x in rows]
    o=[float(x[1]) for x in rows]; h=[float(x[2]) for x in rows]; l=[float(x[3]) for x in rows]
    e9=ema(c[-30:],9); e21=ema(c[-50:],21); rv=rsi(c); entry=c[-1]
    tr=[max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1])) for i in range(max(1,len(rows)-14),len(rows))]
    atr=sum(tr)/max(1,len(tr)); side="WAIT"
    if entry>e9 and e9>e21 and 45<=rv<=75 and entry>o[-1]: side="BUY / LONG"
    elif entry<e9 and e9<e21 and 25<=rv<=55 and entry<o[-1]: side="SELL / SHORT"
    return {"side":side,"entry":entry,"sl":entry-1.2*atr if side=="BUY / LONG" else entry+1.2*atr if side=="SELL / SHORT" else None,
      "tp1":entry+atr if side=="BUY / LONG" else entry-atr if side=="SELL / SHORT" else None,
      "tp2":entry+2*atr if side=="BUY / LONG" else entry-2*atr if side=="SELL / SHORT" else None,
      "rsi":rv,"ema9":e9,"ema21":e21,"candle":int(rows[-1][0]),"interval":interval}

def send(msg):
    if not TOKEN or not CHAT:
      print("Telegram secrets missing; signal printed to Actions log only."); return
    body=json.dumps({"chat_id":CHAT,"text":msg}).encode()
    req=urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage",data=body,headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req,timeout=15) as r: result=json.loads(r.read())
    if not result.get("ok"): raise RuntimeError(result.get("description","Telegram send failed"))

def main():
  for coin in COINS:
    try:
      s15=analyze(coin,"15m"); s5=analyze(coin,"5m")
      chosen=s15 if s15["side"]!="WAIT" else (s5 if s5["side"]!="WAIT" else None)
      if not chosen:
        print(f"{coin}: WAIT | 5m={s5['side']} 15m={s15['side']}"); continue
      # ₹500 cap is checked against exchange minimum futures order notional.
      # If exchange lot rules cannot be fetched, fail closed and send no trade alert.
      try:
        min_cost=minimum_order_cost_inr(coin,chosen["entry"])
      except Exception as budget_error:
        print(f"{coin}: budget check failed; alert blocked: {budget_error}"); continue
      if min_cost > BUDGET_INR:
        print(f"{coin}: alert blocked; exchange minimum order ≈ ₹{min_cost:.0f} exceeds ₹{BUDGET_INR}")
        continue
      typ="CONFIRMED 15m" if chosen is s15 else "EARLY 5m"
      stamp=f"{typ}|{chosen['side']}|{chosen['candle']}"
      key=f"signal_{coin}"
      if STATE.get(key)==stamp:
        print(f"{coin}: duplicate suppressed ({stamp})"); continue
      direction=("BUY CALL" if chosen["side"]=="BUY / LONG" else "BUY PUT") if coin in ("BTC","ETH") else chosen["side"]
      msg=(f"🚨 {coin} {typ} SIGNAL\nSignal: {direction}\nEntry: {chosen['entry']:.8g}\nSL: {chosen['sl']:.8g}\nTP1: {chosen['tp1']:.8g}\nTP2: {chosen['tp2']:.8g}\nRSI: {chosen['rsi']:.2f}\nEMA9: {chosen['ema9']:.8g} | EMA21: {chosen['ema21']:.8g}\n5m: {s5['side']} | 15m: {s15['side']}\nCandle UTC: {chosen['candle']}\nSignal only; no orders.")
      print(msg); send(msg); STATE[key]=stamp; save_state()
    except Exception as e:
      err=f"⚠️ {coin} DATA ERROR\nBackground feed failed: {e}\nOther coins continue."
      print(err)
      # Rate-limit repeated errors to one per coin per hour.
      hour=str(int(__import__("time").time()//3600)); key=f"error_{coin}"
      if STATE.get(key)!=hour:
        send(err); STATE[key]=hour; save_state()

if __name__=="__main__": main()

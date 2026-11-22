#!/usr/bin/env python3
import json, os, urllib.request, urllib.error, time
COINS = ["BTC", "ETH", "BNB", "ZEC"]
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

def candles(symbol, interval):
    urls = [
        f"https://api.binance.com/api/v3/klines?symbol={symbol}USDT&interval={interval}&limit=60",
        f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}USDT&interval={interval}&limit=60",
        f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}USDT&interval={interval}&limit=60",
    ]
    errors=[]
    for url in urls:
        try:
            req=urllib.request.Request(url,headers={"User-Agent":"market-alerts/1.0"})
            with urllib.request.urlopen(req,timeout=12) as r: data=json.loads(r.read())
            if not isinstance(data,list) or len(data)<35: raise ValueError("not enough candles")
            return data[:-1] # ignore current unfinished candle
        except Exception as e: errors.append(str(e))
    raise RuntimeError("all feeds failed: "+" | ".join(errors))

def ema(values, period):
    k=2/(period+1); out=values[0]
    for v in values[1:]: out=v*k+out*(1-k)
    return out

def rsi(values, period=14):
    gains=[]; losses=[]
    for a,b in zip(values[-period-1:-1],values[-period:]):
        d=b-a; gains.append(max(d,0)); losses.append(max(-d,0))
    avg_gain=sum(gains)/period; avg_loss=sum(losses)/period
    return 100 if avg_loss==0 else 100-100/(1+avg_gain/avg_loss)

def analyze(coin, interval):
    rows=candles(coin,interval); close=[float(x[4]) for x in rows]
    opens=[float(x[1]) for x in rows]; highs=[float(x[2]) for x in rows]; lows=[float(x[3]) for x in rows]
    e9=ema(close[-30:],9); e21=ema(close[-50:],21); rv=rsi(close)
    last=rows[-1]; entry=close[-1]
    tr=[]
    for i in range(max(1,len(rows)-14),len(rows)):
        tr.append(max(highs[i]-lows[i],abs(highs[i]-close[i-1]),abs(lows[i]-close[i-1])))
    atr=sum(tr)/max(1,len(tr))
    side="WAIT"
    if entry>e9 and e9>e21 and 45<=rv<=75 and entry>opens[-1]: side="BUY / LONG"
    elif entry<e9 and e9<e21 and 25<=rv<=55 and entry<opens[-1]: side="SELL / SHORT"
    return {"side":side,"entry":entry,"sl":entry-1.2*atr if side=="BUY / LONG" else entry+1.2*atr if side=="SELL / SHORT" else None,
            "tp1":entry+atr if side=="BUY / LONG" else entry-atr if side=="SELL / SHORT" else None,
            "tp2":entry+2*atr if side=="BUY / LONG" else entry-2*atr if side=="SELL / SHORT" else None,
            "rsi":rv,"ema9":e9,"ema21":e21,"candle":int(last[0]),"interval":interval}

def send(msg):
    if not TOKEN or not CHAT:
        print("Telegram secrets missing; signal printed to Actions log only.")
        return
    body=json.dumps({"chat_id":CHAT,"text":msg}).encode()
    req=urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage",data=body,headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req,timeout=15) as r:
        result=json.loads(r.read())
    if not result.get("ok"): raise RuntimeError(result.get("description","Telegram send failed"))

def main():
    for coin in COINS:
        try:
            s15=analyze(coin,"15m")
            s5=analyze(coin,"5m")
            chosen=s15 if s15["side"]!="WAIT" else (s5 if s5["side"]!="WAIT" else None)
            if chosen:
                typ="CONFIRMED 15m" if chosen is s15 else "EARLY 5m"
                direction=("BUY CALL" if chosen["side"]=="BUY / LONG" else "BUY PUT") if coin in ("BTC","ETH") else chosen["side"]
                msg=(f"🚨 {coin} {typ} SIGNAL\nSignal: {direction}\nEntry: {chosen['entry']:.8g}\nSL: {chosen['sl']:.8g}\nTP1: {chosen['tp1']:.8g}\nTP2: {chosen['tp2']:.8g}\nRSI: {chosen['rsi']:.2f}\nEMA9: {chosen['ema9']:.8g} | EMA21: {chosen['ema21']:.8g}\n5m: {s5['side']} | 15m: {s15['side']}\nCandle UTC: {chosen['candle']}\nSignal only; no orders.")
                print(msg); send(msg)
            else:
                print(f"{coin}: WAIT | 5m={s5['side']} 15m={s15['side']}")
        except Exception as e:
            err=f"⚠️ {coin} DATA ERROR\nBackground feed failed: {e}\nOther coins continue."
            print(err); send(err)

if __name__=="__main__": main()

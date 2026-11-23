import os, json, urllib.request, urllib.parse, datetime, sys

COINS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "ZECUSDT"]
STATE_PATH = ".signal-state/state.json"
INTERVAL = "15m"
CANDLE_MS = 900000

def request_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 market-signal-monitor", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read())

def get_klines(symbol):
    errors = []
    query = urllib.parse.urlencode({"symbol": symbol, "interval": INTERVAL, "limit": 100})
    for host in ("api.binance.com", "api1.binance.com", "api2.binance.com", "api3.binance.com", "data-api.binance.vision"):
        try:
            payload = request_json(f"https://{host}/api/v3/klines?{query}")
            if isinstance(payload, list) and len(payload) >= 30:
                print(f"{symbol}: data source Binance ({host})")
                return payload
            raise RuntimeError(f"Unexpected Binance response: {str(payload)[:160]}")
        except Exception as exc:
            errors.append(f"Binance {host}: {exc}")
    try:
        url = "https://api.bybit.com/v5/market/kline?" + urllib.parse.urlencode({"category":"spot","symbol":symbol,"interval":"15","limit":100})
        payload = request_json(url)
        if payload.get("retCode") == 0:
            rows = list(reversed(payload.get("result", {}).get("list", [])))
            if len(rows) >= 30:
                return [[int(r[0]),r[1],r[2],r[3],r[4],r[5],int(r[0])+CANDLE_MS] for r in rows]
        raise RuntimeError(f"Bybit response: {payload.get('retCode')} {payload.get('retMsg')}")
    except Exception as exc:
        errors.append(f"Bybit: {exc}")
    raise RuntimeError("All candle sources failed: " + " | ".join(errors[-3:]))

def ema(values, period):
    k = 2 / (period + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(value*k + out[-1]*(1-k))
    return out

def rsi(values, period=14):
    changes = [values[i]-values[i-1] for i in range(1,len(values))]
    gains = [max(c,0) for c in changes]
    losses = [max(-c,0) for c in changes]
    avg_gain = sum(gains[:period])/period
    avg_loss = sum(losses[:period])/period
    for i in range(period,len(changes)):
        avg_gain=(avg_gain*(period-1)+gains[i])/period
        avg_loss=(avg_loss*(period-1)+losses[i])/period
    if avg_loss == 0: return 100
    return 100-100/(1+avg_gain/avg_loss)

def send(msg):
    token=os.environ.get("TELEGRAM_BOT_TOKEN","")
    chat=os.environ.get("TELEGRAM_CHAT_ID","")
    if not token or not chat:
        print(f"TELEGRAM CONFIG ERROR: token={'present' if token else 'MISSING'}, chat_id={'present' if chat else 'MISSING'}")
        return False
    req=urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
        data=urllib.parse.urlencode({"chat_id":chat,"text":msg}).encode(),
        headers={"User-Agent":"market-signal-monitor"})
    try:
        with urllib.request.urlopen(req,timeout=20) as response:
            result=json.loads(response.read())
        if result.get("ok"):
            print("Telegram delivery: SUCCESS")
            return True
        print("Telegram delivery FAILED: "+str(result.get("description","unknown error")))
    except Exception as exc:
        print("Telegram delivery FAILED: "+str(exc))
    return False

state={}
try:
    with open(STATE_PATH) as f: state=json.load(f)
except Exception: pass

now=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
data_errors=0

for symbol in COINS:
    try:
        klines=get_klines(symbol)
        closes=[float(r[4]) for r in klines]
        highs=[float(r[2]) for r in klines]
        lows=[float(r[3]) for r in klines]
        times=[int(r[0]) for r in klines]
        # Exclude the still-forming candle; signal on every new CLOSED 15m candle.
        closes,highs,lows,times=closes[:-1],highs[:-1],lows[:-1],times[:-1]
        e9,e21=ema(closes,9),ema(closes,21)
        rv=rsi(closes)
        price,candle_ts=closes[-1],times[-1]
        prev_price,prev_e9=closes[-2],e9[-2]
        bullish=(price>e9[-1] and rv>=40 and rv<=82 and (price>prev_price or e9[-1]>=e21[-1]))
        bearish=(price<e9[-1] and rv>=18 and rv<=60 and (price<prev_price or e9[-1]<=e21[-1]))
        direction="WAIT"
        if bullish: direction="BUY / LONG"
        elif bearish: direction="SELL / SHORT"

        trs=[]
        for i in range(max(1,len(closes)-14),len(closes)):
            trs.append(max(highs[i]-lows[i],abs(highs[i]-closes[i-1]),abs(lows[i]-closes[i-1])))
        atr=sum(trs)/len(trs) if trs else 0
        sl=price-1.2*atr if direction=="BUY / LONG" else price+1.2*atr if direction=="SELL / SHORT" else None
        risk=abs(price-sl) if sl is not None else None
        targets=[price+risk,price+2*risk,price+3*risk] if direction=="BUY / LONG" else [price-risk,price-2*risk,price-3*risk] if direction=="SELL / SHORT" else [None,None,None]

        prev=state.get(symbol,{})
        new_signal=direction!="WAIT" and (prev.get("direction")!=direction or prev.get("candle")!=candle_ts)
        delivered=True
        if new_signal:
            candle_time=datetime.datetime.fromtimestamp(candle_ts/1000,datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            msg=(f"🚦 15-MIN TRADE SIGNAL — {symbol}\n{direction}\nClosed 15m candle: {candle_time}\n"
                 f"Entry: {price:.8g}\nStop Loss: {sl:.8g}\nTP1: {targets[0]:.8g}\nTP2: {targets[1]:.8g}\nTP3: {targets[2]:.8g}\n"
                 f"EMA9: {e9[-1]:.8g} | EMA21: {e21[-1]:.8g} | RSI14: {rv:.1f}\nSignal only — no order placement.\nChecked: {now}")
            delivered=send(msg)
        if not new_signal or delivered:
            state[symbol]={"direction":direction,"candle":candle_ts,"price":price,"ema9":e9[-1],"ema21":e21[-1],"rsi":rv,"entry":price,"sl":sl,"tp1":targets[0],"tp2":targets[1],"tp3":targets[2],"checked":now}
        else:
            print(f"{symbol}: delivery failed; will retry")
        print(f"{symbol}: {direction}; price={price}; RSI={rv:.1f}")
    except Exception as exc:
        data_errors+=1
        print(f"{symbol}: DATA ERROR: {exc}")

os.makedirs(os.path.dirname(STATE_PATH),exist_ok=True)
with open(STATE_PATH,"w") as f: json.dump(state,f,indent=2)
if data_errors:
    print(f"WARNING: {data_errors} coin(s) had data errors; next scan retries.")
    sys.exit(1)

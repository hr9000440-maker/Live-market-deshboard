import os, json, urllib.request, urllib.parse, math, datetime

COINS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "ZECUSDT"]
STATE_PATH = ".signal-state/state.json"

def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent":"market-signal-monitor"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def ema(values, period):
    k=2/(period+1)
    out=[values[0]]
    for x in values[1:]: out.append(x*k+out[-1]*(1-k))
    return out

def rsi(values, period=14):
    gains=[]; losses=[]
    for i in range(1,len(values)):
        d=values[i]-values[i-1]; gains.append(max(d,0)); losses.append(max(-d,0))
    avg_g=sum(gains[:period])/period; avg_l=sum(losses[:period])/period
    for i in range(period,len(gains)):
        avg_g=(avg_g*(period-1)+gains[i])/period
        avg_l=(avg_l*(period-1)+losses[i])/period
    if avg_l==0: return 100
    return 100-100/(1+avg_g/avg_l)

def send(msg):
    token=os.environ.get("TELEGRAM_BOT_TOKEN","")
    chat=os.environ.get("TELEGRAM_CHAT_ID","")
    if not token or not chat:
        print("Telegram secrets not configured; alert not sent.")
        return
    data=urllib.parse.urlencode({"chat_id":chat,"text":msg}).encode()
    req=urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",data=data)
    with urllib.request.urlopen(req,timeout=20) as r: print("Telegram:",r.status)

state={}
try:
    with open(STATE_PATH) as f: state=json.load(f)
except Exception: pass

now=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
for symbol in COINS:
    try:
        klines=get_json(f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=15m&limit=100")
        closes=[float(k[4]) for k in klines]
        times=[int(k[0]) for k in klines]
        # Use only closed candle; Binance's final row is often still forming.
        closes=closes[:-1]; times=times[:-1]
        e9=ema(closes,9); e21=ema(closes,21); rv=rsi(closes)
        price=closes[-1]; candle_ts=times[-1]
        direction="WAIT"
        if price>e9[-1]>e21[-1] and 40<=rv<=80: direction="BUY / LONG"
        elif price<e9[-1]<e21[-1] and 20<=rv<=60: direction="SELL / SHORT"
        prev=state.get(symbol,{})
        # Notify only when the closed 15m candle's direction changes to a trade signal.
        if direction!="WAIT" and (prev.get("direction")!=direction or prev.get("candle")!=candle_ts):
            msg=(f"📡 SERVER SIGNAL — {symbol}\n{direction}\n"
                 f"Closed 15m candle: {datetime.datetime.fromtimestamp(candle_ts/1000,datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
                 f"Entry reference: {price:.8g}\nEMA9: {e9[-1]:.8g} | EMA21: {e21[-1]:.8g}\nRSI14: {rv:.1f}\n"
                 f"SL/TP: risk-managed levels require exchange/contract-specific sizing; paper trade only.\nChecked: {now}")
            send(msg)
        state[symbol]={"direction":direction,"candle":candle_ts,"price":price,"ema9":e9[-1],"ema21":e21[-1],"rsi":rv,"checked":now}
        print(f"{symbol}: {direction} price={price} RSI={rv:.1f}")
    except Exception as e:
        print(f"{symbol}: DATA ERROR: {e}")
with open(STATE_PATH,"w") as f: json.dump(state,f,indent=2)

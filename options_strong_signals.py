#!/usr/bin/env python3
import json, os, urllib.request
COINS=('BTC','ETH'); BUDGET_INR=500; TOKEN=os.getenv('TELEGRAM_BOT_TOKEN',''); CHAT=os.getenv('TELEGRAM_CHAT_ID','')
try: STATE=json.load(open('options_algo_state.json'))
except Exception: STATE={}
def fetch(sym, interval):
 url='https://api.binance.com/api/v3/klines?symbol='+sym+'USDT&interval='+interval+'&limit=80'
 req=urllib.request.Request(url,headers={'User-Agent':'options-signal/1.0'})
 with urllib.request.urlopen(req,timeout=15) as r: rows=json.loads(r.read())
 if len(rows)<40: raise RuntimeError('insufficient candles')
 return rows[:-1]
def ema(v,p):
 k=2/(p+1); x=v[0]
 for n in v[1:]: x=n*k+x*(1-k)
 return x
def rsi(v,p=14):
 dif=[b-a for a,b in zip(v[-p-1:-1],v[-p:])]; g=sum(max(d,0) for d in dif)/p; l=sum(max(-d,0) for d in dif)/p
 return 100 if l==0 else 100-100/(1+g/l)
def analyze(coin):
 a=fetch(coin,'5m'); b=fetch(coin,'15m'); c5=[float(x[4]) for x in a]; c15=[float(x[4]) for x in b]
 e9=ema(c5[-30:],9); e21=ema(c5[-50:],21); e915=ema(c15[-30:],9); e2115=ema(c15[-50:],21); rv=rsi(c5); last=c5[-1]
 d='CALL' if last>e9>e21 and e915>e2115 and 55<=rv<=70 and last>float(a[-2][4]) else 'PUT' if last<e9<e21 and e915<e2115 and 30<=rv<=45 and last<float(a[-2][4]) else 'WAIT'
 return {'direction':d,'price':last,'rsi':rv,'candle':int(a[-1][0])}
def send(msg):
 if not TOKEN or not CHAT: print('Telegram secrets unavailable: '+msg); return
 data=json.dumps({'chat_id':CHAT,'text':msg}).encode(); req=urllib.request.Request('https://api.telegram.org/bot'+TOKEN+'/sendMessage',data=data,headers={'Content-Type':'application/json'})
 with urllib.request.urlopen(req,timeout=15) as r: result=json.loads(r.read())
 if not result.get('ok'): raise RuntimeError(result.get('description','Telegram failed'))
def main():
 for coin in COINS:
  try:
   s=analyze(coin)
   if s['direction']=='WAIT': print(coin+': no strong signal'); continue
   if STATE.get(coin)==s['candle'] and STATE.get(coin+'_direction')==s['direction']: print(coin+': duplicate suppressed'); continue
   msg='STRONG OPTIONS SIGNAL (SIGNAL ONLY)\n'+coin+' BUY '+s['direction']+'\nUnderlying: $'+format(s['price'],'.2f')+'\n5m+15m trend confirmed\nRSI: '+format(s['rsi'],'.1f')+'\nBudget cap: ₹'+str(BUDGET_INR)+'\nPremium/SL/targets unverified. No live order placed.'
   send(msg); STATE[coin]=s['candle']; STATE[coin+'_direction']=s['direction']
  except Exception as e: print(coin+' error: '+str(e))
 with open('options_algo_state.json','w') as f: json.dump(STATE,f)
if __name__=='__main__': main()
const fs = require('fs');

const token = process.env.TELEGRAM_BOT_TOKEN;
const chatId = process.env.TELEGRAM_CHAT_ID;
if (!token || !chatId) {
  console.error('Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID GitHub Actions secret.');
  process.exit(1);
}

const coins = ['BTC', 'ETH', 'BNB', 'ZEC'];
const statePath = 'state/signals.json';
let state = {};
try { state = JSON.parse(fs.readFileSync(statePath, 'utf8')); } catch {}

function ema(values, period) {
  const k = 2 / (period + 1);
  let out = values[0];
  for (let i = 1; i < values.length; i++) out = values[i] * k + out * (1 - k);
  return out;
}
function rsi(values, period = 14) {
  let gain = 0, loss = 0;
  for (let i = values.length - period; i < values.length; i++) {
    const d = values[i] - values[i - 1];
    if (d > 0) gain += d; else loss -= d;
  }
  if (loss === 0) return 100;
  return 100 - 100 / (1 + gain / loss);
}
function fmt(n) {
  return Number(n).toLocaleString('en-US', { maximumFractionDigits: 6 });
}
async function telegram(text) {
  const res = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ chat_id: chatId, text, disable_web_page_preview: true })
  });
  const data = await res.json();
  if (!res.ok || !data.ok) throw new Error(data.description || 'Telegram send failed');
}
async function analyze(coin) {
  const symbol = coin + 'USDT';
  const endpoints = [
    `https://api.binance.com/api/v3/klines?symbol=${symbol}&interval=15m&limit=60`,
    `https://data-api.binance.vision/api/v3/klines?symbol=${symbol}&interval=15m&limit=60`,
    `https://api.bybit.com/v5/market/kline?category=linear&symbol=${symbol}&interval=15&limit=60`
  ];
  let rows, errors = [];
  for (const url of endpoints) {
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(12000) });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const j = await res.json();
      const data = Array.isArray(j) ? j : (j.result?.list || []);
      const normalized = data.map(x => Array.isArray(x)
        ? { t:+x[0], o:+x[1], h:+x[2], l:+x[3], c:+x[4], v:+x[5] }
        : { t:+x[0], o:+x[1], h:+x[2], l:+x[3], c:+x[4], v:+x[5] }).sort((a,b)=>a.t-b.t);
      if (normalized.length < 35 || normalized.some(x => !Number.isFinite(x.c) || x.c <= 0)) throw new Error('Invalid candle data');
      rows = normalized;
      break;
    } catch (e) { errors.push(e.message); }
  }
  if (!rows) throw new Error(coin + ' feed failed: ' + errors.join(' / '));

  // Exclude the currently-forming candle; only closed 15-minute candles can signal.
  const closed = rows.slice(0, -1);
  const last = closed.at(-1);
  const closes = closed.map(x=>x.c);
  const e9 = ema(closes.slice(-30), 9);
  const e21 = ema(closes.slice(-50), 21);
  const rv = rsi(closes, 14);
  const buy = last.c > e9 && e9 > e21 && rv >= 45 && rv <= 75 && last.c > last.o;
  const sell = last.c < e9 && e9 < e21 && rv >= 25 && rv <= 55 && last.c < last.o;
  const side = buy ? 'BUY / LONG' : sell ? 'SELL / SHORT' : 'WAIT';
  if (side === 'WAIT') return { coin, side, candleTime: last.t };

  const ranges = [];
  for (let i = closed.length - 14; i < closed.length; i++) {
    const p = closed[i-1].c, x = closed[i];
    ranges.push(Math.max(x.h-x.l, Math.abs(x.h-p), Math.abs(x.l-p)));
  }
  const atr = ranges.reduce((a,b)=>a+b,0)/ranges.length;
  const entry = last.c;
  const sl = side === 'BUY / LONG' ? entry - 1.2*atr : entry + 1.2*atr;
  const risk = Math.abs(entry-sl);
  const tp1 = side === 'BUY / LONG' ? entry+risk : entry-risk;
  const tp2 = side === 'BUY / LONG' ? entry+2*risk : entry-2*risk;
  return { coin, side, candleTime: last.t, entry, sl, tp1, tp2, rv, e9, e21 };
}

(async () => {
  for (const coin of coins) {
    try {
      const s = await analyze(coin);
      if (s.side === 'WAIT') { console.log(coin + ': WAIT'); continue; }
      const key = coin + ':' + s.candleTime + ':' + s.side;
      if (state[key]) { console.log(coin + ': duplicate skipped'); continue; }
      const direction = (coin === 'BTC' || coin === 'ETH')
        ? (s.side === 'BUY / LONG' ? 'BUY CALL' : 'BUY PUT')
        : s.side;
      const when = new Date(s.candleTime + 900000).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' });
      const message = [
        '🚨 CRYPTO CONFIRMED 15m SIGNAL',
        'Coin: ' + coin,
        'Signal: ' + direction,
        'Entry: ' + fmt(s.entry),
        '🛑 SL: ' + fmt(s.sl),
        '🎯 TP1: ' + fmt(s.tp1),
        '🎯 TP2: ' + fmt(s.tp2),
        'RSI: ' + fmt(s.rv),
        'EMA9: ' + fmt(s.e9) + ' | EMA21: ' + fmt(s.e21),
        'Candle: ' + when + ' (IST)',
        'Signal only — automatic order नहीं।'
      ].join('\n');
      await telegram(message);
      state[key] = new Date().toISOString();
      console.log(coin + ': Telegram signal sent');
    } catch (e) { console.error(coin + ': ' + e.message); }
  }
  fs.mkdirSync('state', { recursive: true });
  const entries = Object.entries(state).sort((a,b)=>a[1].localeCompare(b[1])).slice(-500);
  fs.writeFileSync(statePath, JSON.stringify(Object.fromEntries(entries), null, 2) + '\n');
})().catch(e => { console.error(e); process.exit(1); });

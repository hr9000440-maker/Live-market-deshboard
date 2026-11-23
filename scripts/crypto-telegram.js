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
  if (values.length < period) return NaN;
  const k = 2 / (period + 1);
  let out = values[0];
  for (let i = 1; i < values.length; i++) out = values[i] * k + out * (1 - k);
  return out;
}
function rsi(values, period = 14) {
  if (!Array.isArray(values) || values.length < period + 1) return NaN;
  let avgGain = 0, avgLoss = 0;
  for (let i = 1; i <= period; i++) {
    const d = values[i] - values[i - 1];
    avgGain += Math.max(d, 0); avgLoss += Math.max(-d, 0);
  }
  avgGain /= period; avgLoss /= period;
  for (let i = period + 1; i < values.length; i++) {
    const d = values[i] - values[i - 1];
    avgGain = (avgGain * (period - 1) + Math.max(d, 0)) / period;
    avgLoss = (avgLoss * (period - 1) + Math.max(-d, 0)) / period;
  }
  if (avgGain === 0 && avgLoss === 0) return 50;
  if (avgLoss === 0) return 100;
  return 100 - 100 / (1 + avgGain / avgLoss);
}
function fmt(n) {
  return Number(n).toLocaleString('en-US', { maximumFractionDigits: 6 });
}
async function telegram(text) {
  const res = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ chat_id: chatId, text, disable_web_page_preview: true }),
    signal: AbortSignal.timeout(15000)
  });
  const data = await res.json();
  if (!res.ok || !data.ok) throw new Error(data.description || 'Telegram send failed');
}
async function analyze(coin) {
  const symbol = coin + 'USDT';
  const endpoints = [
    `https://api.binance.com/api/v3/klines?symbol=${symbol}&interval=15m&limit=100`,
    `https://data-api.binance.vision/api/v3/klines?symbol=${symbol}&interval=15m&limit=100`,
    `https://api.bybit.com/v5/market/kline?category=spot&symbol=${symbol}&interval=15&limit=100`
  ];
  let rows, errors = [];
  for (const url of endpoints) {
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(12000) });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const j = await res.json();
      if (!Array.isArray(j) && j.retCode !== 0) throw new Error(j.retMsg || 'API error');
      const data = Array.isArray(j) ? j : (j.result?.list || []);
      const normalized = data.map(x => Array.isArray(x)
        ? { t:+x[0], o:+x[1], h:+x[2], l:+x[3], c:+x[4], v:+x[5] }
        : { t:+x[0], o:+x[1], h:+x[2], l:+x[3], c:+x[4], v:+x[5] }).sort((a,b)=>a.t-b.t);
      if (normalized.length < 60 || normalized.some(x => ![x.t,x.o,x.h,x.l,x.c,x.v].every(Number.isFinite) || x.c<=0 || x.h<x.l || x.v<0)) throw new Error('Invalid/incomplete candles');
      rows = normalized;
      break;
    } catch (e) { errors.push(e.message); }
  }
  if (!rows) throw new Error(coin + ' feed failed: ' + errors.join(' / '));

  // Ignore the active candle; signal only from confirmed 15m closes.
  const closed = rows.slice(0, -1);
  const last = closed.at(-1), prev = closed.at(-2);
  const closes = closed.map(x=>x.c);
  const e9 = ema(closes.slice(-40), 9), e21 = ema(closes.slice(-60), 21), e50 = ema(closes.slice(-80), 50);
  const rv = rsi(closes, 14);
  const avgVol = closed.slice(-21,-1).reduce((a,x)=>a+x.v,0)/20;
  const volumeOK = last.v >= avgVol * 0.8;
  const bullishCandle = last.c > last.o && last.c > prev.c && (last.c-last.l) >= (last.h-last.l)*0.55;
  const bearishCandle = last.c < last.o && last.c < prev.c && (last.h-last.c) >= (last.h-last.l)*0.55;
  const atrRows = closed.slice(-15);
  const ranges = atrRows.map((x,i) => {
    const p = closed[closed.length-15+i-1]?.c ?? x.c;
    return Math.max(x.h-x.l, Math.abs(x.h-p), Math.abs(x.l-p));
  });
  const atr = ranges.reduce((a,b)=>a+b,0)/ranges.length;
  const atrPct = atr/last.c*100;
  // Fail-safe: avoid abnormal volatility and weak/uncorroborated setups.
  const buy = last.c > e9 && e9 > e21 && e21 > e50 && rv >= 50 && rv <= 70 && bullishCandle && volumeOK && atrPct <= 3.5;
  const sell = last.c < e9 && e9 < e21 && e21 < e50 && rv >= 30 && rv <= 50 && bearishCandle && volumeOK && atrPct <= 3.5;
  const side = buy ? 'BUY / LONG' : sell ? 'SELL / SHORT' : 'WAIT';
  if (side === 'WAIT') return { coin, side, candleTime: last.t, diagnostics: { rsi:rv, volumeOK, atrPct } };

  const entry = last.c;
  const sl = side === 'BUY / LONG' ? entry - 1.5*atr : entry + 1.5*atr;
  const risk = Math.abs(entry-sl);
  const tp1 = side === 'BUY / LONG' ? entry+risk : entry-risk;
  const tp2 = side === 'BUY / LONG' ? entry+2*risk : entry-2*risk;
  return { coin, side, candleTime: last.t, entry, sl, tp1, tp2, rv, e9, e21, atrPct, lastVol:last.v, avgVol };
}

(async () => {
  const errors = [];
  let signals = 0;
  for (const coin of coins) {
    try {
      const s = await analyze(coin);
      if (s.side === 'WAIT') { console.log(coin + ': no qualified signal', JSON.stringify(s.diagnostics)); continue; }
      const key = coin + ':' + s.candleTime + ':' + s.side;
      if (state[key]) { console.log(coin + ': duplicate skipped'); continue; }
      const direction = (coin === 'BTC' || coin === 'ETH')
        ? (s.side === 'BUY / LONG' ? 'BUY CALL' : 'BUY PUT')
        : s.side;
      const when = new Date(s.candleTime + 900000).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' });
      const message = [
        '🚨 HIGH-CONFIDENCE FILTERED 15m SIGNAL',
        'Coin: ' + coin,
        'Signal: ' + direction,
        'Entry: ' + fmt(s.entry),
        '🛑 SL: ' + fmt(s.sl),
        '🎯 TP1: ' + fmt(s.tp1),
        '🎯 TP2: ' + fmt(s.tp2),
        'RSI14: ' + fmt(s.rv),
        'EMA9: ' + fmt(s.e9) + ' | EMA21: ' + fmt(s.e21),
        'ATR: ' + fmt(s.atrPct) + '% | Volume: ' + (s.lastVol >= s.avgVol ? 'above avg' : 'near avg'),
        'Closed candle: ' + when + ' (IST)',
        'Signal only — no automatic order. Risk remains; not guaranteed.'
      ].join('\n');
      await telegram(message);
      state[key] = new Date().toISOString();
      const perfPath = 'state/performance.json';
      let perf = { trades: [] };
      try { perf = JSON.parse(fs.readFileSync(perfPath, 'utf8')); } catch {}
      perf.trades = Array.isArray(perf.trades) ? perf.trades : [];
      if (!perf.trades.some(t => t.key === key)) perf.trades.push({ key, coin, side: s.side, candleTime: s.candleTime, entry: s.entry, sl: s.sl, tp1: s.tp1, tp2: s.tp2, status: 'OPEN', createdAt: new Date().toISOString() });
      fs.mkdirSync('state', { recursive: true });
      fs.writeFileSync(perfPath, JSON.stringify(perf, null, 2) + '\\n');
      signals++;
      console.log(coin + ': Telegram signal sent');
    } catch (e) {
      errors.push(coin + ': ' + e.message);
      console.error(coin + ': ' + e.message);
    }
  }
  fs.mkdirSync('state', { recursive: true });
  const entries = Object.entries(state).sort((a,b)=>a[1].localeCompare(b[1])).slice(-500);
  fs.writeFileSync(statePath, JSON.stringify(Object.fromEntries(entries), null, 2) + '\n');
  if (errors.length) {
    const hour = new Date().toISOString().slice(0,13);
    const key = 'health:' + hour;
    if (!state[key]) {
      try { await telegram('⚠️ DASHBOARD SIGNAL HEALTH\n' + errors.join('\n') + '\nHealthy coins continue; failed coins retry next scan.'); state[key]=new Date().toISOString(); } catch (e) { console.error('Health alert failed: '+e.message); }
    }
  }
  console.log(`Scan complete: ${signals} new signal(s), ${errors.length} data error(s), 4 coins checked.`);
  fs.writeFileSync(statePath, JSON.stringify(Object.fromEntries(Object.entries(state).sort((a,b)=>a[1].localeCompare(b[1])).slice(-500)), null, 2) + '\n');
})().catch(e => { console.error(e); process.exit(1); });

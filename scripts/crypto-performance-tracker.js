const fs=require('fs');
const token=process.env.TELEGRAM_BOT_TOKEN, chatId=process.env.TELEGRAM_CHAT_ID;
const file='state/performance.json';
let perf={trades:[]}; try{perf=JSON.parse(fs.readFileSync(file,'utf8'));}catch{}
perf.trades=Array.isArray(perf.trades)?perf.trades:[];
async function telegram(text){
 const r=await fetch(`https://api.telegram.org/bot${token}/sendMessage`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({chat_id:chatId,text}),signal:AbortSignal.timeout(15000)});
 const j=await r.json(); if(!r.ok||!j.ok)throw Error(j.description||'Telegram send failed');
}
async function candles(coin){
 const symbol=coin+'USDT';
 for(const url of [`https://api.binance.com/api/v3/klines?symbol=${symbol}&interval=15m&limit=100`,`https://data-api.binance.vision/api/v3/klines?symbol=${symbol}&interval=15m&limit=100`]){
  try{const r=await fetch(url,{signal:AbortSignal.timeout(12000)});if(!r.ok)throw Error('HTTP '+r.status);const rows=await r.json();if(!Array.isArray(rows)||rows.length<2)throw Error('bad candle data');return rows.slice(0,-1).map(x=>({t:+x[0],h:+x[2],l:+x[3],c:+x[4]}));}catch(e){console.log(coin+' source failed: '+e.message);}
 }
 throw Error('both candle feeds unavailable');
}
(async()=>{
 let changes=[];
 for(const coin of ['BTC','ETH','BNB','ZEC']){
  const open=perf.trades.filter(t=>t.coin===coin&&['OPEN','TP1 HIT'].includes(t.status));
  if(!open.length)continue;
  let rows;try{rows=await candles(coin);}catch(e){console.error(e.message);continue;}
  for(const t of open){
   const later=rows.filter(c=>c.t>t.candleTime);
   for(const c of later){
    const buy=t.side==='BUY / LONG';
    const sl=buy?c.l<=t.sl:c.h>=t.sl;
    const tp1=buy?c.h>=t.tp1:c.l<=t.tp1;
    const tp2=buy?c.h>=t.tp2:c.l<=t.tp2;
    // Conservative: if SL and target touched in the same candle, assume SL first.
    if(sl){t.status=t.status==='TP1 HIT'?'SL AFTER TP1':'SL HIT';t.closedAt=new Date(c.t+900000).toISOString();changes.push(`${t.coin} ${t.side}: ${t.status}`);break;}
    if(tp2){t.status='TP2 HIT';t.closedAt=new Date(c.t+900000).toISOString();changes.push(`${t.coin} ${t.side}: TP2 HIT`);break;}
    if(tp1&&t.status==='OPEN'){t.status='TP1 HIT';t.tp1At=new Date(c.t+900000).toISOString();changes.push(`${t.coin} ${t.side}: TP1 HIT (tracking continues)`);}
   }
  }
 }
 fs.mkdirSync('state',{recursive:true});fs.writeFileSync(file,JSON.stringify(perf,null,2)+'\n');
 if(changes.length){await telegram('📊 SIGNAL PERFORMANCE UPDATE\n'+changes.join('\n')+'\nSignal tracking only; outcomes use candle high/low and conservative SL-first assumption.');}
 const totals={};for(const t of perf.trades){totals[t.status]=(totals[t.status]||0)+1;}
 console.log('Performance tracking: '+JSON.stringify(totals)+'; updates='+changes.length);
})().catch(e=>{console.error(e);process.exit(1);});

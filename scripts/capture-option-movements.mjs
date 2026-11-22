import fs from 'node:fs';
const OUT='data/option-movement-snapshots.json';
const coins=['BTC','ETH','BNB','ZEC'];
const now=new Date().toISOString();
let previous=[];
if(fs.existsSync(OUT)){
 try{
  const raw=fs.readFileSync(OUT,'utf8').trim();
  if(raw) { const parsed=JSON.parse(raw); if(Array.isArray(parsed)) previous=parsed; else console.warn('Existing snapshot file is not an array; starting a fresh history.'); }
 }catch(e){ console.warn('Existing snapshot file is empty or invalid; starting a fresh history:',e.message); }
}
const snap=[];
const add=(source,coin,contract,mark,bid,ask,underlying=0,oi=0,volume24h=0,extra={})=>{
 mark=Number(mark||0);bid=Number(bid||0);ask=Number(ask||0);
 if(!(mark>0))return false;
 snap.push({time:now,source,coin,contract,mark,bid,ask,spreadPct:bid>0&&ask>=bid?((ask-bid)/((ask+bid)/2))*100:null,underlying:Number(underlying||0),oi:Number(oi||0),volume24h:Number(volume24h||0),...extra});
 return true;
};
async function json(url,options={}){
 const r=await fetch(url,{...options,headers:{Accept:'application/json',...(options.headers||{})}});
 if(!r.ok)throw new Error('HTTP '+r.status);
 return r.json();
}
async function captureDeribit(coin){
 const j=await json('https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency='+coin+'&kind=option');
 if(j.error)throw new Error(j.error.message);
 let n=0;
 for(const q of (j.result||[]))if(add('Deribit',coin,q.instrument_name,q.mark_price,q.bid_price,q.ask_price,q.underlying_price,q.open_interest,q.volume))n++;
 return n;
}
async function captureParadex(coin){
 const base='https://api.prod.paradex.trade/v1/';
 const [markets,summary]=await Promise.all([
  json(base+'markets?asset_kind=OPTION&base_asset='+coin),
  json(base+'markets/summary?market=ALL&asset_kind=OPTION&base_asset='+coin)
 ]);
 const marketRows=markets.results||markets.data||[];
 const summaryRows=summary.results||summary.data||[];
 if(!Array.isArray(marketRows)||!Array.isArray(summaryRows))throw new Error('Paradex response format बदल गया');
 const bySymbol=new Map(summaryRows.map(x=>[x.symbol||x.market||x.instrument_name,x]));
 let n=0;
 for(const m of marketRows){
  const name=m.symbol||m.market||m.instrument_name;
  if(!name||!String(name).toUpperCase().includes(coin))continue;
  const q=bySymbol.get(name)||{};
  const mark=q.mark_price??q.mark??m.mark_price;
  const bid=q.bid??q.bid_price??m.bid_price??0;
  const ask=q.ask??q.ask_price??m.ask_price??0;
  if(add('Paradex',coin,name,mark,bid,ask,q.underlying_price??m.underlying_price,q.open_interest??m.open_interest,q.volume_24h??q.total_volume??m.volume_24h))n++;
 }
 if(!n)throw new Error('Paradex पर '+coin+' के live option quotes नहीं मिले');
 return n;
}
async function captureDelta(coin){
 const url='https://api.india.delta.exchange/v2/tickers?contract_types=call_options%2Cput_options&underlying_asset_symbols='+coin;
 const j=await json(url);
 let n=0;
 for(const q of (j.result||[]))if(add('Delta Exchange India',coin,q.symbol||String(q.product_id||''),q.mark_price,q.quotes?.best_bid,q.quotes?.best_ask,q.spot_price,q.oi,q.volume))n++;
 return n;
}
async function captureBinanceBnb(){
 const info=await json('https://eapi.binance.com/eapi/v1/exchangeInfo');
 const symbols=(info.optionSymbols||[]).filter(s=>s.status==='TRADING'&&String(s.symbol||'').startsWith('BNB-'));
 if(!symbols.length)throw new Error('No live BNB option symbols returned by Binance');
 const [tickers,marks]=await Promise.all([
  json('https://eapi.binance.com/eapi/v1/ticker'),
  json('https://eapi.binance.com/eapi/v1/mark')
 ]);
 const tm=new Map((tickers||[]).map(x=>[x.symbol,x]));
 const mm=new Map((marks||[]).map(x=>[x.symbol,x]));
 let n=0;
 for(const s of symbols){
  const t=tm.get(s.symbol)||{},m=mm.get(s.symbol)||{};
  if(add('Binance Options', 'BNB',s.symbol,m.markPrice||t.lastPrice,t.bidPrice,t.askPrice,0,0,t.volume,{delta:Number(m.delta||0),markIV:Number(m.markIV||0)}))n++;
 }
 return n;
}
async function captureDeriveZec(){
 const base='https://api.lyra.finance/public/';
 const instrumentsResponse=await json(base+'get_instruments',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({currency:'ZEC',instrument_type:'option',expired:false})});
 const instruments=instrumentsResponse.result||instrumentsResponse.instruments||[];
 if(!instruments.length)throw new Error('No active ZEC option instruments returned by Derive');
 const expiries=[...new Set(instruments.map(x=>x.expiry_date||x.expiryDate).filter(Boolean))];
 let n=0;
 for(const expiry of expiries){
  const response=await json(base+'get_tickers',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({currency:'ZEC',instrument_type:'option',expiry_date:expiry})});
  const rows=response.result||response.tickers||[];
  for(const q of rows){
   const name=q.instrument_name||q.instrumentName||q.symbol||'';
   const price=q.mark_price??q.markPrice??q.option_pricing?.mark_price??q.option_pricing?.markPrice;
   const bid=q.best_bid_price??q.bid_price??q.bidPrice??0,ask=q.best_ask_price??q.ask_price??q.askPrice??0;
   if(add('Derive', 'ZEC',name,price,bid,ask,q.index_price??q.indexPrice, q.open_interest??q.openInterest,q.volume??q.volume_24h,{delta:Number(q.option_pricing?.delta??q.delta??0),markIV:Number(q.option_pricing?.mark_iv??q.mark_iv??q.markIV??0)}))n++;
  }
 }
 return n;
}
for(const coin of coins){
 let captured=0;
 try{captured=await captureDeribit(coin)}catch(e){console.error(coin+' Deribit failed:',e.message)}
 if(captured===0&&(coin==='BNB'||coin==='ZEC')){
  try{captured=await captureParadex(coin)}catch(e){console.error(coin+' Paradex failed:',e.message)}
 }
 if(captured===0&&coin==='BNB'){
  try{captured=await captureBinanceBnb()}catch(e){console.error('BNB Binance Options failed:',e.message)}
 }
 if(captured===0&&coin==='ZEC'){
  try{captured=await captureDeriveZec()}catch(e){console.error('ZEC Derive failed:',e.message)}
 }
 if(captured===0){
  try{captured=await captureDelta(coin)}catch(e){console.error(coin+' Delta fallback failed:',e.message)}
 }
 console.log(coin+': '+captured+' option quote rows');
}
if(snap.length===0){
 console.error('CAPTURE FAILED: zero option quotes captured for BTC, ETH, BNB, and ZEC. Snapshot file was not overwritten.');
 process.exit(1);
}
const combined=[...snap,...previous].slice(0,25000);
fs.mkdirSync('data',{recursive:true});fs.writeFileSync(OUT,JSON.stringify(combined,null,2)+'\n');
console.log('CAPTURE SUCCESS: '+snap.length+' fresh quote rows; total stored '+combined.length);

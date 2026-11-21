import fs from 'node:fs';
const OUT='data/option-movement-snapshots.json';
const coins=['BTC','ETH'];
const now=new Date().toISOString();
const previous=fs.existsSync(OUT)?JSON.parse(fs.readFileSync(OUT,'utf8')):[];
const snap=[];
for(const coin of coins){
 try{
  const r=await fetch('https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency='+coin+'&kind=option');
  if(!r.ok)throw new Error('HTTP '+r.status);
  const j=await r.json();if(j.error)throw new Error(j.error.message);
  for(const q of (j.result||[])){
   const mark=Number(q.mark_price||0),bid=Number(q.bid_price||0),ask=Number(q.ask_price||0);
   if(mark<=0)continue;
   snap.push({time:now,coin,contract:q.instrument_name,mark,bid,ask,spreadPct:bid>0&&ask>=bid?((ask-bid)/((ask+bid)/2))*100:null,underlying:Number(q.underlying_price||0),oi:Number(q.open_interest||0),volume24h:Number(q.volume||0)});
  }
 }catch(e){console.error(coin+' capture failed:',e.message)}
}
const combined=[...snap,...previous].slice(0,25000);
fs.mkdirSync('data',{recursive:true});fs.writeFileSync(OUT,JSON.stringify(combined,null,2));
console.log('Captured '+snap.length+' quote rows; total stored '+combined.length);

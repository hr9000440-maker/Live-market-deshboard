(function(){
'use strict';
const panel=document.createElement('section');
panel.id='autoDiagnosticsPanel';
panel.style.cssText='position:fixed;right:10px;bottom:10px;z-index:99999;width:min(390px,calc(100vw - 20px));max-height:65vh;overflow:auto;background:#101b2b;color:#eaf2ff;border:1px solid #42617f;border-radius:12px;padding:12px;font:13px/1.45 system-ui;box-shadow:0 8px 30px #0009;display:none';
panel.innerHTML='<div style="display:flex;align-items:center;justify-content:space-between;gap:8px"><b>🔎 AUTOMATIC SYSTEM CHECK</b><button id="diagClose" style="background:#30465f;color:white;border:0;border-radius:6px;padding:5px 9px">बंद</button></div><div id="diagStatus" style="margin:8px 0">जाँच शुरू हो रही है…</div><div id="diagBody"></div><div style="color:#9fb2ca;font-size:11px;margin-top:8px">हर 60 सेकंड जाँच • केवल read-only • कोई ऑर्डर नहीं</div>';
const toggle=document.createElement('button');
toggle.id='diagToggle';
toggle.textContent='🔎 सिस्टम जाँच';
toggle.style.cssText='position:fixed;right:10px;bottom:10px;z-index:100000;background:#1e6c58;color:white;border:0;border-radius:24px;padding:11px 15px;font-weight:700;box-shadow:0 3px 14px #0008';
document.addEventListener('DOMContentLoaded',()=>{
 document.body.appendChild(panel);document.body.appendChild(toggle);
 toggle.addEventListener('click',()=>{panel.style.display=panel.style.display==='none'?'block':'none';if(panel.style.display==='block')runCheck();});
 panel.querySelector('#diagClose').addEventListener('click',()=>panel.style.display='none');
 runCheck();setInterval(runCheck,60000);
});
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function status(v){return v==='OK'?'🟢 ठीक':v==='ERROR'?'🔴 त्रुटि':v==='UNAVAILABLE'?'🟠 उपलब्ध नहीं':v==='BAD'?'🔴 गलत':'🟡 जाँच जारी';}
async function runCheck(){
 const statusEl=document.getElementById('diagStatus'),body=document.getElementById('diagBody');if(!statusEl||!body)return;
 statusEl.textContent='सर्वर और चारों कॉइन की जाँच हो रही है…';
 try{
  const r=await fetch('/api/diagnostics?ts='+Date.now(),{cache:'no-store'});if(!r.ok)throw new Error('Diagnostics HTTP '+r.status);
  const d=await r.json();let bad=0;
  const rows=(d.coin_checks||[]).map(c=>{
   if(c.spot!=='OK'||c.futures!=='OK'||c.options!=='OK')bad++;
   return '<tr><td><b>'+esc(c.coin)+'</b></td><td>'+status(c.spot)+'</td><td>'+status(c.futures)+'</td><td>'+status(c.options)+' <small>('+esc(c.option_count)+')</small></td></tr>'+
    (c.errors&&c.errors.length?'<tr><td colspan="4" style="color:#ffb7a8;font-size:11px">'+esc(c.errors.join(' • '))+'</td></tr>':'');
  }).join('');
  const st=d.coinswitch_stream||{},streamRows=Object.entries(st.coins||{}).map(([coin,x])=>'<tr><td>'+esc(coin)+'</td><td>'+esc(x.candles)+' candles</td><td>'+(x.fresh?'🟢 Fresh':x.age_seconds==null?'🔴 No data':'🟠 '+esc(x.age_seconds)+' sec पुराना')+'</td></tr>').join('');
  const errors=d.browser_errors||[];
  statusEl.innerHTML='<b>'+(bad===0?'🟢 API जाँच ठीक':'🟠 '+bad+' कॉइन में जाँच बाकी/समस्या')+'</b><br><small>जाँच: '+esc(new Date(d.checked_at).toLocaleString('hi-IN'))+'</small>';
  body.innerHTML='<b>मार्केट API</b><table style="width:100%;border-collapse:collapse;margin:5px 0"><tr><th>Coin</th><th>Spot</th><th>Futures</th><th>Options</th></tr>'+rows+'</table><b>CoinSwitch WebSocket</b><div>कनेक्शन: '+esc(st.connection||'—')+' • आखिरी डेटा: '+esc(st.last_message_at||'नहीं मिला')+'</div><table style="width:100%">'+streamRows+'</table>'+(st.last_error?'<div style="color:#ffb7a8">Stream error: '+esc(st.last_error)+'</div>':'')+'<b>Browser errors ('+errors.length+')</b>'+(errors.length?'<ul>'+errors.map(e=>'<li>'+esc(e.kind)+': '+esc(e.message)+' <small>'+esc(e.source)+' '+esc(e.line)+'</small></li>').join('')+'</ul>':'<div>अभी कोई रिपोर्टेड browser error नहीं।</div>')+'<div style="font-size:11px;color:#c6d4e5;margin-top:6px">'+esc(d.note||'')+'</div>';
 }catch(e){statusEl.innerHTML='<span style="color:#ff9c8c">🔴 जाँच नहीं हो सकी: '+esc(e.message)+'</span>';}
}
function sendError(kind,message,source,line){
 try{fetch('/api/diagnostics/browser-error',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind,message:String(message||'').slice(0,300),source:String(source||'').slice(0,240),line:Number(line)||0}),keepalive:true});}catch(_){}
}
window.addEventListener('error',e=>sendError('error',e.message,e.filename,e.lineno));
window.addEventListener('unhandledrejection',e=>sendError('promise',e.reason&&e.reason.message||e.reason,'',0));
})();
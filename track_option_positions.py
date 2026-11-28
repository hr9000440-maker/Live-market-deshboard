#!/usr/bin/env python3
"""Server-side premium monitor for contracts emitted by options_strong_signals.py.
Alert-only: never places or closes orders.
"""
import json, os, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone

API = "https://live-market-deshboard.onrender.com/api/options/{coin}"
PATH = "state/option_positions.json"
TOKENS = ("TELEGRAM_BOT_TOKEN", "TELEGRAMBOTTOKAN", "TELEGRAM_BOT_TOKAN", "TELEGRAM_BOT_FATHER")

def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent":"live-market-dashboard/1.0","Accept":"application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())

def telegram(msg):
    tokens=list(dict.fromkeys(os.getenv(k,"").strip() for k in TOKENS if os.getenv(k,"").strip()))
    chat=os.getenv("TELEGRAM_CHAT_ID","").strip()
    if not tokens or not chat:
        raise RuntimeError("Telegram secrets missing")
    data=urllib.parse.urlencode({"chat_id":chat,"text":msg,"disable_web_page_preview":"true"}).encode()
    errors=[]
    for token in tokens:
        req=urllib.request.Request("https://api.telegram.org/bot"+token+"/sendMessage",data=data)
        try:
            with urllib.request.urlopen(req,timeout=20) as r:
                result=json.loads(r.read().decode())
            if result.get("ok"): return
            errors.append(str(result.get("description","Telegram error")))
        except urllib.error.HTTPError as e:
            errors.append("HTTP "+str(e.code))
            if e.code!=401: break
    raise RuntimeError("Telegram send failed: "+"; ".join(errors))

def main():
    try:
        with open(PATH,encoding="utf-8") as f: positions=json.load(f)
    except FileNotFoundError:
        positions=[]
    except json.JSONDecodeError as e:
        print("ERROR option positions ledger is invalid JSON; refusing to overwrite: " + str(e))
        raise SystemExit(1)
    changed=False
    cache={}
    for p in positions:
        # Retry an undelivered Telegram result/risk alert, including for positions already closed.
        pending=p.get("pending_telegram")
        if pending:
            try:
                telegram(pending)
                p.pop("pending_telegram",None)
                changed=True
            except Exception as notify_error:
                print("WARN pending Telegram alert not delivered: "+str(notify_error))
                continue
        if p.get("status")!="ACTIVE": continue
        coin=p["coin"]
        try:
            if coin not in cache: cache[coin]=get_json(API.format(coin=coin))
            payload=cache[coin]
            rows=payload.get("rows") or []
            row=next((x for x in rows if str(x.get("instrument_name") or "")==p["contract"]),None)
            if not row: raise RuntimeError("Contract quote unavailable")
            bid=float(row.get("bid_price") or 0)
            ask=float(row.get("ask_price") or 0)
            mark=float(row.get("mark_price") or 0)
            underlying=float(payload.get("underlying") or 0)
            if mark<=0: raise RuntimeError("Premium unavailable")
            source=str(payload.get("source") or p.get("source") or "").lower()
            quote_usd=any(x in source for x in ("paradex","bybit","binance"))
            premium=(bid if bid>0 else mark) if quote_usd else (bid if bid>0 else mark)*underlying
            if premium<=0: raise RuntimeError("USD premium conversion unavailable")
            entry=float(p["entry"]); peak=max(float(p.get("peak",entry)),premium)
            stage=int(p.get("stage",0)); p["peak"]=peak
            sl=float(p["sl"]); tp1=float(p["tp1"]); tp2=float(p["tp2"]); tp3=float(p["tp3"])
            msg=None
            rocket_msg=None
            move_pct=(premium/entry-1.0)*100.0 if entry>0 else 0.0
            jackpot = p.get("signal_type") == "JACKPOT"
            tp1_label = "2x premium" if jackpot else "+25%"
            tp2_label = "5x premium" if jackpot else "+50%"
            tp3_change_pct = ((tp3 / entry) - 1.0) * 100.0 if entry > 0 else 0.0
            tp3_label = "10x premium" if jackpot else f"+{tp3_change_pct:.0f}%"
            if premium<=sl:
                msg=f"📣 OPTION RESULT — 🛑 INITIAL SL HIT — EXIT\n{coin} {p['contract']}\nEntry: {entry:.4f} USD\nCurrent Premium: {premium:.4f} USD\nSL: {sl:.4f} USD\nResult: {move_pct:+.2f}% premium change\nSignal closed for tracking. No order was placed."
                p["status"]="STOPPED"
                p["risk_stage"]=3
            elif premium>=tp3:
                msg=f"📣 OPTION RESULT — 🎯 FINAL TARGET HIT ({tp3_label})\n{coin} {p['contract']}\nEntry: {entry:.4f} USD\nCurrent Premium: {premium:.4f} USD\nFinal target: {tp3:.4f} USD\nResult: {move_pct:+.2f}% premium change\nProfit-book alert. No order was placed."
                p["status"]="TARGET_HIT"
            elif stage<2 and premium>=tp2:
                p["stage"]=2
                trail=max(entry,tp1,peak*0.85)
                p["trail"]=trail
                msg=f"📣 OPTION RESULT — 🎯 TP2 HIT ({tp2_label})\n{coin} {p['contract']}\nEntry: {entry:.4f} USD\nCurrent Premium: {premium:.4f} USD\nTP1: {tp1:.4f} | TP2: {tp2:.4f} USD\nResult: {move_pct:+.2f}% premium change\nTrailing SL activated: {trail:.4f} USD\nSignal only — no order placed."
            elif stage<1 and premium>=tp1:
                p["stage"]=1
                p["trail"]=entry
                msg=f"📣 OPTION RESULT — 🎯 TP1 HIT ({tp1_label})\n{coin} {p['contract']}\nEntry: {entry:.4f} USD\nCurrent Premium: {premium:.4f} USD\nTP1: {tp1:.4f} USD\nResult: {move_pct:+.2f}% premium change\nBreak-even SL: {entry:.4f} USD\nSignal only — no order placed."
            elif stage>=1:
                trail=max(float(p.get("trail",entry)), entry if stage==1 else max(entry,tp1,peak*0.85))
                p["trail"]=trail
                if premium<=trail:
                    msg=f"📣 OPTION RESULT — 🔒 TRAILING SL HIT — EXIT\n{coin} {p['contract']}\nEntry: {entry:.4f} USD\nPeak Premium: {peak:.4f} USD\nCurrent Premium: {premium:.4f} USD\nTrailing SL: {trail:.4f} USD\nResult: {move_pct:+.2f}% premium change\nSignal closed for tracking. No order was placed."
                    p["status"]="TRAIL_STOPPED"
            # Early risk warnings are advisory only; the original 25% hard SL is unchanged.
            # Stage 1 warns at -18%; stage 2 warns at -22%; stage 3 is the hard SL.
            if p.get("status")=="ACTIVE" and msg is None:
                risk_stage=int(p.get("risk_stage",0))
                if premium<=entry*0.78 and risk_stage<2:
                    msg=f"⚠️ OPTION RISK WARNING — -22% / HIGH RISK\n{coin} {p['contract']}\nEntry: {entry:.4f} USD\nCurrent Bid/Mark: {premium:.4f} USD\nHard SL remains: {sl:.4f} USD\nLoss from entry: {move_pct:+.2f}%\nPlease review the position manually. No order was placed."
                    p["risk_stage"]=2
                elif premium<=entry*0.82 and risk_stage<1:
                    msg=f"⚠️ OPTION EARLY WARNING — -18%\n{coin} {p['contract']}\nEntry: {entry:.4f} USD\nCurrent Bid/Mark: {premium:.4f} USD\nHard SL remains: {sl:.4f} USD\nLoss from entry: {move_pct:+.2f}%\nPlease review the position manually. No order was placed."
                    p["risk_stage"]=1
            # Keep the +75% rocket alert only when this record has a later final target.
            # For current +75% TP3 records, the final-target alert already covers this milestone.
            if (not p.get("rocket_alerted",False)
                    and tp3_change_pct > 75.0 + 1e-6
                    and premium >= entry * 1.75):
                rocket_msg=f"🚀 ROCKET ALERT — +75% PREMIUM MOVE\n{coin} {p['contract']}\nOriginal Entry: {entry:.4f} USD\nCurrent Premium: {premium:.4f} USD\nMove from entry: {move_pct:+.2f}%\nSame signal — no new entry or order. Check existing targets / manage manually."
            p["last_premium"]=premium
            p["last_checked"]=datetime.now(timezone.utc).isoformat()
            changed=True
            if msg:
                try:
                    telegram(msg)
                    print(coin+" "+p["contract"]+": "+p["status"]+" / stage "+str(p.get("stage",0)))
                except Exception as notify_error:
                    p["pending_telegram"]=msg
                    print("WARN alert queued for retry "+coin+" "+p["contract"]+": "+str(notify_error))
            if rocket_msg:
                telegram(rocket_msg)
                p["rocket_alerted"]=True
                print(coin+" "+p["contract"]+": ROCKET alert sent")
        except Exception as e:
            print("WARN "+coin+" "+str(p.get("contract"))+": "+str(e))
            # A failed quote check is itself important: never imply that SL monitoring is healthy.
            now=datetime.now(timezone.utc)
            last_warning=p.get("last_data_warning_at")
            try:
                last_dt=datetime.fromisoformat(str(last_warning).replace("Z","+00:00")) if last_warning else None
            except ValueError:
                last_dt=None
            if last_dt is None or (now-last_dt).total_seconds()>=900:
                warning=("🚨 OPTION MONITOR DATA DELAY\n"+coin+" "+str(p.get("contract"))+
                         "\nLive premium quote unavailable; automatic SL tracking is temporarily unverified."+
                         "\nLast known premium: "+str(p.get("last_premium","—"))+
                         "\nReason: "+str(e)[:220]+
                         "\nPlease check the contract manually. No order was placed.")
                try:
                    telegram(warning)
                except Exception as notify_error:
                    p["pending_telegram"]=warning
                    print("WARN Telegram data-delay alert queued for retry: "+str(notify_error))
                p["last_data_warning_at"]=now.isoformat()
                changed=True
    if changed:
        os.makedirs("state",exist_ok=True)
        with open(PATH,"w",encoding="utf-8") as f:
            json.dump(positions,f,ensure_ascii=False,indent=2); f.write("\n")

if __name__=="__main__": main()

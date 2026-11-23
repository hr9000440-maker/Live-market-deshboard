#!/usr/bin/env python3
import json, os, urllib.request, sys, datetime

CHECKS = [
    ("Render health", "https://live-market-deshboard.onrender.com/health", "json"),
    ("Render dashboard", "https://live-market-deshboard.onrender.com/", "html"),
    ("GitHub Pages", "https://hr9000440-maker.github.io/Live-market-deshboard/", "html"),
]
issues=[]
for label,url,kind in CHECKS:
    try:
        req=urllib.request.Request(url,headers={"User-Agent":"dashboard-health-monitor/1.0"})
        with urllib.request.urlopen(req,timeout=30) as response:
            body=response.read().decode("utf-8","replace")
            if response.status != 200: raise RuntimeError(f"HTTP {response.status}")
        if kind=="json":
            payload=json.loads(body)
            if payload.get("status")!="ok": raise RuntimeError("health response not OK")
        else:
            if "LIVE MARKET DASHBOARD" not in body.upper() and "LIVE-MARKET-DASHBOARD" not in body.upper():
                raise RuntimeError("dashboard title/content missing")
        print(f"PASS {label}: HTTP 200")
    except Exception as exc:
        issue=f"{label}: {exc}"
        issues.append(issue)
        print("FAIL "+issue)

if issues:
    token=os.getenv("TELEGRAM_BOT_TOKEN","")
    chat=os.getenv("TELEGRAM_CHAT_ID","")
    msg="⚠️ DASHBOARD HEALTH CHECK\n"+ "\\n".join(issues)+"\nChecked: "+datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if token and chat:
        try:
            data=json.dumps({"chat_id":chat,"text":msg}).encode()
            req=urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",data=data,headers={"Content-Type":"application/json"})
            with urllib.request.urlopen(req,timeout=20) as response:
                result=json.loads(response.read())
            if not result.get("ok"): print("Telegram alert failed: "+str(result.get("description")))
        except Exception as exc: print("Telegram alert error: "+str(exc))
    sys.exit(1)
print("All dashboard health checks passed.")

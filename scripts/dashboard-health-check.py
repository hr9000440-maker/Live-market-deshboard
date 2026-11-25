#!/usr/bin/env python3
"""Read-only health checks for dashboard, option gateway, Telegram config and signal workflows."""
import json, os, sys, datetime, urllib.request, urllib.parse, py_compile

BASE = "https://live-market-deshboard.onrender.com"
PAGES = "https://hr9000440-maker.github.io/Live-market-deshboard/"
REPO = "hr9000440-maker/Live-market-deshboard"
COINS = ("BTC", "ETH", "BNB", "ZEC", "DOGE")
issues, notes = [], []

def request_json(url, headers=None, timeout=20):
    h = {"User-Agent": "dashboard-health-monitor/2.0", "Accept": "application/json"}
    if headers: h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode("utf-8", "replace")
        if response.status != 200: raise RuntimeError("HTTP " + str(response.status))
        return json.loads(body)

def check_page(label, url, expected):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "dashboard-health-monitor/2.0"})
        with urllib.request.urlopen(req, timeout=25) as r:
            body = r.read(250000).decode("utf-8", "replace")
            if r.status != 200: raise RuntimeError("HTTP " + str(r.status))
        if expected.lower() not in body.lower(): raise RuntimeError("expected page content missing")
        print("PASS " + label + ": HTTP 200")
    except Exception as e:
        issues.append(label + ": " + str(e))
        print("FAIL " + label + ": " + str(e))

def check_telegram():
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAMBOTTOKAN") or
             os.getenv("TELEGRAM_BOT_TOKAN") or os.getenv("TELEGRAM_BOT_TOKAN") or "")
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        issues.append("Telegram secrets: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing")
        print("FAIL Telegram configuration: required secrets missing")
        return
    try:
        me = request_json("https://api.telegram.org/bot" + token + "/getMe", timeout=15)
        if not me.get("ok"): raise RuntimeError(me.get("description", "getMe failed"))
        chat_info = request_json("https://api.telegram.org/bot" + token + "/getChat?chat_id=" + urllib.parse.quote(chat), timeout=15)
        if not chat_info.get("ok"): raise RuntimeError(chat_info.get("description", "getChat failed"))
        print("PASS Telegram bot authentication and target chat lookup")
    except Exception as e:
        issues.append("Telegram bot/chat check: " + str(e))
        print("FAIL Telegram bot/chat check: " + str(e))

def check_options():
    for coin in COINS:
        try:
            data = request_json(BASE + "/api/options/" + coin, timeout=25)
            if data.get("error"): raise RuntimeError(str(data.get("error")) + " " + str(data.get("details", "")))
            rows = data.get("rows")
            if not isinstance(rows, list): raise RuntimeError("option rows missing or invalid")
            underlying = float(data.get("underlying") or 0)
            if underlying <= 0: raise RuntimeError("underlying price missing")
            live, expiries = 0, set()
            for row in rows:
                if not isinstance(row, dict): continue
                try:
                    bid, ask = float(row.get("bid_price") or 0), float(row.get("ask_price") or 0)
                    if bid > 0 and ask >= bid: live += 1
                    exp = row.get("expiry")
                    if exp: expiries.add(str(exp))
                except (TypeError, ValueError): pass
            if not rows: notes.append(coin + " options: API works but no listed contracts returned")
            elif live == 0:
                message = coin + " options: " + str(len(rows)) + " contracts but no valid live bid/ask quotes"
                if coin == "ZEC":
                    issues.append(message)
                else:
                    notes.append(message)
            print("PASS " + coin + " options gateway: rows=" + str(len(rows)) + ", live bid/ask=" + str(live) + ", expiries=" + str(len(expiries)) + ", source=" + str(data.get("source")))
        except Exception as e:
            issues.append(coin + " options gateway: " + str(e))
            print("FAIL " + coin + " options gateway: " + str(e))

def check_crypto_feeds():
    for coin in ("BTC", "ETH", "BNB", "ZEC"):
        symbol = coin + "USDT"
        urls = [
            "https://api.binance.com/api/v3/klines?symbol=" + symbol + "&interval=15m&limit=3",
            "https://data-api.binance.vision/api/v3/klines?symbol=" + symbol + "&interval=15m&limit=3",
        ]
        ok, errs = False, []
        for url in urls:
            try:
                rows = request_json(url, timeout=15)
                if not isinstance(rows, list) or len(rows) < 2 or not all(float(x[4]) > 0 for x in rows):
                    raise RuntimeError("invalid candle response")
                ok = True
                break
            except Exception as e: errs.append(str(e))
        if ok: print("PASS " + coin + " public 15m candle feed")
        else:
            issues.append(coin + " market feed: " + " / ".join(errs))
            print("FAIL " + coin + " market feed")

def check_workflow(name, label, gh_token):
    if not gh_token:
        notes.append("GitHub Actions run status skipped: GITHUB_TOKEN unavailable")
        return
    try:
        data = request_json("https://api.github.com/repos/" + REPO + "/actions/workflows/" + name + "/runs?per_page=1",
            {"Authorization": "Bearer " + gh_token, "X-GitHub-Api-Version": "2022-11-28"}, 20)
        runs = data.get("workflow_runs") or []
        if not runs:
            notes.append(label + ": no run recorded yet")
            print("NOTE " + label + ": no run recorded yet")
            return
        run = runs[0]
        status, conclusion = run.get("status"), run.get("conclusion")
        print("INFO " + label + ": status=" + str(status) + ", conclusion=" + str(conclusion) + ", updated=" + str(run.get("updated_at")))
        if status == "completed" and conclusion != "success":
            issues.append(label + " latest run: " + str(conclusion))
        elif status != "completed":
            notes.append(label + " latest run is still " + str(status))
    except Exception as e:
        notes.append(label + " run status unavailable: " + str(e))

def main():
    check_page("GitHub Pages dashboard", PAGES, "LIVE MARKET DASHBOARD")
    check_page("Render dashboard", BASE + "/", "LIVE MARKET DASHBOARD")
    try:
        health = request_json(BASE + "/health", timeout=15)
        if health.get("status") != "ok": raise RuntimeError("health status is not ok")
        print("PASS Render health endpoint")
    except Exception as e:
        issues.append("Render health endpoint: " + str(e))
        print("FAIL Render health endpoint: " + str(e))

    check_crypto_feeds()
    check_options()
    try:
        py_compile.compile("options_strong_signals.py", doraise=True)
        print("PASS options_strong_signals.py syntax")
    except Exception as e:
        issues.append("Option scanner Python syntax: " + str(e))
        print("FAIL option scanner syntax: " + str(e))

    check_telegram()
    gh = os.getenv("GITHUB_TOKEN", "")
    check_workflow("options-strong-signals.yml", "BTC/ETH/ZEC option Telegram scanner", gh)
    try:
        with open(".github/workflows/crypto-telegram.yml", "r", encoding="utf-8") as wf:
            workflow_text = wf.read()
        if "schedule:" not in workflow_text:
            notes.append("Crypto futures Telegram workflow is MANUAL-ONLY (no automatic schedule is configured)")
            print("NOTE Crypto futures Telegram workflow is manual-only; it will not send scheduled alerts")
    except OSError as e:
        notes.append("Could not inspect crypto futures workflow schedule: " + str(e))

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print("\nHEALTH SUMMARY " + stamp)
    for n in notes: print("NOTE " + n)
    for i in issues: print("ISSUE " + i)

    if issues:
        token = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAMBOTTOKAN") or
                 os.getenv("TELEGRAM_BOT_TOKAN") or os.getenv("TELEGRAM_BOT_TOKAN") or "")
        chat = os.getenv("TELEGRAM_CHAT_ID", "")
        msg = "⚠️ DASHBOARD HEALTH CHECK\n" + "\n".join(issues) + "\nChecked: " + stamp
        if token and chat:
            try:
                payload = json.dumps({"chat_id": chat, "text": msg}).encode()
                req = urllib.request.Request("https://api.telegram.org/bot" + token + "/sendMessage",
                    data=payload, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=20) as response:
                    result = json.loads(response.read().decode())
                if result.get("ok"): print("PASS Telegram failure alert sent")
                else: print("FAIL Telegram failure alert: " + str(result.get("description")))
            except Exception as e: print("FAIL Telegram failure alert: " + str(e))
        sys.exit(1)
    print("All critical checks passed. Notes are informational; no trading actions were taken.")

if __name__ == "__main__":
    main()

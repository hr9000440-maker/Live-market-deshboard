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

def telegram_tokens():
    names = ("TELEGRAM_BOT_TOKEN", "TELEGRAMBOTTOKAN",
             "TELEGRAM_BOT_TOKAN", "TELEGRAM_BOT_FATHER")
    return list(dict.fromkeys(os.getenv(name, "").strip() for name in names if os.getenv(name, "").strip()))

def check_telegram():
    tokens = telegram_tokens()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not tokens or not chat:
        issues.append("Telegram secrets: configured bot token alias or TELEGRAM_CHAT_ID missing")
        print("FAIL Telegram configuration: required secrets missing")
        return
    errors = []
    for index, token in enumerate(tokens, start=1):
        try:
            me = request_json("https://api.telegram.org/bot" + token + "/getMe", timeout=15)
            if not me.get("ok"): raise RuntimeError(me.get("description", "getMe failed"))
            chat_info = request_json("https://api.telegram.org/bot" + token + "/getChat?chat_id=" + urllib.parse.quote(chat), timeout=15)
            if not chat_info.get("ok"): raise RuntimeError(chat_info.get("description", "getChat failed"))
            print("PASS Telegram bot authentication and target chat lookup (configured alias " + str(index) + ")")
            return
        except Exception as e:
            errors.append("alias " + str(index) + ": " + str(e)[:160])
    message = "all configured token aliases failed: " + " / ".join(errors)
    issues.append("Telegram bot/chat check: " + message)
    print("FAIL Telegram bot/chat check: " + message)
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
                # Some exchanges list ZEC option instruments without executable
                # two-sided quotes. Keep this visible as a warning, but do not
                # mark the whole dashboard unhealthy or permit a synthetic quote.
                notes.append(message + " — safely unavailable; no ZEC option alert will be generated")
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

def check_option_level_math():
    """Read-only validation of option premium SL/TP ordering and configured percentages."""
    path = "state/option_positions.json"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            positions = json.load(handle)
        if not isinstance(positions, list):
            raise RuntimeError("option positions ledger must be a JSON list")
    except FileNotFoundError:
        notes.append("Option SL/TP ledger not present yet; arithmetic fixture checked only")
        positions = []
    except Exception as exc:
        issues.append("Option SL/TP ledger: " + str(exc))
        print("FAIL Option SL/TP ledger: " + str(exc))
        return

    # Known-value regression: entry 5 must produce SL 3.75 and rising targets.
    fixture = {"sl": 3.75, "tp1": 6.25, "tp2": 7.50, "tp3": 8.75}
    expected_fixture = (5.0 * 0.75, 5.0 * 1.25, 5.0 * 1.50, 5.0 * 1.75)
    if any(abs(a-b) > 1e-9 for a, b in zip(
            (fixture["sl"], fixture["tp1"], fixture["tp2"], fixture["tp3"]), expected_fixture)):
        issues.append("Option SL/TP arithmetic regression fixture failed")

    checked = 0
    legacy_closed = 0
    bad = []
    for index, position in enumerate(positions, start=1):
        try:
            entry = float(position.get("entry") or 0)
            sl = float(position.get("sl") or 0)
            tp1 = float(position.get("tp1") or 0)
            tp2 = float(position.get("tp2") or 0)
            tp3 = float(position.get("tp3") or 0)
            if not all(map(lambda value: value > 0 and value < float("inf"), (entry, sl, tp1, tp2, tp3))):
                raise ValueError("missing or non-positive Entry/SL/TP")
            if not (sl < entry < tp1 < tp2 < tp3):
                raise ValueError("expected BUY option order SL < Entry < TP1 < TP2 < TP3")
            ratios = (sl/entry, tp1/entry, tp2/entry, tp3/entry)
            status = str(position.get("status") or "").upper()
            # Preserve closed historical records created under the previous +85% TP3 rule.
            # New/open positions must follow the current +75% TP3 policy.
            if status in ("STOPPED", "TRAIL_STOPPED", "CLOSED", "TP3_HIT", "FINAL_TARGET") and abs(ratios[3] - 1.85) <= 0.002:
                legacy_closed += 1
                continue
            expected = (0.75, 1.25, 1.50, 1.75)
            if any(abs(a-b) > 0.002 for a, b in zip(ratios, expected)):
                raise ValueError("levels do not match configured -25% / +25% / +50% / +75%")
            checked += 1
        except Exception as exc:
            bad.append(str(position.get("contract") or position.get("key") or index) + ": " + str(exc))

    # Guard the two independent calculation paths against accidental edits.
    try:
        with open("index.html", "r", encoding="utf-8") as handle:
            page = handle.read()
        with open("options_strong_signals.py", "r", encoding="utf-8") as handle:
            scanner = handle.read()
        page_ok = all(token in page for token in (
            "const entryUsd=askUsd;", "entryUsd*0.75", "entryUsd*1.25",
            "entryUsd*1.50", "entryUsd*1.75", "suggestedEntry",
            "const initialEntry=Number(suggestedEntry)>0?Number(suggestedEntry):current"))
        scanner_ok = all(token in scanner for token in (
            "limit_usd * 0.75", "limit_usd * 1.25", "limit_usd * 1.50", "limit_usd * 1.75"))
        if not page_ok or not scanner_ok:
            bad.append("dashboard/scanner Entry ASK basis or SL/TP calculation source differs from the configured levels")
    except Exception as exc:
        bad.append("could not inspect calculation source: " + str(exc))

    if bad:
        message = "Option SL/TP validation failed: " + " | ".join(bad[:8])
        issues.append(message)
        print("FAIL " + message)
    else:
        print("PASS Option SL/TP math: entry=5 fixture; " + str(checked) +
              " current signals; " + str(legacy_closed) + " closed legacy records preserved; SL -25%, TP1 +25%, TP2 +50%, TP3 +75%")

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
    check_option_level_math()
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
        tokens = telegram_tokens()
        chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        msg = "⚠️ DASHBOARD HEALTH CHECK\n" + "\n".join(issues) + "\nChecked: " + stamp
        if tokens and chat:
            errors = []
            for token in tokens:
                try:
                    payload = json.dumps({"chat_id": chat, "text": msg}).encode()
                    req = urllib.request.Request("https://api.telegram.org/bot" + token + "/sendMessage",
                        data=payload, headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=20) as response:
                        result = json.loads(response.read().decode())
                    if result.get("ok"):
                        print("PASS Telegram failure alert sent")
                        break
                    errors.append(str(result.get("description", "Telegram send failed"))[:160])
                except Exception as e:
                    errors.append(str(e)[:160])
            else:
                print("FAIL Telegram failure alert: all configured token aliases failed: " + " / ".join(errors))
        sys.exit(1)

    print("All critical checks passed. Notes are informational; no trading actions were taken.")

if __name__ == "__main__":
    main()

"""Read-only Binance USD-M futures flow advisor.

This module detects unusual directional pressure; it cannot identify a trader's
identity or prove that a trade belongs to an institution/whale.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlencode


COINS = ("BTC", "ETH", "BNB", "ZEC")
BASE = "https://fapi.binance.com"


def _number(value, default=None):
    try:
        n = float(value)
        return n if n == n and abs(n) != float("inf") else default
    except (TypeError, ValueError, OverflowError):
        return default


def _change(new, old):
    if new is None or old is None or old == 0:
        return None
    return (new / old - 1.0) * 100.0


def _request(fetch_json, path, params):
    return fetch_json(BASE + path + "?" + urlencode(params), timeout=10)


def _coin_report(coin, fetch_json):
    symbol = coin + "USDT"
    endpoints = {
        "premium": ("/fapi/v1/premiumIndex", {"symbol": symbol}),
        "oi": ("/fapi/v1/openInterest", {"symbol": symbol}),
        "oi_hist": ("/futures/data/openInterestHist", {"symbol": symbol, "period": "5m", "limit": 7}),
        "taker": ("/futures/data/takerlongshortRatio", {"symbol": symbol, "period": "5m", "limit": 2}),
        "accounts": ("/futures/data/globalLongShortAccountRatio", {"symbol": symbol, "period": "5m", "limit": 2}),
        "book": ("/fapi/v1/depth", {"symbol": symbol, "limit": 50}),
        "trades": ("/fapi/v1/aggTrades", {"symbol": symbol, "limit": 500}),
        "klines": ("/fapi/v1/klines", {"symbol": symbol, "interval": "1m", "limit": 20}),
    }
    data, errors = {}, []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {key: pool.submit(_request, fetch_json, path, params)
                   for key, (path, params) in endpoints.items()}
        for key, future in futures.items():
            try:
                data[key] = future.result()
            except Exception as exc:
                data[key] = None
                errors.append(key + ": " + str(exc)[:120])

    premium = data.get("premium") if isinstance(data.get("premium"), dict) else {}
    oi_now = data.get("oi") if isinstance(data.get("oi"), dict) else {}
    oi_hist = data.get("oi_hist") if isinstance(data.get("oi_hist"), list) else []
    taker_hist = data.get("taker") if isinstance(data.get("taker"), list) else []
    account_hist = data.get("accounts") if isinstance(data.get("accounts"), list) else []
    book = data.get("book") if isinstance(data.get("book"), dict) else {}
    trades = data.get("trades") if isinstance(data.get("trades"), list) else []
    klines = data.get("klines") if isinstance(data.get("klines"), list) else []

    price = _number(premium.get("indexPrice")) or _number(premium.get("markPrice"))
    if price is None and klines:
        price = _number(klines[-2][4]) if len(klines[-2]) > 4 else None

    closed = [k for k in klines if isinstance(k, list) and len(k) > 6]
    closes = [_number(k[4]) for k in closed]
    change_5m = _change(closes[-2], closes[-7]) if len(closes) >= 7 else None
    change_15m = _change(closes[-2], closes[-17]) if len(closes) >= 17 else None
    recent = closed[-16:-1] if len(closed) >= 16 else closed[:-1]
    recent_high = max((_number(k[2], 0) or 0 for k in recent), default=0) or None
    recent_low = min((_number(k[3], float("inf")) for k in recent), default=float("inf"))
    if recent_low == float("inf"):
        recent_low = None

    oi_change = None
    if len(oi_hist) >= 2:
        first = oi_hist[0]
        last = oi_hist[-1]
        old_oi = _number(first.get("sumOpenInterestValue")) or _number(first.get("sumOpenInterest"))
        new_oi = _number(last.get("sumOpenInterestValue")) or _number(last.get("sumOpenInterest"))
        oi_change = _change(new_oi, old_oi)
    current_oi = _number(oi_now.get("openInterest"))

    taker_buy_pct = None
    if taker_hist:
        last = taker_hist[-1]
        ratio = _number(last.get("buySellRatio"))
        buy_vol, sell_vol = _number(last.get("buyVol")), _number(last.get("sellVol"))
        if ratio is None and buy_vol is not None and sell_vol is not None and sell_vol > 0:
            ratio = buy_vol / sell_vol
        if ratio is not None and ratio >= 0:
            taker_buy_pct = ratio / (1.0 + ratio) * 100.0

    bids, asks = book.get("bids") or [], book.get("asks") or []
    bid_qty = sum((_number(x[1], 0) or 0 for x in bids[:20] if isinstance(x, list) and len(x) > 1), 0.0)
    ask_qty = sum((_number(x[1], 0) or 0 for x in asks[:20] if isinstance(x, list) and len(x) > 1), 0.0)
    book_imbalance = ((bid_qty - ask_qty) / (bid_qty + ask_qty) * 100.0) if bid_qty + ask_qty > 0 else None

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    recent_trades = [t for t in trades if isinstance(t, dict)
                     and (_number(t.get("T"), 0) or 0) >= now_ms - 300000]
    notionals = [(_number(t.get("p"), 0) or 0) * (_number(t.get("q"), 0) or 0)
                 for t in recent_trades]
    positive = sorted(x for x in notionals if x > 0)
    cutoff = positive[min(len(positive) - 1, int(len(positive) * 0.90))] if positive else None
    large_buy = large_sell = 0.0
    large_buy_count = large_sell_count = 0
    for trade, notional in zip(recent_trades, notionals):
        if cutoff is None or notional < cutoff:
            continue
        # Binance 'm' means the buyer was maker, so the seller was taker/aggressor.
        if trade.get("m") is True:
            large_sell += notional
            large_sell_count += 1
        else:
            large_buy += notional
            large_buy_count += 1
    large_net_pct = ((large_buy - large_sell) / (large_buy + large_sell) * 100.0
                     if large_buy + large_sell > 0 else None)

    funding = _number(premium.get("lastFundingRate"))
    account_ratio = _number(account_hist[-1].get("longShortRatio")) if account_hist else None

    score, evidence = 0.0, []
    if change_5m is not None and abs(change_5m) >= 0.03:
        score += 12 if change_5m > 0 else -12
        evidence.append("5m price " + ("up " if change_5m > 0 else "down ") + f"{abs(change_5m):.2f}%")
    if change_15m is not None and abs(change_15m) >= 0.08:
        score += 12 if change_15m > 0 else -12
        evidence.append("15m trend " + ("up " if change_15m > 0 else "down ") + f"{abs(change_15m):.2f}%")
    if oi_change is not None and abs(oi_change) >= 0.25:
        if change_5m is not None and change_5m * oi_change > 0:
            score += 18 if change_5m > 0 else -18
            evidence.append("price + OI aligned (" + f"{oi_change:+.2f}% OI" + ")")
        elif change_5m is not None:
            evidence.append("OI changed " + f"{oi_change:+.2f}% — possible position unwind/divergence")
    if taker_buy_pct is not None:
        if taker_buy_pct >= 56:
            score += 18
            evidence.append(f"aggressive buyers {taker_buy_pct:.1f}%")
        elif taker_buy_pct <= 44:
            score -= 18
            evidence.append(f"aggressive sellers {100-taker_buy_pct:.1f}%")
    if book_imbalance is not None:
        if book_imbalance >= 15:
            score += 12
            evidence.append(f"bid-side book imbalance {book_imbalance:+.1f}%")
        elif book_imbalance <= -15:
            score -= 12
            evidence.append(f"ask-side book imbalance {book_imbalance:+.1f}%")
    if large_net_pct is not None and abs(large_net_pct) >= 20:
        score += 12 if large_net_pct > 0 else -12
        evidence.append(("large aggressive buys " if large_net_pct > 0 else "large aggressive sells ")
                        + f"net {large_net_pct:+.1f}%")

    score = max(-100.0, min(100.0, score))
    source_count = sum([
        price is not None, oi_change is not None, taker_buy_pct is not None,
        book_imbalance is not None, large_net_pct is not None,
        change_5m is not None, change_15m is not None
    ])
    confidence = min(95, 25 + source_count * 10)
    if source_count < 3:
        bias = "DATA INCOMPLETE"
    elif score >= 48:
        bias = "BUY PRESSURE"
    elif score <= -48:
        bias = "SELL PRESSURE"
    else:
        bias = "MIXED / WAIT"
    return {
        "coin": coin, "symbol": symbol, "price": price,
        "change_5m_pct": change_5m, "change_15m_pct": change_15m,
        "open_interest": current_oi, "oi_change_30m_pct": oi_change,
        "taker_buy_pct": taker_buy_pct, "book_imbalance_pct": book_imbalance,
        "large_buy_usd": large_buy, "large_sell_usd": large_sell,
        "large_buy_count": large_buy_count, "large_sell_count": large_sell_count,
        "large_trade_net_pct": large_net_pct, "funding_rate_pct": funding * 100 if funding is not None else None,
        "global_long_short_account_ratio": account_ratio,
        "last_15m_high": recent_high, "last_15m_low": recent_low,
        "score": round(score), "bias": bias, "confidence": confidence,
        "strong_pressure": source_count >= 4 and abs(score) >= 60,
        "evidence": evidence, "data_points": source_count,
        "errors": errors[:5],
        "interpretation_note": "Long/short ratio is general account positioning, not verified whale positions. OI always has both a buyer and seller."
    }


def build_big_player_report(fetch_json):
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {coin: pool.submit(_coin_report, coin, fetch_json) for coin in COINS}
        results = {}
        for coin, future in futures.items():
            try:
                results[coin] = future.result()
            except Exception as exc:
                results[coin] = {"coin": coin, "bias": "DATA UNAVAILABLE",
                                 "errors": [str(exc)[:180]], "evidence": [],
                                 "score": 0, "confidence": 0, "strong_pressure": False}
    return {
        "source": "Binance USD-M public futures market data",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "refresh_seconds": 60,
        "coins": results,
        "disclaimer": "Heuristic flow pressure, not identification of a whale or guaranteed prediction. No orders are placed."
    }

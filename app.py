import os
import threading
import time
from datetime import datetime, timezone
from flask import Flask, jsonify, send_from_directory, make_response

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=None)

# CoinSwitch public Futures V2 market-data stream. This connection is read-only:
# no account actions, order endpoints, API secrets, or trading permissions are used.
COINS = ("BTC", "ETH", "BNB", "ZEC")
SYMBOLS = {coin + "USDT": coin for coin in COINS}
NAMESPACE = "/exchange_2"
KLINE_EVENT = "FETCH_CANDLESTICK_CS_PRO"
feed_lock = threading.RLock()
feed = {
    "connection": "starting",
    "connected_at": None,
    "last_message_at": None,
    "last_error": None,
    "coins": {coin: {"symbol": coin + "USDT", "candles": []} for coin in COINS},
}
_socket_thread_started = False


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _start_coinswitch_socket():
    global _socket_thread_started
    with feed_lock:
        if _socket_thread_started:
            return
        _socket_thread_started = True

    def runner():
        try:
            import socketio
        except Exception as exc:
            with feed_lock:
                feed["connection"] = "dependency_missing"
                feed["last_error"] = str(exc)[:240]
            return

        sio = socketio.Client(
            logger=False,
            engineio_logger=False,
            reconnection=True,
            reconnection_attempts=0,
            reconnection_delay=1,
            reconnection_delay_max=30,
        )

        @sio.event(namespace=NAMESPACE)
        def connect():
            with feed_lock:
                feed["connection"] = "connected"
                feed["connected_at"] = _utc_now()
                feed["last_error"] = None
            for coin in COINS:
                sio.emit(
                    KLINE_EVENT,
                    {"event": "subscribe", "pair": coin + "USDT_15"},
                    namespace=NAMESPACE,
                )

        @sio.event(namespace=NAMESPACE)
        def disconnect():
            with feed_lock:
                feed["connection"] = "disconnected"

        @sio.on(KLINE_EVENT, namespace=NAMESPACE)
        def on_kline(payload):
            if not isinstance(payload, dict):
                return
            symbol = str(payload.get("s", "")).upper()
            coin = SYMBOLS.get(symbol)
            if not coin:
                return
            try:
                candle = {
                    "symbol": symbol,
                    "interval": 15,
                    "open": float(payload["o"]),
                    "high": float(payload["h"]),
                    "low": float(payload["l"]),
                    "close": float(payload["c"]),
                    "volume": float(payload["v"]),
                    "quote_volume": float(payload.get("q", 0) or 0),
                    "start_time": int(payload["t"]),
                    "close_time": int(payload["T"]),
                    "closed": bool(payload.get("x", False)),
                    "exchange_update_time": int(payload.get("ts", 0) or 0),
                }
                if not all(candle[k] == candle[k] for k in ("open", "high", "low", "close", "volume")):
                    return
                if candle["close"] <= 0 or candle["high"] < candle["low"] or candle["volume"] < 0:
                    return
            except (KeyError, TypeError, ValueError, OverflowError):
                return

            with feed_lock:
                rows = feed["coins"][coin]["candles"]
                existing = next((i for i, row in enumerate(rows) if row["start_time"] == candle["start_time"]), None)
                if existing is None:
                    rows.append(candle)
                else:
                    rows[existing] = candle
                rows.sort(key=lambda row: row["start_time"])
                feed["coins"][coin]["candles"] = rows[-200:]
                feed["last_message_at"] = _utc_now()

        while True:
            try:
                with feed_lock:
                    feed["connection"] = "connecting"
                sio.connect(
                    "https://ws.coinswitch.co",
                    namespaces=[NAMESPACE],
                    transports=["websocket"],
                    socketio_path="/pro/realtime-rates-socket/futures/exchange_2",
                    wait=True,
                    wait_timeout=25,
                )
                sio.wait()
            except Exception as exc:
                with feed_lock:
                    feed["connection"] = "reconnecting"
                    feed["last_error"] = str(exc)[:240]
                time.sleep(10)

    threading.Thread(target=runner, name="coinswitch-futures-feed", daemon=True).start()


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response



def _fetch_json(url, timeout=12):
    import json
    from urllib.request import Request, urlopen
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "LiveMarketDashboard/1.0"})
    with urlopen(req, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError("upstream HTTP " + str(response.status))
        return json.loads(response.read().decode("utf-8"))


@app.get("/api/options/<coin>")
def crypto_options(coin):
    """Read-only public option-chain proxy; keeps browser CORS out of the data path."""
    coin = coin.upper()
    if coin not in ("BTC", "ETH", "ZEC", "DOGE"):
        return jsonify({"error": "unsupported coin", "coin": coin}), 400
    errors = []
    rows = []
    underlying = None
    source = None
    if coin == "DOGE":
        # Bybit public DOGE options market data. Read-only; no account key or order API.
        # Discover DOGE contracts robustly, with a fallback to the documented All listing.
        try:
            base_payload = _fetch_json("https://api.bybit.com/v5/market/option-base-coins?underlyingType=0")
            base_rows = (base_payload.get("result") or {}).get("list") or []
            doge_base = next((x for x in base_rows if str(x.get("baseCoin", "")).upper() == "DOGE"), None)
            if not doge_base or int(doge_base.get("hasSymbol", 0) or 0) != 1:
                raise RuntimeError("Bybit reports no currently tradable DOGE option symbols")

            instruments_payload = _fetch_json("https://api.bybit.com/v5/market/instruments-info?category=option&baseCoin=DOGE&status=Trading&limit=1000")
            if instruments_payload.get("retCode") != 0:
                raise RuntimeError("Bybit instruments API: " + str(instruments_payload.get("retMsg", "unknown error")))
            instruments = (instruments_payload.get("result") or {}).get("list") or []
            # Some API edge nodes may ignore the baseCoin filter; use documented All listing as fallback.
            if not any(str(x.get("baseCoin", "")).upper() == "DOGE" for x in instruments if isinstance(x, dict)):
                all_payload = _fetch_json("https://api.bybit.com/v5/market/instruments-info?category=option&baseCoin=All&status=Trading&limit=1000")
                if all_payload.get("retCode") != 0:
                    raise RuntimeError("Bybit fallback instruments API: " + str(all_payload.get("retMsg", "unknown error")))
                instruments = (all_payload.get("result") or {}).get("list") or []
            instruments = [x for x in instruments if isinstance(x, dict)
                           and str(x.get("baseCoin", "")).upper() == "DOGE"
                           and str(x.get("status", "")).upper() == "TRADING"]

            tickers_payload = _fetch_json("https://api.bybit.com/v5/market/tickers?category=option&baseCoin=DOGE")
            if tickers_payload.get("retCode") != 0:
                raise RuntimeError("Bybit tickers API: " + str(tickers_payload.get("retMsg", "unknown error")))
            tickers = (tickers_payload.get("result") or {}).get("list") or []
            ticker_map = {str(x.get("symbol", "")).upper(): x for x in tickers if isinstance(x, dict)}

            for item in instruments:
                symbol = str(item.get("symbol") or "").upper()
                parts = symbol.split("-")
                if len(parts) < 5 or parts[0] != "DOGE":
                    continue
                try:
                    strike = float(parts[2])
                except (TypeError, ValueError):
                    continue
                expiry_ms = int(item.get("deliveryTime") or 0)
                expiry = (datetime.fromtimestamp(expiry_ms / 1000, timezone.utc).strftime("%d%b%y").upper()
                          if expiry_ms > 0 else parts[1].upper())
                side = str(item.get("optionsType") or parts[3]).upper()
                option_type = "C" if side.startswith("CALL") or side == "C" else ("P" if side.startswith("PUT") or side == "P" else "")
                if strike <= 0 or not expiry or not option_type:
                    continue
                quote = ticker_map.get(symbol, {})
                def _num(*keys):
                    for key in keys:
                        try:
                            value = float(quote.get(key) or 0)
                            if value > 0:
                                return value
                        except (TypeError, ValueError):
                            pass
                    return 0.0
                index_price = _num("indexPrice", "underlyingPrice")
                bid = _num("bid1Price", "bidPrice")
                ask = _num("ask1Price", "askPrice")
                mark = _num("markPrice", "lastPrice")
                if index_price > 0 and underlying is None:
                    underlying = index_price
                # Bybit USDT-settled option premiums are already USDT values.
                rows.append({
                    "instrument_name": "DOGE-" + expiry + "-" + format(strike, "g") + "-" + option_type,
                    "exchange_symbol": symbol,
                    "strike": strike,
                    "option_type": option_type,
                    "expiry": expiry,
                    "bid_price": bid,
                    "ask_price": ask,
                    "mark_price": mark,
                    "open_interest": quote.get("openInterest", 0),
                    "volume": quote.get("volume24h", 0),
                    "underlying_price": index_price,
                    "min_trade_amount": (item.get("lotSizeFilter") or {}).get("minOrderQty", 0),
                })
            source = "Bybit public options API"
            if not rows:
                errors.append("Bybit confirms DOGE options, but no active contracts/quotes were returned")
        except Exception as exc:
            errors.append("Bybit: " + str(exc)[:180])
            return jsonify({"error": "Bybit DOGE options API unavailable", "details": errors}), 502
    elif coin in ("BTC", "ETH"):
        hosts = ("https://api.deribit.com/api/v2/public/", "https://www.deribit.com/api/v2/public/")
        instruments = summaries = None
        for host in hosts:
            try:
                payload = _fetch_json(host + "get_instruments?currency=" + coin + "&kind=option&expired=false")
                if isinstance(payload.get("result"), list):
                    instruments = payload["result"]
                    break
                errors.append("instruments response format")
            except Exception as exc:
                errors.append("instruments: " + str(exc)[:140])
        for host in hosts:
            try:
                payload = _fetch_json(host + "get_book_summary_by_currency?currency=" + coin + "&kind=option")
                if isinstance(payload.get("result"), list):
                    summaries = payload["result"]
                    break
                errors.append("summary response format")
            except Exception as exc:
                errors.append("summary: " + str(exc)[:140])
        if instruments is None and summaries is None:
            return jsonify({"error": "Deribit option API unavailable", "details": errors[-4:]}), 502
        summary_map = {x.get("instrument_name"): x for x in (summaries or []) if isinstance(x, dict)}
        source_rows = instruments if instruments else (summaries or [])
        for item in source_rows:
            if not isinstance(item, dict):
                continue
            name = item.get("instrument_name", "")
            if not name.startswith(coin + "-") or item.get("is_active") is False:
                continue
            quote = summary_map.get(name, {})
            try:
                strike = float(quote.get("strike") or item.get("strike") or 0)
            except (TypeError, ValueError):
                strike = 0
            if strike <= 0:
                continue
            row = dict(item)
            row.update(quote)
            row.update({
                "instrument_name": name,
                "strike": strike,
                "option_type": quote.get("option_type") or item.get("option_type") or "",
                "expiry": name.split("-")[1] if len(name.split("-")) > 1 else "",
                "bid_price": quote.get("bid_price", 0),
                "ask_price": quote.get("ask_price", 0),
                "mark_price": quote.get("mark_price", 0),
                "open_interest": quote.get("open_interest", 0),
                "volume": quote.get("volume", 0),
                "underlying_price": quote.get("underlying_price", 0),
                "min_trade_amount": item.get("min_trade_amount", quote.get("min_trade_amount", 0)),
            })
            rows.append(row)
            try:
                p = float(row.get("underlying_price") or 0)
                if p > 0 and underlying is None:
                    underlying = p
            except (TypeError, ValueError):
                pass
        source = "Deribit public options API"
    else:
        try:
            markets = _fetch_json("https://api.prod.paradex.trade/v1/markets?asset_kind=OPTION&base_asset=ZEC")
            summaries = _fetch_json("https://api.prod.paradex.trade/v1/markets/summary?market=ALL&asset_kind=OPTION&base_asset=ZEC")
            market_list = markets.get("results") or markets.get("data") or []
            summary_list = summaries.get("results") or summaries.get("data") or []
            if not isinstance(market_list, list) or not isinstance(summary_list, list):
                raise RuntimeError("Paradex response format changed")
            by_symbol = {}
            for summary in summary_list:
                if not isinstance(summary, dict):
                    continue
                summary_symbol = (summary.get("symbol") or summary.get("market") or summary.get("instrument_name") or "").upper()
                if summary_symbol:
                    by_symbol[summary_symbol] = summary
            for item in market_list:
                if not isinstance(item, dict):
                    continue
                name = item.get("symbol") or item.get("market") or item.get("instrument_name") or ""
                if "ZEC" not in name.upper():
                    continue
                quote = by_symbol.get(name.upper(), {})
                try:
                    strike = float(item.get("strike_price") or item.get("strike") or quote.get("strike_price") or quote.get("strike") or 0)
                except (TypeError, ValueError):
                    strike = 0
                if strike <= 0:
                    continue
                exp = item.get("expiry_date") or item.get("expiration_date") or item.get("expiry") or item.get("expiration") or ""
                typ = str(item.get("option_type") or item.get("option_side") or item.get("type") or name.split("-")[-1]).upper()
                rows.append({
                    "instrument_name": name, "strike": strike,
                    "option_type": "C" if "CALL" in typ or typ == "C" else ("P" if "PUT" in typ or typ == "P" else typ),
                    "expiry": exp,
                    # Paradex market summaries use bid/ask/mark_price; tolerate
                    # alternate documented/legacy aliases without inventing quotes.
                    "bid_price": quote.get("bid_price") or quote.get("bid") or item.get("bid_price") or item.get("bid") or 0,
                    "ask_price": quote.get("ask_price") or quote.get("ask") or item.get("ask_price") or item.get("ask") or 0,
                    "mark_price": quote.get("mark_price") or quote.get("mark") or item.get("mark_price") or item.get("mark") or 0,
                    "open_interest": quote.get("open_interest", item.get("open_interest", 0)),
                    "volume": quote.get("volume_24h") or quote.get("volume") or quote.get("total_volume") or item.get("volume_24h") or item.get("volume") or 0,
                    "underlying_price": quote.get("underlying_price", item.get("underlying_price", 0)),
                    "min_trade_amount": quote.get("min_trade_amount", item.get("min_trade_amount", item.get("min_order_size", item.get("order_size_increment", 0)))),
                })
            underlying = next((float(x.get("underlying_price") or 0) for x in rows if float(x.get("underlying_price") or 0) > 0), None)
            source = "Paradex public options API"
        except Exception as exc:
            errors.append("Paradex: " + str(exc)[:180])
            return jsonify({"error": "ZEC option API unavailable or no listed ZEC options", "details": errors}), 502

    if not underlying:
        try:
            ticker = _fetch_json("https://api.binance.com/api/v3/ticker/price?symbol=" + coin + "USDT", timeout=7)
            underlying = float(ticker.get("price") or 0) or None
        except Exception as exc:
            errors.append("spot fallback: " + str(exc)[:120])
    return jsonify({
        "coin": coin, "source": source, "rows": rows,
        "underlying": underlying, "updated_at": _utc_now(),
        "count": len(rows), "details": errors[-4:],
    })

@app.get("/api/algo/status")
def algo_status():
    return jsonify({
        "credentials_configured": bool(os.getenv("COINSWITCH_API_KEY") and os.getenv("COINSWITCH_SECRET_KEY")),
        "api_validation": "not_checked",
        "live_orders": "NOT_ACTIVE",
        "mode": "SAFE_SETUP",
        "message": "API key validation only; no orders can be placed from this endpoint."
    })

@app.get("/api/algo/validate")
def algo_validate():
    try:
        from coinswitch_api import validate_keys
        status, result = validate_keys()
        valid = status == 200 and str(result.get("message", "")).lower() == "valid access"
        return jsonify({"valid": valid, "http_status": status, "message": "CoinSwitch API access verified" if valid else "CoinSwitch returned an unexpected validation response"}), (200 if valid else 502)
    except Exception as exc:
        safe = str(exc)[:200]
        return jsonify({"valid": False, "message": safe}), 503

@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "live-market-dashboard"})


@app.get("/api/coinswitch/futures/klines")
def coinswitch_futures_klines():
    _start_coinswitch_socket()
    with feed_lock:
        snapshot = {
            "source": "CoinSwitch Futures V2 WebSocket",
            "connection": feed["connection"],
            "connected_at": feed["connected_at"],
            "last_message_at": feed["last_message_at"],
            "last_error": feed["last_error"],
            "server_time": _utc_now(),
            "coins": feed["coins"],
        }
    now_ms = int(time.time() * 1000)
    for coin_data in snapshot["coins"].values():
        rows = coin_data["candles"]
        last = rows[-1] if rows else None
        coin_data["candle_count"] = len(rows)
        coin_data["last_price"] = last["close"] if last else None
        coin_data["last_candle_closed"] = last["closed"] if last else None
        coin_data["age_seconds"] = max(0, int((now_ms - last["exchange_update_time"]) / 1000)) if last and last["exchange_update_time"] else None
        coin_data["fresh"] = bool(last and last["exchange_update_time"] and now_ms - last["exchange_update_time"] <= 120000)
    response = make_response(jsonify(snapshot))
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.get("/")
def home():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/<path:filename>")
def files(filename):
    return send_from_directory(BASE_DIR, filename)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

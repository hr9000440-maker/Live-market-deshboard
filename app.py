import os
import threading
import time
from datetime import datetime, timezone
from flask import Flask, jsonify, send_from_directory, make_response
from big_player_advisor import build_big_player_report

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
    if coin not in ("BTC", "ETH", "ZEC", "DOGE", "BNB"):
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
            if base_payload.get("retCode") != 0:
                raise RuntimeError("Bybit base-coin discovery: " + str(base_payload.get("retMsg", "unknown error")))
            base_rows = (base_payload.get("result") or {}).get("list") or []
            doge_base = next((x for x in base_rows if str(x.get("baseCoin", "")).upper() == "DOGE"), None)
            # Do not reject solely on hasSymbol: the instrument catalogue is the
            # authoritative source and Bybit's discovery flag can lag listings.

            instruments_payload = _fetch_json("https://api.bybit.com/v5/market/instruments-info?category=option&baseCoin=DOGE&limit=1000")
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
                           and str(x.get("status", "TRADING")).upper() in ("TRADING", "PRELAUNCH", "DELIVERING")]

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
                    "bid_price": bid / index_price if bid > 0 and index_price > 0 else 0,
                    "ask_price": ask / index_price if ask > 0 and index_price > 0 else 0,
                    "mark_price": mark / index_price if mark > 0 and index_price > 0 else 0,
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

        # Binance public European Options API fallback. This is also read-only:
        # it fetches listed DOGE contracts and public quotes only; no order endpoints.
        if not rows:
            try:
                info = _fetch_json("https://eapi.binance.com/eapi/v1/exchangeInfo")
                symbols = (info.get("optionSymbols") or []) if isinstance(info, dict) else []
                doge_symbols = [x for x in symbols if isinstance(x, dict)
                                and str(x.get("underlying", "")).upper() == "DOGEUSDT"
                                and str(x.get("status", "")).upper() == "TRADING"]
                tickers = _fetch_json("https://eapi.binance.com/eapi/v1/ticker")
                ticker_map = {str(x.get("symbol", "")).upper(): x for x in tickers if isinstance(x, dict)} if isinstance(tickers, list) else {}
                marks = _fetch_json("https://eapi.binance.com/eapi/v1/mark")
                mark_map = {str(x.get("symbol", "")).upper(): x for x in marks if isinstance(x, dict)} if isinstance(marks, list) else {}
                try:
                    index_data = _fetch_json("https://eapi.binance.com/eapi/v1/index?underlying=DOGEUSDT")
                    underlying = float(index_data.get("indexPrice") or 0) or None
                except Exception as exc:
                    errors.append("Binance index: " + str(exc)[:100])
                for item in doge_symbols:
                    symbol = str(item.get("symbol") or "").upper()
                    parts = symbol.split("-")
                    if len(parts) < 4:
                        continue
                    try:
                        strike = float(item.get("strikePrice") or parts[2])
                    except (TypeError, ValueError):
                        continue
                    side = str(item.get("side") or parts[-1]).upper()
                    option_type = "C" if side == "CALL" or side == "C" else ("P" if side == "PUT" or side == "P" else "")
                    if strike <= 0 or not option_type:
                        continue
                    expiry_ms = int(item.get("expiryDate") or 0)
                    expiry = (datetime.fromtimestamp(expiry_ms / 1000, timezone.utc).strftime("%d%b%y").upper()
                              if expiry_ms > 0 else parts[1].upper())
                    quote = ticker_map.get(symbol, {})
                    mark = mark_map.get(symbol, {})
                    def _binance_num(obj, *keys):
                        for key in keys:
                            try:
                                value = float(obj.get(key) or 0)
                                if value > 0:
                                    return value
                            except (TypeError, ValueError):
                                pass
                        return 0.0
                    bid = _binance_num(quote, "bidPrice")
                    ask = _binance_num(quote, "askPrice")
                    last = _binance_num(quote, "lastPrice")
                    mark_price = _binance_num(mark, "markPrice") or last
                    rows.append({
                        "instrument_name": "DOGE-" + expiry + "-" + format(strike, "g") + "-" + option_type,
                        "exchange_symbol": symbol,
                        "strike": strike,
                        "option_type": option_type,
                        "expiry": expiry,
                        # Normalize USDT premium by index so the existing UI can convert it back to USD.
                        "bid_price": bid / underlying if bid > 0 and underlying else 0,
                        "ask_price": ask / underlying if ask > 0 and underlying else 0,
                        "mark_price": mark_price / underlying if mark_price > 0 and underlying else 0,
                        "open_interest": quote.get("amount", 0),
                        "volume": quote.get("volume", 0),
                        "underlying_price": underlying or 0,
                        "min_trade_amount": item.get("minQty", 0),
                    })
                if rows:
                    source = "Binance public options API"
                else:
                    errors.append("Binance: no active DOGE option contracts with quotes")
            except Exception as exc:
                errors.append("Binance: " + str(exc)[:180])
            if not rows:
                # Gate.io public Options API fallback. Read-only market endpoints;
                # discover DOGE underlying first so unsupported pairs are never fabricated.
                try:
                    gate_underlyings = _fetch_json("https://api.gateio.ws/api/v4/options/underlyings")
                    gate_underlying = next(
                        (x for x in gate_underlyings if isinstance(x, dict)
                         and str(x.get("name", "")).upper() in ("DOGE_USDT", "DOGE_USD")),
                        None,
                    ) if isinstance(gate_underlyings, list) else None
                    if not gate_underlying:
                        raise RuntimeError("Gate.io does not currently list DOGE options underlying")
                    gate_pair = str(gate_underlying.get("name")).upper()
                    contracts = _fetch_json(
                        "https://api.gateio.ws/api/v4/options/contracts?underlying=" + gate_pair
                    )
                    tickers = _fetch_json(
                        "https://api.gateio.ws/api/v4/options/tickers?underlying=" + gate_pair
                    )
                    if not isinstance(contracts, list) or not isinstance(tickers, list):
                        raise RuntimeError("Gate.io options response format changed")
                    ticker_map = {
                        str(x.get("name", "")).upper(): x for x in tickers if isinstance(x, dict)
                    }
                    for item in contracts:
                        if not isinstance(item, dict):
                            continue
                        name = str(item.get("name") or "").upper()
                        parts = name.split("-")
                        if len(parts) < 4 or not name.startswith("DOGE_"):
                            continue
                        try:
                            strike = float(parts[-2])
                        except (TypeError, ValueError):
                            continue
                        is_call = item.get("is_call")
                        option_type = "C" if is_call is True else ("P" if is_call is False else "")
                        if not option_type or strike <= 0:
                            continue
                        expiry_ts = int(float(item.get("expiration_time") or 0))
                        expiry = (
                            datetime.fromtimestamp(expiry_ts, timezone.utc).strftime("%d%b%y").upper()
                            if expiry_ts > 0 else (parts[1] if len(parts) > 1 else "")
                        )
                        quote = ticker_map.get(name, {})
                        try:
                            index_price = float(
                                quote.get("index_price") or item.get("index_price")
                                or item.get("underlying_price")
                                or gate_underlying.get("index_price") or 0
                            )
                        except (TypeError, ValueError):
                            index_price = 0.0
                        def _gate_num(obj, *keys):
                            for key in keys:
                                try:
                                    value = float(obj.get(key) or 0)
                                    if value > 0:
                                        return value
                                except (TypeError, ValueError):
                                    pass
                            return 0.0
                        # Gate's contracts endpoint can carry quote fields too;
                        # do not discard them when the separate ticker list omits a symbol.
                        bid = _gate_num(quote, "bid1_price", "bid_price") or _gate_num(item, "bid1_price", "bid_price")
                        ask = _gate_num(quote, "ask1_price", "ask_price") or _gate_num(item, "ask1_price", "ask_price")
                        mark = (
                            _gate_num(quote, "mark_price", "last_price")
                            or _gate_num(item, "mark_price", "last_price")
                        )
                        if index_price > 0 and underlying is None:
                            underlying = index_price
                        rows.append({
                            "instrument_name": name,
                            "exchange_symbol": name,
                            "strike": strike,
                            "option_type": option_type,
                            "expiry": expiry,
                            # Gate premiums are quoted in USDT/USD; normalize to DOGE
                            # because the existing chain UI converts mark * underlying.
                            "bid_price": bid / index_price if bid > 0 and index_price > 0 else 0,
                            "ask_price": ask / index_price if ask > 0 and index_price > 0 else 0,
                            "mark_price": mark / index_price if mark > 0 and index_price > 0 else 0,
                            "open_interest": quote.get("position_size", item.get("position_size", 0)),
                            "volume": quote.get("volume_24h", quote.get("trade_size", item.get("trade_size", 0))),
                            "underlying_price": index_price,
                            "min_trade_amount": item.get("order_size_min", 0),
                        })
                    if rows:
                        source = "Gate.io public options API"
                    else:
                        raise RuntimeError("Gate.io lists DOGE underlying but returned no active contracts")
                except Exception as exc:
                    errors.append("Gate.io: " + str(exc)[:180])
            if not rows:
                return jsonify({"error": "DOGE options data unavailable from Bybit, Binance and Gate.io", "details": errors[-4:]}), 502
    elif coin == "BNB":
        # Gate.io public Options API fallback. Discover the underlying first;
        # never fabricate BNB option contracts if Gate does not list BNB_USDT.
        try:
            gate_underlyings = _fetch_json("https://api.gateio.ws/api/v4/options/underlyings")
            gate_underlying = next(
                (x for x in gate_underlyings if isinstance(x, dict)
                 and str(x.get("name", "")).upper() in ("BNB_USDT", "BNB_USD")),
                None,
            ) if isinstance(gate_underlyings, list) else None
            if not gate_underlying:
                raise RuntimeError("Gate.io options catalogue does not list BNB_USDT/BNB_USD")
            gate_pair = str(gate_underlying.get("name")).upper()
            contracts = _fetch_json(
                "https://api.gateio.ws/api/v4/options/contracts?underlying=" + gate_pair
            )
            tickers = _fetch_json(
                "https://api.gateio.ws/api/v4/options/tickers?underlying=" + gate_pair
            )
            if not isinstance(contracts, list) or not isinstance(tickers, list):
                raise RuntimeError("Gate.io options response format changed")
            ticker_map = {
                str(x.get("name", "")).upper(): x for x in tickers if isinstance(x, dict)
            }
            for item in contracts:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").upper()
                parts = name.split("-")
                if len(parts) < 4 or not (name.startswith("BNB-") or name.startswith("BNB_")):
                    continue
                try:
                    strike = float(item.get("strike_price") or parts[-2])
                except (TypeError, ValueError):
                    continue
                is_call = item.get("is_call")
                option_type = "C" if is_call is True else ("P" if is_call is False else "")
                if not option_type or strike <= 0:
                    continue
                expiry_ts = int(float(item.get("expiration_time") or 0))
                expiry = (parts[1] if len(parts) > 1 and parts[1].isdigit() and len(parts[1]) == 8 else (
                    datetime.fromtimestamp(expiry_ts, timezone.utc).strftime("%d%b%y").upper()
                    if expiry_ts > 0 else (parts[1] if len(parts) > 1 else "")
                ))
                quote = ticker_map.get(name, {})
                def _bnb_gate_num(obj, *keys):
                    for key in keys:
                        try:
                            value = float(obj.get(key) or 0)
                            if value > 0:
                                return value
                        except (TypeError, ValueError):
                            pass
                    return 0.0
                index_price = _bnb_gate_num(
                    quote, "index_price", "underlying_price"
                ) or _bnb_gate_num(item, "index_price", "underlying_price") or _bnb_gate_num(
                    gate_underlying, "index_price"
                )
                bid = _bnb_gate_num(quote, "bid1_price", "bid_price")
                ask = _bnb_gate_num(quote, "ask1_price", "ask_price")
                mark = _bnb_gate_num(quote, "mark_price", "last_price") or _bnb_gate_num(
                    item, "mark_price", "last_price"
                )
                if index_price > 0 and underlying is None:
                    underlying = index_price
                rows.append({
                    "instrument_name": name,
                    "exchange_symbol": name,
                    "strike": strike,
                    "option_type": option_type,
                    "expiry": expiry,
                    # Normalize Gate quote-currency premiums to BNB units for
                    # compatibility with the existing premium × underlying UI.
                    "bid_price": bid / index_price if bid > 0 and index_price > 0 else 0,
                    "ask_price": ask / index_price if ask > 0 and index_price > 0 else 0,
                    "mark_price": mark / index_price if mark > 0 and index_price > 0 else 0,
                    "open_interest": quote.get("position_size", item.get("position_size", 0)),
                    "volume": quote.get("volume_24h", quote.get("trade_size", item.get("trade_size", 0))),
                    "underlying_price": index_price,
                    "min_trade_amount": item.get("order_size_min", 0),
                })
            if not rows:
                raise RuntimeError("Gate.io lists BNB underlying but returned no active option contracts")
            source = "Gate.io public options API"
        except Exception as exc:
            errors.append("Gate.io: " + str(exc)[:180])
            return jsonify({
                "error": "BNB options data unavailable from Gate.io",
                "details": errors[-4:],
                "coin": coin,
                "source": "Gate.io public options API",
                "rows": [],
                "underlying": underlying,
                "updated_at": _utc_now(),
                "count": 0,
            }), 502
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
                if not exp and item.get("expiry_at"):
                    try:
                        exp = datetime.fromtimestamp(float(item["expiry_at"]) / 1000, timezone.utc).strftime("%d%b%y").upper()
                    except (TypeError, ValueError, OverflowError, OSError):
                        exp = ""
                if not exp and item.get("expiry_timestamp"):
                    try:
                        expiry_stamp = float(item["expiry_timestamp"])
                        if expiry_stamp > 10_000_000_000:
                            expiry_stamp /= 1000
                        exp = datetime.fromtimestamp(expiry_stamp, timezone.utc).strftime("%d%b%y").upper()
                    except (TypeError, ValueError, OverflowError, OSError):
                        exp = ""
                if not exp:
                    exp = next((part for part in name.replace("_", "-").split("-") if len(part) in (6, 8) and part.isdigit()), "")
                typ = str(item.get("option_type") or item.get("option_side") or item.get("type") or name.split("-")[-1]).upper()
                # Paradex ZEC options are quoted in USD, while this dashboard's
                # shared option-chain renderer expects premiums in underlying-coin
                # units and converts them back to USD with premium * underlying.
                # Normalize each quote using that contract's own underlying price.
                try:
                    index_price = float(
                        quote.get("underlying_price") or item.get("underlying_price") or 0
                    )
                except (TypeError, ValueError):
                    index_price = 0.0
                def _paradex_price(*values):
                    for value in values:
                        try:
                            number = float(value or 0)
                            if number > 0:
                                return number
                        except (TypeError, ValueError):
                            pass
                    return 0.0
                bid_usd = _paradex_price(quote.get("bid"), quote.get("bid_price"), item.get("bid"), item.get("bid_price"))
                ask_usd = _paradex_price(quote.get("ask"), quote.get("ask_price"), item.get("ask"), item.get("ask_price"))
                mark_usd = _paradex_price(quote.get("mark_price"), quote.get("mark"), item.get("mark_price"), item.get("mark"))
                # Never invent a quote or divide by a missing/invalid index price.
                bid_coin = bid_usd / index_price if bid_usd > 0 and index_price > 0 else 0
                ask_coin = ask_usd / index_price if ask_usd > 0 and index_price > 0 else 0
                mark_coin = mark_usd / index_price if mark_usd > 0 and index_price > 0 else 0
                rows.append({
                    "instrument_name": name, "strike": strike,
                    "option_type": "C" if "CALL" in typ or typ == "C" else ("P" if "PUT" in typ or typ == "P" else typ),
                    "expiry": exp,
                    "bid_price": bid_coin,
                    "ask_price": ask_coin,
                    "mark_price": mark_coin,
                    "open_interest": quote.get("open_interest", item.get("open_interest", 0)),
                    "volume": quote.get("volume_24h") or quote.get("volume") or quote.get("total_volume") or item.get("volume_24h") or item.get("volume") or 0,
                    "underlying_price": index_price,
                    "min_trade_amount": item.get("min_trade_amount", item.get("order_size_increment", item.get("min_order_size", 0))),
                })
            underlying = next((float(x.get("underlying_price") or 0) for x in rows if float(x.get("underlying_price") or 0) > 0), None)
            # If Paradex publishes contracts without live quotes, query the
            # public Derive ticker API before declaring ZEC unavailable.
            if not any(
                float(row.get("bid_price") or 0) > 0
                or float(row.get("ask_price") or 0) > 0
                or float(row.get("mark_price") or 0) > 0
                for row in rows
            ):
                base = "https://api.lyra.finance/public/"
                def _post_public(path, payload):
                    import json
                    from urllib.request import Request, urlopen
                    req = Request(
                        base + path, data=json.dumps(payload).encode("utf-8"),
                        headers={"Accept": "application/json", "Content-Type": "application/json",
                                 "User-Agent": "LiveMarketDashboard/1.0"},
                    )
                    with urlopen(req, timeout=15) as response:
                        return json.loads(response.read().decode("utf-8"))
                instruments_response = _post_public(
                    "get_instruments",
                    {"currency": "ZEC", "instrument_type": "option", "expired": False},
                )
                instruments = instruments_response.get("result") or instruments_response.get("instruments") or []
                if not isinstance(instruments, list) or not instruments:
                    raise RuntimeError("Paradex quotes empty; Derive has no active ZEC instruments")
                expiry_dates = sorted({
                    str(x.get("expiry_date") or x.get("expiryDate"))
                    for x in instruments if isinstance(x, dict)
                    and (x.get("expiry_date") or x.get("expiryDate"))
                })
                derive_rows = []
                for expiry_date in expiry_dates:
                    ticker_response = _post_public(
                        "get_tickers",
                        {"currency": "ZEC", "instrument_type": "option", "expiry_date": expiry_date},
                    )
                    tickers = ticker_response.get("result") or ticker_response.get("tickers") or []
                    for ticker in tickers if isinstance(tickers, list) else []:
                        if not isinstance(ticker, dict):
                            continue
                        name = str(ticker.get("instrument_name") or ticker.get("instrumentName") or ticker.get("symbol") or "")
                        instrument = next((x for x in instruments if isinstance(x, dict)
                                           and str(x.get("instrument_name") or x.get("instrumentName") or "") == name), {})
                        try:
                            strike = float(instrument.get("strike") or instrument.get("strike_price") or ticker.get("strike") or 0)
                        except (TypeError, ValueError):
                            strike = 0
                        if not name or strike <= 0:
                            continue
                        raw_type = str(instrument.get("option_type") or instrument.get("optionType") or name.split("-")[-1]).upper()
                        option_type = "C" if raw_type in ("C", "CALL") else ("P" if raw_type in ("P", "PUT") else "")
                        if not option_type:
                            continue
                        pricing = ticker.get("option_pricing") or ticker.get("optionPricing") or {}
                        def _positive(*values):
                            for value in values:
                                try:
                                    number = float(value or 0)
                                    if number > 0:
                                        return number
                                except (TypeError, ValueError):
                                    pass
                            return 0.0
                        index_price = _positive(ticker.get("index_price"), ticker.get("indexPrice"))
                        bid_usd = _positive(ticker.get("best_bid_price"), ticker.get("bid_price"), ticker.get("bidPrice"))
                        ask_usd = _positive(ticker.get("best_ask_price"), ticker.get("ask_price"), ticker.get("askPrice"))
                        mark_usd = _positive(ticker.get("mark_price"), ticker.get("markPrice"), pricing.get("mark_price"), pricing.get("markPrice"))
                        exp = str(instrument.get("expiry_date") or instrument.get("expiryDate") or expiry_date)
                        if exp.isdigit() and len(exp) == 8:
                            exp = datetime.strptime(exp, "%Y%m%d").strftime("%d%b%y").upper()
                        derive_rows.append({
                            "instrument_name": name, "exchange_symbol": name,
                            "strike": strike, "option_type": option_type, "expiry": exp,
                            "bid_price": bid_usd / index_price if bid_usd and index_price else 0,
                            "ask_price": ask_usd / index_price if ask_usd and index_price else 0,
                            "mark_price": mark_usd / index_price if mark_usd and index_price else 0,
                            "open_interest": ticker.get("open_interest", ticker.get("openInterest", 0)),
                            "volume": ticker.get("volume_24h", ticker.get("volume", 0)),
                            "underlying_price": index_price,
                            "min_trade_amount": instrument.get("minimum_amount", instrument.get("amount_step", 0)),
                        })
                if not any(float(x.get("bid_price") or 0) > 0 or float(x.get("ask_price") or 0) > 0 or float(x.get("mark_price") or 0) > 0 for x in derive_rows):
                    raise RuntimeError("Derive returned no live ZEC option quotes")
                rows = derive_rows
                underlying = next((float(x.get("underlying_price") or 0) for x in rows if float(x.get("underlying_price") or 0) > 0), underlying)
                source = "Derive public options API (USD quotes normalized)"
            else:
                source = "Paradex public options API (USD quotes normalized)"
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


@app.get("/api/advisor/big-players")
def big_player_advisor():
    """Read-only multi-source futures flow analysis for BTC/ETH/BNB/ZEC."""
    try:
        report = build_big_player_report(_fetch_json)
        response = make_response(jsonify(report))
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response
    except Exception as exc:
        response = make_response(jsonify({
            "error": "Big Player Advisor data unavailable",
            "details": str(exc)[:180],
            "updated_at": _utc_now(),
        }), 503)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

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

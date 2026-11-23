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
                    socketio_path="pro/realtime-rates-socket/futures/exchange_2",
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

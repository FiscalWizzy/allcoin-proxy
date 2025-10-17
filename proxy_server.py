import os
import time
import json
import threading
import feedparser
import random
from typing import Dict, Tuple, List, Optional

import requests
from flask import Flask, request, jsonify

# -----------------------
# Config (env-overridable)
# -----------------------
EXCHANGE_RATE_API_KEY = os.environ.get("EXCHANGE_RATE_API_KEY", "e351e54a567119afe9bb037d")
EXCHANGE_RATE_API_URL = os.environ.get("EXCHANGE_RATE_API_URL", "https://v6.exchangerate-api.com/v6")
BINANCE_API = os.environ.get("BINANCE_API", "https://api.binance.com")
COINGECKO_API = os.environ.get("COINGECKO_API", "https://api.coingecko.com/api/v3")
PORT = int(os.environ.get("PORT", "8080"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")  # "DEBUG" to be chatty

# Adaptive refresh cadence (seconds)
CADENCE = {
    "1m": 5,
    "5m": 30,
    "1h": 180,
    "1d": 600,
}

# Tracked markets
TRACKED_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "XRPUSDT", "DOGEUSDT", "ARUSDT",
    "LINKUSDT", "RENDERUSDT", "LTCUSDT", "SOLUSDT",
    "BCHUSDT", "ETCUSDT", "ADAUSDT", "TRXUSDT", "DOTUSDT",
]
TRACKED_INTERVALS = ["1m", "5m", "1h", "1d"]

# Candles memory bounds
MAX_CANDLES_PER_KEY = 5000
RETURN_CANDLES = 200

# Fiat board cadence (seconds)
FIAT_REFRESH = 1800  # 30 min

# Insights cadence (seconds)
INSIGHTS_REFRESH = 120 # 2 min

_threads_started = False


# -------------
# Flask & HTTP
# -------------
app = Flask(__name__)
_session = requests.Session()

_threads_started = False

@app.before_request
def activate_background_threads():
    global _threads_started
    if not _threads_started:
        _threads_started = True
        _log("INFO", "🚀 Bootstrapping background threads for Render (Flask 3.x)...")
        start_threads()




def _log(level: str, *args):
    if LOG_LEVEL == "DEBUG" or level != "DEBUG":
        print(*args, flush=True)

def http_get_json(url: str, timeout: float = 10.0, params: Optional[dict] = None):
    resp = _session.get(url, timeout=timeout, params=params)
    resp.raise_for_status()
    return resp.json()


# -------------
# In-memory cache
# -------------
_cache_lock = threading.Lock()

# candle_cache[(symbol, interval)] = List[ (ts_sec, open, high, low, close) ]
candle_cache: Dict[Tuple[str, str], List[Tuple[float, float, float, float, float]]] = {}
# last trade price cache: last_price[symbol] = float
last_price: Dict[str, float] = {}

# Fiat board state
last_fiat_rates: Dict[str, float] = {}
fiat_board_snapshot: Dict = {}

# Insights snapshot
insights_snapshot: Dict = {}

# ---------------------------
# Helpers for candle merging
# ---------------------------
def _normalize_klines(raw_klines) -> List[Tuple[float, float, float, float, float]]:
    # Binance kline: [openTime, open, high, low, close, volume, closeTime, ...]
    out = []
    for c in raw_klines:
        try:
            out.append((
                float(c[0]) / 1000.0,
                float(c[1]),
                float(c[2]),
                float(c[3]),
                float(c[4]),
            ))
        except Exception:
            continue
    return out

def _bounded_extend(key, new_rows):
    """Append and cap to MAX_CANDLES_PER_KEY."""
    with _cache_lock:
        cur = candle_cache.get(key, [])
        if cur and new_rows and new_rows[0][0] <= cur[-1][0]:
            # If overlapping, drop duplicates by timestamp
            ts_set = {t for (t, *_rest) in cur}
            cur.extend([row for row in new_rows if row[0] not in ts_set])
        else:
            cur.extend(new_rows)
        # Cap
        if len(cur) > MAX_CANDLES_PER_KEY:
            cur = cur[-MAX_CANDLES_PER_KEY:]
        candle_cache[key] = cur

def _get_cached_slice(key, end_ts: Optional[float], limit: int) -> List[Tuple[float, float, float, float, float]]:
    with _cache_lock:
        rows = candle_cache.get(key, [])
        if not rows:
            return []
        if end_ts is None:
            return rows[-limit:]
        # filter rows strictly older-or-equal to end_ts
        filtered = [r for r in rows if r[0] <= end_ts]
        return filtered[-limit:] if filtered else []

# --------------------------
# Background: Crypto candles
# --------------------------
def _fetch_klines(symbol: str, interval: str, limit: int = 200, end_time_ms: Optional[int] = None):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if end_time_ms is not None:
        params["endTime"] = end_time_ms
    url = f"{BINANCE_API}/api/v3/klines"
    return http_get_json(url, timeout=10, params=params)

def _update_candles_for(symbols: List[str], interval: str):
    for sym in symbols:
        try:
            raw = _fetch_klines(sym, interval, limit=200)
            rows = _normalize_klines(raw)
            if not rows:
                continue
            _bounded_extend((sym, interval), rows)
            # Update last price from the last close
            with _cache_lock:
                last_price[sym] = rows[-1][4]
            _log("DEBUG", f"✅ {sym} {interval}: {len(rows)} new, cache={len(candle_cache.get((sym, interval), []))}")
        except Exception as e:
            _log("INFO", f"⚠️ Candles failed {sym} {interval}: {e}")

def _candles_loop(interval: str):
    # Runs forever with its own cadence
    cadence = CADENCE.get(interval, 30)
    while True:
        started = time.time()
        _update_candles_for(TRACKED_SYMBOLS, interval)
        took = time.time() - started
        sleep_for = max(1.0, cadence - took)
        _log("INFO", f"⏱️ {interval} batch took {took:.1f}s; sleeping {sleep_for:.1f}s")
        time.sleep(sleep_for)

# -----------------------
# Background: Fiat board
# -----------------------
def _fiat_board_loop():
    global fiat_board_snapshot, last_fiat_rates
    while True:
        try:
            usd = http_get_json(f"{EXCHANGE_RATE_API_URL}/{EXCHANGE_RATE_API_KEY}/latest/USD", timeout=10)
            eur = http_get_json(f"{EXCHANGE_RATE_API_URL}/{EXCHANGE_RATE_API_KEY}/latest/EUR", timeout=10)

            usd_rates = usd.get("conversion_rates", {}) or {}
            eur_rates = eur.get("conversion_rates", {}) or {}

            pairs = {
                "USD_EUR": usd_rates.get("EUR"),
                "GBP_USD": (1 / usd_rates["GBP"]) if usd_rates.get("GBP") else None,
                "USD_JPY": usd_rates.get("JPY"),
                "USD_CHF": usd_rates.get("CHF"),
                "AUD_USD": (1 / usd_rates["AUD"]) if usd_rates.get("AUD") else None,
                "USD_CAD": usd_rates.get("CAD"),
                "EUR_GBP": eur_rates.get("GBP"),
            }

            # Build snapshot with previous/current
            with _cache_lock:
                pairs_out = {}
                for k, cur in pairs.items():
                    prev = last_fiat_rates.get(k)
                    if prev is None:
                        # first run: neutral baseline
                        pairs_out[k] = {"current": cur, "previous": cur}
                    else:
                        pairs_out[k] = {"current": cur, "previous": prev}
                    last_fiat_rates[k] = cur
                fiat_board_snapshot = {"pairs": pairs_out, "timestamp": time.time()}

            _log("INFO", "✅ Fiat board refreshed")
        except Exception as e:
            _log("INFO", f"⚠️ Fiat board update failed: {e}")
        time.sleep(FIAT_REFRESH)

# -----------------------
# Background: Frankfurter live delta
# -----------------------
def _frankfurter_delta_loop():
    """Compare cached fiat rates with Frankfurter's live data every 60s."""
    global fiat_board_snapshot, last_fiat_rates
    while True:
        time.sleep(60)  # check every minute
        try:
            frank = http_get_json("https://api.frankfurter.app/latest?from=USD", timeout=8)
            rates = frank.get("rates", {}) or {}

            # Only update pairs we already track
            with _cache_lock:
                if not fiat_board_snapshot:
                    continue

                pairs_out = fiat_board_snapshot.get("pairs", {}).copy()
                changed = False

                for pair in list(pairs_out.keys()):
                    if pair == "USD_EUR":
                        new_val = rates.get("EUR")
                    elif pair == "USD_JPY":
                        new_val = rates.get("JPY")
                    elif pair == "USD_CHF":
                        new_val = rates.get("CHF")
                    elif pair == "USD_CAD":
                        new_val = rates.get("CAD")
                    elif pair == "AUD_USD":
                        aud = rates.get("AUD")
                        new_val = (1 / aud) if aud else None
                    elif pair == "GBP_USD":
                        gbp = rates.get("GBP")
                        new_val = (1 / gbp) if gbp else None
                    elif pair == "EUR_GBP":
                        # Use Frankfurter’s EUR base for this one
                        eur = http_get_json("https://api.frankfurter.app/latest?from=EUR", timeout=5)
                        new_val = eur.get("rates", {}).get("GBP")
                    else:
                        continue

                    if not new_val:
                        continue

                    prev_val = pairs_out[pair]["current"]
                    # Only update if difference > 0.0001 (avoid flicker)
                    if abs(new_val - prev_val) > 0.0001:
                        pairs_out[pair]["previous"] = prev_val
                        pairs_out[pair]["current"] = new_val
                        last_fiat_rates[pair] = new_val
                        changed = True

                if changed:
                    fiat_board_snapshot["pairs"] = pairs_out
                    fiat_board_snapshot["timestamp"] = time.time()
                    _log("INFO", "⚡ Fiat board instantly refreshed via Frankfurter")

        except Exception as e:
            _log("INFO", f"⚠️ Frankfurter delta check failed: {e}")


# -----------------------
# Background: Insights
# -----------------------
def _insights_loop():
    """Fetch BTC, ETH, EUR/USD, and DAX; auto-backoff on 429 errors."""
    global insights_snapshot
    cooldown = 0
    while True:
        try:
            if cooldown > 0:
                _log("INFO", f"🕒 Cooling down {cooldown}s to respect API limits")
                time.sleep(cooldown)
                cooldown = 0

            # --- Crypto prices from CoinGecko ---
            cg = http_get_json(
                f"{COINGECKO_API}/simple/price",
                timeout=8,
                params={"ids": "bitcoin,ethereum", "vs_currencies": "usd"},
            )

            # --- EUR/USD from ExchangeRate API ---
            fx = http_get_json(
                f"{EXCHANGE_RATE_API_URL}/{EXCHANGE_RATE_API_KEY}/pair/EUR/USD",
                timeout=8,
            )

            # --- DAX from Yahoo Finance ---
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/127.0.0.1 Safari/537.36"
                )
            }
            dax_resp = requests.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/%5EGDAXI",
                params={"interval": "1h"},
                headers=headers,
                timeout=8,
            )
            dax_json = dax_resp.json()
            dax_val = (
                dax_json.get("chart", {})
                .get("result", [{}])[0]
                .get("meta", {})
                .get("regularMarketPrice")
            )

            # --- Build snapshot ---
            snap = {
                "btc_usd": (cg.get("bitcoin", {}) or {}).get("usd"),
                "eth_usd": (cg.get("ethereum", {}) or {}).get("usd"),
                "eur_usd": fx.get("conversion_rate"),
                "dax": dax_val,
                "timestamp": time.time(),
            }
            with _cache_lock:
                insights_snapshot = snap
            _log("INFO", f"✅ Insights updated: {snap}")

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                _log("INFO", "⚠️ CoinGecko rate limit hit. Backing off for 2 minutes.")
                cooldown = 120  # wait 2 minutes
            else:
                _log("INFO", f"⚠️ Insights HTTP error: {e}")
        except Exception as e:
            _log("INFO", f"⚠️ Insights update failed: {e}")

        time.sleep(INSIGHTS_REFRESH + random.randint(0, 60))

# -----------
# HTTP routes
# -----------
@app.route("/health")
def health():
    return jsonify({"ok": True, "time": time.time()})

@app.route("/fiat-board")
def fiat_board():
    with _cache_lock:
        data = fiat_board_snapshot.copy() if fiat_board_snapshot else None
    if not data:
        return jsonify({"error": "no cached data yet"}), 503
    return jsonify(data)

@app.route("/insights")
def insights():
    with _cache_lock:
        data = insights_snapshot.copy() if insights_snapshot else None
    if not data:
        return jsonify({"error": "no cached data yet"}), 503
    return jsonify(data)

@app.route("/crypto-chart")
def crypto_chart():
    symbol = request.args.get("symbol", "BTCUSDT").upper()
    interval = request.args.get("interval", "1m")
    end_time = request.args.get("endTime")  # ms since epoch (Binance style) (optional)
    limit = int(request.args.get("limit", str(RETURN_CANDLES)))

    # Serve from cache; if cache is empty and endTime provided, attempt on-demand fill for that slice
    end_ts = None
    if end_time:
        try:
            end_ts = float(end_time) / 1000.0
        except Exception:
            end_ts = None

    key = (symbol, interval)
    rows = _get_cached_slice(key, end_ts=end_ts, limit=limit)

    if not rows and end_time:
        # Optional: cache-first but try to fetch older slice if asked explicitly
        try:
            raw = _fetch_klines(symbol, interval, limit=limit, end_time_ms=int(end_time))
            older = _normalize_klines(raw)
            if older:
                _bounded_extend(key, older)
                rows = _get_cached_slice(key, end_ts=end_ts, limit=limit)
        except Exception as e:
            _log("INFO", f"⚠️ On-demand candle fetch failed {symbol} {interval}: {e}")

    if not rows:
        return jsonify({"error": "no cached data yet"}), 503

    # Return as server-expected [[ts_ms, o, h, l, c], ...] (your client divides by 1000 currently—keep consistent)
    out = [[int(r[0] * 1000), r[1], r[2], r[3], r[4]] for r in rows]
    return jsonify({"candles": out})

@app.route("/convert")
def convert():
    """Convert fiat currencies using cached rates from fiat_board_snapshot."""
    from_cur = request.args.get("from", "").upper()
    to_cur = request.args.get("to", "").upper()
    amount = request.args.get("amount", type=float, default=1.0)

    with _cache_lock:
        pairs = fiat_board_snapshot.get("pairs", {}) if fiat_board_snapshot else {}

    # If no cache yet
    if not pairs:
        return jsonify({"error": "no cached fiat data yet"}), 503

    # Build a small lookup using USD as the central base
    usd_rates = {
        "USD": 1.0,
        "EUR": pairs.get("USD_EUR", {}).get("current"),
        "JPY": pairs.get("USD_JPY", {}).get("current"),
        "CHF": pairs.get("USD_CHF", {}).get("current"),
        "CAD": pairs.get("USD_CAD", {}).get("current"),
        "GBP": 1 / pairs.get("GBP_USD", {}).get("current") if pairs.get("GBP_USD") else None,
        "AUD": 1 / pairs.get("AUD_USD", {}).get("current") if pairs.get("AUD_USD") else None,
    }

    # Validate requested currencies
    if from_cur not in usd_rates or to_cur not in usd_rates or not usd_rates[from_cur] or not usd_rates[to_cur]:
        return jsonify({"error": f"unsupported or missing pair {from_cur}/{to_cur}"}), 400

    try:
        amount_in_usd = amount / usd_rates[from_cur]
        converted = amount_in_usd * usd_rates[to_cur]
        return jsonify({
            "from": from_cur,
            "to": to_cur,
            "amount": amount,
            "converted": round(converted, 6)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/crypto-price")
def crypto_price():
    # Return last_price for tracked symbols (e.g., BTCUSDT, ETHUSDT, …)
    with _cache_lock:
        data = dict(last_price)
    if not data:
        return jsonify({"error": "no cached data yet"}), 503
    return jsonify(data)

@app.route("/debug")
def debug_info():
    with _cache_lock:
        data = {
            "candle_keys": list(candle_cache.keys())[:5],
            "fiat_pairs": list(fiat_board_snapshot.get("pairs", {}).keys()) if fiat_board_snapshot else [],
            "insights_ready": bool(insights_snapshot),
            "last_price_count": len(last_price),
            "thread_count": threading.active_count(),
            "timestamp": time.time(),
        }
    return jsonify(data)


# -------------------
# Thread orchestration
# -------------------
def start_threads():
    # Four candle threads (one per interval) to decouple cadences
    for iv in TRACKED_INTERVALS:
        t = threading.Thread(target=_candles_loop, args=(iv,), daemon=True)
        t.start()

    # Fiat board
    threading.Thread(target=_fiat_board_loop, daemon=True).start()

    # Frankfurter instant delta loop
    threading.Thread(target=_frankfurter_delta_loop, daemon=True).start()

    # Insights
    threading.Thread(target=_insights_loop, daemon=True).start()


# ✅ Start threads automatically on Render (after function exists)
if os.environ.get("RUN_MAIN") != "true":
    _log("INFO", "🚀 Bootstrapping background threads for Render...")
    threading.Thread(target=start_threads, daemon=True).start()

# -----
# Main
# -----
if __name__ == "__main__":
    _log("INFO", "🚀 Starting background threads…")
    start_threads()
    _log("INFO", "✅ Threads started. Serving Flask on 0.0.0.0:8080")
    # For Cloud Run, debug=False is recommended
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)

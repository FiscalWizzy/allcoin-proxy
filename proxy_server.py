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
EXCHANGE_RATE_API_URL = os.environ.get("EXCHANGE_RATE_API_URL", "https://api.frankfurter.app")
BINANCE_API = os.environ.get("BINANCE_API", "https://api.binance.com")
PORT = int(os.environ.get("PORT", "8080"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")  # "DEBUG" to be chatty

# -----------------------
# Exchange rate caching
# -----------------------
EXCHANGE_CACHE = {}
EXCHANGE_TTL = 1800  # cache for 30 minutes


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
INSIGHTS_REFRESH = 1800 # 30 min

_threads_started = False
STRICT_CACHE_ONLY = True  # do not hit Binance inside request handler


# -----------------------
# Persistence paths
# -----------------------
DATA_DIR = "/tmp/allcoin_cache"  # Render allows writing to /tmp
os.makedirs(DATA_DIR, exist_ok=True)

CANDLE_FILE = os.path.join(DATA_DIR, "candles.json")
FIAT_FILE = os.path.join(DATA_DIR, "fiat.json")
INSIGHTS_FILE = os.path.join(DATA_DIR, "insights.json")

_backfill_done = {iv: False for iv in TRACKED_INTERVALS}

all_fiat_rates: dict = {}

# ✅ Daily baseline for 24-hour movement comparison
daily_baseline = {"timestamp": 0, "rates": {}}


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

def get_exchange_rate_cached(base: str, quote: str) -> float | None:
    """Fetch exchange rates from Frankfurter once per hour and reuse."""
    key = f"{base}_{quote}"
    now = time.time()

    # Serve from cache if still fresh
    if key in _exchange_cache:
        rate, ts = _exchange_cache[key]
        if now - ts < _exchange_cache_ttl:
            return rate

    try:
        resp = requests.get(
            f"https://api.frankfurter.app/latest?from={base}&to={quote}",
            timeout=10
        )
        data = resp.json()
        rate = data["rates"].get(quote)
        if rate:
            _exchange_cache[key] = (rate, now)
            return rate
    except Exception as e:
        print(f"⚠️ get_exchange_rate_cached failed: {e}")

    return None


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

daily_baseline: Dict = {"day": None, "timestamp": 0, "rates": {}}


# Insights snapshot
insights_snapshot: Dict = {}

def _save_cache():
    """Persist in-memory caches to disk."""
    with _cache_lock:
        try:
            with open(CANDLE_FILE, "w") as f:
                json.dump(candle_cache, f)
            with open(FIAT_FILE, "w") as f:
                json.dump(fiat_board_snapshot, f)
            with open(INSIGHTS_FILE, "w") as f:
                json.dump(insights_snapshot, f)
            _log("INFO", "💾 Cache saved successfully.")
        except Exception as e:
            _log("INFO", f"⚠️ Cache save failed: {e}")

def _load_cache():
    """Load cached data from disk if present."""
    global candle_cache, fiat_board_snapshot, insights_snapshot
    try:
        if os.path.exists(CANDLE_FILE):
            with open(CANDLE_FILE, "r") as f:
                candle_cache = json.load(f)
        if os.path.exists(FIAT_FILE):
            with open(FIAT_FILE, "r") as f:
                fiat_board_snapshot = json.load(f)
        if os.path.exists(INSIGHTS_FILE):
            with open(INSIGHTS_FILE, "r") as f:
                insights_snapshot = json.load(f)
        _log("INFO", "♻️ Cache restored from disk.")
    except Exception as e:
        _log("INFO", f"⚠️ Cache load failed: {e}")


# ---------------------------
# Helpers for candle merging
# ---------------------------
def _normalize_klines(raw_klines) -> List[Tuple[float, float, float, float, float]]:
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
    out.sort(key=lambda x: x[0])  # ✅ ensure chronological order
    return out


def _merge_extend(key, new_rows):
    """Upsert by timestamp and cap to MAX_CANDLES_PER_KEY."""
    with _cache_lock:
        cur = candle_cache.get(key, [])
        if not cur:
            # First fill
            candle_cache[key] = new_rows[-MAX_CANDLES_PER_KEY:]
            return

        # Build an index for fast overwrite
        idx = {t: i for i, (t, *_rest) in enumerate(cur)}
        for row in new_rows:
            t = row[0]
            if t in idx:
                cur[idx[t]] = row  # overwrite existing candle for same openTime
            else:
                cur.append(row)    # append newer candle

        # Cap memory
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

    for attempt in range(3):  # up to 3 retries
        try:
            return http_get_json(url, timeout=10, params=params)
        except requests.exceptions.RequestException as e:
            wait = 2 ** attempt
            _log("INFO", f"⚠️ Retry {attempt+1}/3 for {symbol} {interval} after {wait}s: {e}")
            time.sleep(wait)
    raise Exception(f"❌ Failed to fetch {symbol} {interval} after 3 retries")


def _bounded_extend(key, new_rows):
    """Append new candle rows to cache and cap list length."""
    with _cache_lock:
        cur = candle_cache.get(key, [])
        all_rows = cur + new_rows
        # Deduplicate by timestamp
        seen = {}
        for ts, o, h, l, c in sorted(all_rows, key=lambda r: r[0]):
            seen[ts] = (ts, o, h, l, c)
        merged = list(seen.values())
        if len(merged) > MAX_CANDLES_PER_KEY:
            merged = merged[-MAX_CANDLES_PER_KEY:]
        candle_cache[key] = merged



def _backfill_history(interval: str):
    """One-time startup backfill for each tracked symbol."""
    symbols = TRACKED_SYMBOLS[:]
    total_symbols = len(symbols)
    completed = 0

    for sym in symbols:
        try:
            _log("INFO", f"⬅️ Backfilling {sym} {interval}…")
            end_time = int(time.time() * 1000)
            all_rows = []

            for _ in range(2):  # 5 × 1000 = 5000 max
                # Retry wrapper for robustness
                for attempt in range(3):
                    try:
                        raw = _fetch_klines(sym, interval, limit=1000, end_time_ms=end_time)
                        break
                    except Exception as e:
                        wait = 2 ** attempt
                        _log("INFO", f"⚠️ Retry {attempt+1}/3 for {sym} {interval} after {wait}s: {e}")
                        time.sleep(wait)
                else:
                    _log("INFO", f"❌ Skipping {sym} {interval} after 3 retries")
                    break

                rows = _normalize_klines(raw)
                if not rows:
                    break
                all_rows = rows + all_rows
                end_time = int(rows[0][0] * 1000) - 1  # move backward
                time.sleep(0.3)  # throttle to avoid 429s

            if all_rows:
                _bounded_extend((sym, interval), all_rows)
                with _cache_lock:
                    last_price[sym] = all_rows[-1][4]
            _log("INFO", f"✅ Backfilled {sym} {interval}: {len(all_rows)} candles")

        except Exception as e:
            _log("INFO", f"⚠️ Backfill failed {sym} {interval}: {e}")

        completed += 1
        _log("INFO", f"🎯 Backfill progress {interval}: ({completed}/{total_symbols})")

    _log("INFO", f"🎉 Backfill complete for interval {interval}")


    # --- Track global backfill completion ---
    global _backfill_done
    with _cache_lock:
        _backfill_done[interval] = True

    if all(_backfill_done.get(iv, False) for iv in TRACKED_INTERVALS):
        _log("INFO", "🎉 All intervals backfilled successfully!")


def _update_candles_for(symbols: List[str], interval: str):
    for sym in symbols:
        try:
            raw = _fetch_klines(sym, interval, limit=200)
            rows = _normalize_klines(raw)
            if not rows:
                continue
            _merge_extend((sym, interval), rows)
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

def _background_history_cache():
    """
    Keep a small rolling cache (e.g. last 300–600 candles) per tracked pair & interval.
    Works quietly in the background and never triggers rate-limit spikes.
    """
    max_keep = 600        # keep ~600 candles in memory
    fetch_batch = 200     # fetch this many per update
    sleep_between_pairs = 4.0  # seconds between symbols (to avoid 429s)

    while True:
        for interval in TRACKED_INTERVALS:
            for sym in TRACKED_SYMBOLS:
                try:
                    key = (sym, interval)
                    with _cache_lock:
                        cur = candle_cache.get(key, [])
                        have = len(cur)
                        latest_ts = cur[-1][0] * 1000 if cur else None

                    # If we have fewer than half the desired cache, top it up
                    if have < max_keep // 2:
                        params = {"symbol": sym, "interval": interval, "limit": fetch_batch}
                        if latest_ts:
                            params["endTime"] = int(latest_ts)
                        raw = http_get_json(f"{BINANCE_API}/api/v3/klines", timeout=10, params=params)
                        rows = _normalize_klines(raw)
                        if not rows:
                            continue

                        # Merge safely
                        _bounded_extend(key, rows)

                        # Cap memory
                        with _cache_lock:
                            candle_cache[key] = candle_cache[key][-max_keep:]

                        _log("INFO", f"🕓 Cached {len(rows)} new candles for {sym} {interval} (cache={len(candle_cache[key])})")

                    time.sleep(sleep_between_pairs)

                except Exception as e:
                    _log("INFO", f"⚠️ Cache refill failed {sym} {interval}: {e}")
                    time.sleep(2)

        # Whole cycle done — rest before starting again
        _log("INFO", "💤 History cache cycle complete — sleeping 10m")
        time.sleep(600)  # run every 10 minutes


# -----------------------
# Background: Fiat board
# -----------------------

def _fiat_board_loop():
    """
    Fetch and cache all fiat rates from Frankfurter;
    keep top-10 daily movers compared to the previous day.
    """
    global all_fiat_rates, fiat_board_snapshot, last_fiat_rates, daily_baseline

    while True:
        try:
            # --- Fetch today’s USD base rates ---
            usd_data = http_get_json("https://api.frankfurter.app/latest?from=USD", timeout=10)
            rates = usd_data.get("rates", {}) or {}
            if not rates:
                raise ValueError("Empty data from Frankfurter")

            now = time.time()
            current_day = time.strftime("%Y-%m-%d", time.gmtime())

            # --- If baseline is empty, try to prime it with yesterday’s data ---
            if not daily_baseline.get("rates"):
                try:
                    from datetime import date, timedelta
                    yesterday = (date.today() - timedelta(days=1)).isoformat()
                    yest_data = http_get_json(f"https://api.frankfurter.app/{yesterday}?from=USD", timeout=10)
                    yest_rates = yest_data.get("rates", {}) or {}
                    if yest_rates:
                        daily_baseline = {
                            "day": yesterday,
                            "timestamp": now,
                            "rates": yest_rates.copy(),
                        }
                        _log("INFO", f"🕓 Baseline primed with yesterday’s rates ({yesterday})")
                except Exception as e:
                    _log("INFO", f"⚠️ Could not fetch yesterday’s rates: {e}")

            # --- Reset baseline once per new calendar day ---
            if daily_baseline.get("day") != current_day:
                daily_baseline = {
                    "day": current_day,
                    "timestamp": now,
                    "rates": rates.copy(),
                }
                _log("INFO", f"🌅 New daily baseline set for {current_day}")

            # --- Compute % change vs. baseline ---
            changes = {}
            for cur, val in rates.items():
                base_val = daily_baseline["rates"].get(cur, val)
                if not base_val:
                    pct = 0.0
                else:
                    pct = ((val / base_val) - 1) * 100
                changes[cur] = {"current": val, "baseline": base_val, "change": pct}

            # --- Select top-10 movers by absolute change ---
            top10 = sorted(changes.items(), key=lambda kv: abs(kv[1]["change"]), reverse=True)[:10]
            pairs_out = {f"USD_{k}": v for k, v in top10}

            # --- Update caches safely ---
            with _cache_lock:
                all_fiat_rates = rates.copy()
                fiat_board_snapshot = {"pairs": pairs_out, "timestamp": now}

            _log("INFO", f"✅ Cached {len(all_fiat_rates)} rates; top-10 movers ready for {current_day}")

        except Exception as e:
            _log("INFO", f"⚠️ Fiat board update failed: {e}")

        # --- Wait until next refresh window ---
        time.sleep(FIAT_REFRESH)




# -----------------------
# Background: Insights etc.
# -----------------------

# (Keep your _insights_loop(), candle threads, etc. here — unchanged)


def warmup_insights():
    """Instantly populate the insights snapshot on startup, with auto-retry."""
    global insights_snapshot
    retry_delay = 120  # seconds

    while True:
        try:
            _log("INFO", "⚡ Running startup warm-up for insights…")

            # BTC & ETH
            btc_resp = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=8)
            eth_resp = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT", timeout=8)
            btc_usd = float(btc_resp.json().get("price", 0))
            eth_usd = float(eth_resp.json().get("price", 0))

            # EUR/USD (cached call to avoid 429s)
            eur_usd = get_exchange_rate_cached("EUR", "USD")

            # DAX (Yahoo)
            headers = {"User-Agent": "Mozilla/5.0"}
            dax_resp = requests.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/%5EGDAXI",
                params={"interval": "1h"},
                headers=headers,
                timeout=8,
            )
            dax_json = dax_resp.json()
            dax_val = (
                dax_json.get("chart", {}).get("result", [{}])[0]
                .get("meta", {}).get("regularMarketPrice")
            )

            if not all([btc_usd, eth_usd, eur_usd, dax_val]):
                raise ValueError("One or more warm-up values are missing")

            with _cache_lock:
                insights_snapshot = {
                    "btc_usd": btc_usd,
                    "eth_usd": eth_usd,
                    "eur_usd": eur_usd,
                    "dax": dax_val,
                    "timestamp": time.time(),
                }

            _log("INFO", f"✅ Warm-up complete — BTC={btc_usd}, ETH={eth_usd}, EUR/USD={eur_usd}, DAX={dax_val}")
            break  # ✅ success → stop retrying

        except Exception as e:
            _log("INFO", f"⚠️ Warm-up failed: {e} — retrying in {retry_delay}s")
            time.sleep(retry_delay)


def warmup_fiat_board():
    """Instantly populate fiat_board_snapshot on startup, with auto-retry."""
    global fiat_board_snapshot, last_fiat_rates
    retry_delay = 120  # seconds

    while True:
        try:
            _log("INFO", "⚡ Running startup warm-up for fiat board…")
            print(f"🌍 DEBUG: EXCHANGE_RATE_API_URL = {EXCHANGE_RATE_API_URL}", flush=True)


            usd = http_get_json(f"{EXCHANGE_RATE_API_URL}/latest?from=USD", timeout=10)
            eur = http_get_json(f"{EXCHANGE_RATE_API_URL}/latest?from=EUR", timeout=10)

            usd_rates = usd.get("rates", {}) or {}
            eur_rates = eur.get("rates", {}) or {}


            pairs = {
                "USD_EUR": usd_rates.get("EUR"),
                "GBP_USD": (1 / usd_rates["GBP"]) if usd_rates.get("GBP") else None,
                "USD_JPY": usd_rates.get("JPY"),
                "USD_CHF": usd_rates.get("CHF"),
                "AUD_USD": (1 / usd_rates["AUD"]) if usd_rates.get("AUD") else None,
                "USD_CAD": usd_rates.get("CAD"),
                "EUR_GBP": eur_rates.get("GBP"),
            }

            with _cache_lock:
                pairs_out = {}
                for k, cur in pairs.items():
                    if cur is None:
                        continue
                    prev = last_fiat_rates.get(k, cur)
                    pairs_out[k] = {"current": cur, "previous": prev}
                    last_fiat_rates[k] = cur
                fiat_board_snapshot = {"pairs": pairs_out, "timestamp": time.time()}

            _log("INFO", f"✅ Fiat board warm-up complete with {len(pairs_out)} pairs.")
            break  # ✅ success → stop retrying

        except Exception as e:
            _log("INFO", f"⚠️ Fiat board warm-up failed: {e} — retrying in {retry_delay}s")
            time.sleep(retry_delay)


def manual_refresh_fiat():
    try:
        requests.get(f"{BASE_URL}/refresh-fiat", timeout=10)
        time.sleep(1)  # short delay for backend to update cache
        update_financial_insights()  # your function that redraws board
    except Exception as e:
        print("⚠️ Manual refresh failed:", e)


# -------------------
# Thread orchestration
# -------------------
def start_threads():
    # --- Warm-up before starting background loops ---
    warmup_insights()
    warmup_fiat_board()

    # Live updaters
    for iv in TRACKED_INTERVALS:
        threading.Thread(target=_candles_loop, args=(iv,), daemon=True).start()

    # Staggered backfill (to avoid RAM spike)
    for idx, iv in enumerate(TRACKED_INTERVALS):
        threading.Timer(idx * 60, lambda iv=iv: _backfill_history(iv)).start()

    # Other loops
    threading.Thread(target=_fiat_board_loop, daemon=True).start()
    threading.Thread(target=_insights_loop, daemon=True).start()



def _periodic_save():
    while True:
        time.sleep(300)  # every 5 minutes
        _save_cache()

threading.Thread(target=_periodic_save, daemon=True).start()

def _self_ping():
    while True:
        try:
            requests.get(f"https://allcoin-proxy.onrender.com/health", timeout=5)
            _log("DEBUG", "🔁 Self-ping OK")
        except Exception as e:
            _log("INFO", f"⚠️ Self-ping failed: {e}")
        time.sleep(600)  # every 10 minutes

threading.Thread(target=_self_ping, daemon=True).start()


# -----------------------
# Cached exchange-rate helper (Frankfurter-based, no API key)
# -----------------------
_exchange_cache = {}
_exchange_cache_ttl = 3600  # 1 hour



def _insights_loop():
    """Fetch BTC, ETH, EUR/USD, and DAX; auto-backoff on errors."""
    global insights_snapshot
    cooldown = 0
    while True:
        try:
            if cooldown > 0:
                _log("INFO", f"🕒 Cooling down {cooldown}s to respect limits")
                time.sleep(cooldown)
                cooldown = 0

            # --- BTC & ETH from Binance ---
            btc_resp = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=8)
            eth_resp = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT", timeout=8)
            btc_usd = float(btc_resp.json()["price"])
            eth_usd = float(eth_resp.json()["price"])

            # --- EUR/USD via Frankfurter (cached hourly) ---
            eur_usd = get_exchange_rate_cached("EUR", "USD")

            # --- DAX from Yahoo Finance ---
            headers = {"User-Agent": "Mozilla/5.0"}
            dax_resp = requests.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/%5EGDAXI",
                params={"interval": "1h"},
                headers=headers,
                timeout=8,
            )
            dax_json = dax_resp.json()
            dax_val = (
                dax_json.get("chart", {}).get("result", [{}])[0]
                .get("meta", {}).get("regularMarketPrice")
            )

            snap = {
                "btc_usd": btc_usd,
                "eth_usd": eth_usd,
                "eur_usd": eur_usd,
                "dax": dax_val,
                "timestamp": time.time(),
            }

            with _cache_lock:
                insights_snapshot = snap
            _log("INFO", f"✅ Insights updated: {snap}")

        except Exception as e:
            _log("INFO", f"⚠️ Insights update failed: {e}")
            cooldown = 60  # wait a bit if error

        time.sleep(INSIGHTS_REFRESH)



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
    """
    Serve cached candles only — always instant and rate-limit safe.
    The background cache thread keeps it filled automatically.
    """
    symbol = request.args.get("symbol", "BTCUSDT").upper()
    interval = request.args.get("interval", "1m")
    limit = int(request.args.get("limit", str(RETURN_CANDLES)))
    
    end_time = request.args.get("endTime")
    if end_time:
        try:
            end_time = int(end_time)
        except Exception:
            end_time = None

    end_ts = float(end_time) / 1000.0 if end_time else None

    key = (symbol, interval)
    rows = _get_cached_slice(key, end_ts=end_ts, limit=limit)

    # ⚠️ Strict cache-only: don't call Binance from here anymore
    if not rows:
        _log("INFO", f"⚠️ No cached data for {symbol} {interval}")
        return jsonify({"error": "no cached data yet"}), 503

    out = [[int(r[0] * 1000), r[1], r[2], r[3], r[4]] for r in rows]
    return jsonify({"candles": out})




@app.route("/convert")
def convert():
    """Convert any fiat currencies using the fully cached rate set."""
    from_cur = request.args.get("from", "").upper()
    to_cur = request.args.get("to", "").upper()
    amount = request.args.get("amount", type=float, default=1.0)

    with _cache_lock:
        rates = all_fiat_rates.copy() if all_fiat_rates else {}

    if not rates:
        return jsonify({"error": "no cached fiat data yet"}), 503

    if from_cur == to_cur:
        return jsonify({"from": from_cur, "to": to_cur, "amount": amount, "converted": amount})

    # Reconstruct rates relative to USD
    if from_cur == "USD":
        base_to_usd = 1.0
    elif from_cur in rates:
        base_to_usd = 1 / rates[from_cur]
    else:
        return jsonify({"error": f"unsupported base currency {from_cur}"}), 400

    if to_cur == "USD":
        usd_to_target = 1.0
    elif to_cur in rates:
        usd_to_target = rates[to_cur]
    else:
        return jsonify({"error": f"unsupported target currency {to_cur}"}), 400

    converted = amount * base_to_usd * usd_to_target
    return jsonify({
        "from": from_cur,
        "to": to_cur,
        "amount": amount,
        "converted": round(converted, 6),
        "timestamp": time.time()
    })




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
        counts = {f"{k[0]}:{k[1]}": len(v) for k, v in candle_cache.items()}
        data = {
            "keys": list(counts.keys())[:10],
            "sizes": {k: counts[k] for k in list(counts)[:10]},
            "fiat_pairs": list(fiat_board_snapshot.get("pairs", {}).keys()) if fiat_board_snapshot else [],
            "insights_ready": bool(insights_snapshot),
            "last_price_count": len(last_price),
            "thread_count": threading.active_count(),
            "timestamp": time.time(),
        }
    return jsonify(data)

@app.route("/refresh-fiat")
def refresh_fiat():
    """Manually trigger a one-time fiat board and full cache refresh (baseline vs latest)."""
    global all_fiat_rates, fiat_board_snapshot, last_fiat_rates

    try:
        _log("INFO", "🔄 Manual fiat refresh triggered…")

        # --- Determine baseline (yesterday) and latest (today) ---
        import datetime
        today = datetime.date.today()
        yesterday = today - datetime.timedelta(days=2)
        today_str = today.isoformat()
        yest_str = yesterday.isoformat()

        # --- Fetch baseline and latest from Frankfurter ---
        baseline_data = http_get_json(f"https://api.frankfurter.app/{yest_str}?from=USD", timeout=10)
        latest_data   = http_get_json(f"https://api.frankfurter.app/latest?from=USD", timeout=10)

        # --- Debug info ---
        _log("INFO", f"📅 Baseline date: {yest_str} → {baseline_data.get('date')}")
        _log("INFO", f"📅 Latest date:   {today_str} → {latest_data.get('date')}")
        sample_keys = list(latest_data.get("rates", {}))[:5]
        for k in sample_keys:
            old = baseline_data["rates"].get(k)
            new = latest_data["rates"].get(k)
            _log("INFO", f"🔍 {k}: baseline={old}, latest={new}")

        base_rates = baseline_data.get("rates", {}) or {}
        latest_rates = latest_data.get("rates", {}) or {}

        if not latest_rates:
            return jsonify({"error": "No data returned from Frankfurter"}), 503

        # ✅ Update full cache
        with _cache_lock:
            all_fiat_rates = latest_rates.copy()

        # --- Compute % change between baseline and latest ---
        pairs_out = {}
        for cur, new_val in latest_rates.items():
            old_val = base_rates.get(cur)
            if not old_val:
                change_pct = 0.0
            else:
                change_pct = ((new_val - old_val) / old_val) * 100.0

            pairs_out[f"USD_{cur}"] = {
                "baseline": old_val or new_val,
                "current": new_val,
                "change": round(change_pct, 3)
            }

        # --- Sort by absolute % change and keep top 10 ---
        top10 = dict(sorted(pairs_out.items(), key=lambda x: abs(x[1]["change"]), reverse=True)[:10])

        with _cache_lock:
            fiat_board_snapshot = {"pairs": top10, "timestamp": time.time()}

        _log("INFO", f"✅ Manual fiat refresh complete — {len(top10)} top movers ready.")
        return jsonify({
            "message": "Manual fiat refresh complete",
            "timestamp": time.time(),
            "top_movers": list(top10.keys())
        })

    except Exception as e:
        _log("INFO", f"⚠️ Manual fiat refresh failed: {e}")
        return jsonify({"error": str(e)}), 500



@app.route("/save-cache")
def save_cache():
    _save_cache()
    return jsonify({"status": "saved", "time": time.time()})


# Main
# -----
if __name__ == "__main__":
    _log("INFO", "🚀 Local start detected – loading cache and starting threads.")
    _load_cache()
    start_threads()
    _log("INFO", "✅ Threads started. Serving Flask on 0.0.0.0:8080")

    # Background periodic saver
    threading.Thread(target=lambda: (time.sleep(5), _periodic_save()), daemon=True).start()

    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)

else:
    _log("INFO", "🚀 Gunicorn/Render environment detected – loading cache and starting threads.")
    _load_cache()
    threading.Thread(target=start_threads, daemon=True).start()

    # Background periodic saver
    threading.Thread(target=lambda: (time.sleep(5), _periodic_save()), daemon=True).start()


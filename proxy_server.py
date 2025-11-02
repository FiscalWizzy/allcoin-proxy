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

FIAT_FILE = os.path.join(DATA_DIR, "fiat.json")
INSIGHTS_FILE = os.path.join(DATA_DIR, "insights.json")

all_fiat_rates: dict = {}
# dynamic crypto universe (built from Binance snapshot)
crypto_quotes_map: Dict[str, List[str]] = {}  # base -> list of quotes it trades against


# ✅ Daily baseline for 24-hour movement comparison
daily_baseline = {"timestamp": 0, "rates": {}}


# -------------
# Flask & HTTP
# -------------
app = Flask(__name__)
_session = requests.Session()

_threads_started = False


def _log(level: str, *args):
    if LOG_LEVEL == "DEBUG" or level != "DEBUG":
        print(*args, flush=True)

def http_get_json(url: str, timeout: float = 10.0, params: Optional[dict] = None):
    resp = _session.get(url, timeout=timeout, params=params)
    resp.raise_for_status()
    return resp.json()

def get_exchange_rate_cached(base: str, quote: str):
    with _cache_lock:
        rates = all_fiat_rates.copy() if all_fiat_rates else {}

    if not rates:
        return None
    
    # Direct: USD → OTHER
    if base == "USD":
        return rates.get(quote)

    # Reverse: OTHER → USD
    if quote == "USD":
        return 1 / rates.get(base, 0) if rates.get(base) else None

    # Cross: OTHER → OTHER
    usd_base = get_exchange_rate_cached(base, "USD")
    usd_quote = get_exchange_rate_cached(quote, "USD")

    if usd_base and usd_quote:
        return usd_base * usd_quote

    return None


def get_crypto_price(symbol: str) -> float | None:
    """Fetch a single crypto pair price (e.g., BTCUSDT) from Binance."""
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol.upper()}"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        return float(data["price"])
    except Exception as e:
        _log("INFO", f"⚠️ get_crypto_price failed for {symbol}: {e}")
        return None

def _binance_snapshot_loop():
    """Periodically fetch all ticker prices and build crypto_quotes_map from real Binance symbols."""
    global last_price, crypto_quotes_map
    interval = 180  # 3 min; adjust later

    while True:
        try:
            r = requests.get("https://api.binance.com/api/v3/ticker/price", timeout=10)
            data = r.json()

            lp: Dict[str, float] = {}
            cq: Dict[str, set] = {}

            for item in data:
                sym = item.get("symbol", "")
                p = item.get("price")
                if not sym or p is None:
                    continue
                try:
                    lp[sym] = float(p)
                except:
                    continue

                # discover base/quote purely from symbol by trying every split point
                # (e.g., ETHBTC -> ETH/BTC, BTCEUR -> BTC/EUR, ARBETH -> ARB/ETH)
                # keep ONLY splits where both parts are alphabetic
                for i in range(1, len(sym)):  # split between i-1|i
                    base = sym[:i]
                    quote = sym[i:]
                    if base.isalpha() and quote.isalpha():
                        cq.setdefault(base, set()).add(quote)
                        break  # first plausible split is enough for Binance symbols

            with _cache_lock:
                last_price = lp
                crypto_quotes_map = {b: sorted(list(qs)) for b, qs in cq.items()}

            _log("INFO", f"✅ Binance snapshot: {len(last_price)} prices; {len(crypto_quotes_map)} bases")

        except Exception as e:
            _log("INFO", f"⚠️ _binance_snapshot_loop failed: {e}")

        time.sleep(interval)


# -------------
# In-memory cache
# -------------
_cache_lock = threading.Lock()

# last trade price cache: last_price[symbol] = float
last_price: Dict[str, float] = {}

# Fiat board state
last_fiat_rates: Dict[str, float] = {}
fiat_board_snapshot: Dict = {}

daily_baseline: Dict = {"day": None, "timestamp": 0, "rates": {}}


# Insights snapshot
insights_snapshot: Dict = {}

def _save_cache():
    """Persist in-memory caches to disk (compact and memory-safe)."""
    with _cache_lock:
        try:
            # Save only fiat board and insights (chart caching removed)
            with open(FIAT_FILE, "w") as f:
                json.dump(fiat_board_snapshot, f, indent=None, separators=(',', ':'))

            with open(INSIGHTS_FILE, "w") as f:
                json.dump(insights_snapshot, f, indent=None, separators=(',', ':'))

            _log("INFO", "💾 Cache saved successfully (compact mode, no chart cache).")
        except Exception as e:
            _log("INFO", f"⚠️ Cache save failed: {e}")


def _load_cache():
    """Load cached fiat and insights data from disk."""
    global fiat_board_snapshot, insights_snapshot
    try:
        if os.path.exists(FIAT_FILE):
            with open(FIAT_FILE, "r") as f:
                fiat_board_snapshot = json.load(f)

        if os.path.exists(INSIGHTS_FILE):
            with open(INSIGHTS_FILE, "r") as f:
                insights_snapshot = json.load(f)

        _log("INFO", "♻️ Cache restored from disk (fiat + insights only).")
    except Exception as e:
        _log("INFO", f"⚠️ Cache load failed: {e}")



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
            _log("INFO", "⚡ Warm-up: Fetching latest USD base rates…")
            usd_data = http_get_json(f"https://api.frankfurter.app/latest?from=USD", timeout=20)
            rates = usd_data.get("rates", {}) or {}
            if not rates:
                raise ValueError("No data from Frankfurter")

            now = time.time()

            with _cache_lock:
                all_fiat_rates = rates.copy()
                # initialize fiat board without day comparison yet
                fiat_board_snapshot = {
                    "pairs": {
                        f"USD_{k}": {"current": v, "previous": v}
                        for k, v in rates.items()
                    },
                    "timestamp": now,
                }
                last_fiat_rates = rates.copy()

            _log("INFO", f"✅ Warm-up: {len(all_fiat_rates)} fiat rates loaded")

            break


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
# Thread orchestration (chart-free)
# -------------------
def start_threads():
    # --- Warm-up before starting background loops ---
    warmup_insights()
    warmup_fiat_board()

    # 🔥 Crypto backfill & candle loops REMOVED 🔥

    # Only keep the two background loops you actually need
    threading.Thread(target=_fiat_board_loop, daemon=True).start()
    threading.Thread(target=_insights_loop, daemon=True).start()
    threading.Thread(target=_binance_snapshot_loop, daemon=True).start()




def _periodic_save():
    """Periodically save only fiat + insights cache, safely and efficiently."""
    last_save_time = 0
    save_interval = 300  # 5 minutes

    while True:
        try:
            # Only save if something exists in memory
            if fiat_board_snapshot or insights_snapshot:
                _save_cache()
                last_save_time = time.time()
                _log("INFO", f"💾 Periodic save complete at {time.strftime('%H:%M:%S')}")
            else:
                _log("DEBUG", "🕓 Skipping save (no data yet).")
        except Exception as e:
            _log("INFO", f"⚠️ Periodic save failed: {e}")

        # Sleep until next save
        time.sleep(save_interval)


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

@app.route("/fiats")
def fiats():
    with _cache_lock:
        rates = all_fiat_rates.copy() if all_fiat_rates else {}
    # include USD explicitly as base
    fiats = sorted(set(["USD"] + list(rates.keys())))
    return jsonify({"fiats": fiats, "timestamp": time.time()})

@app.route("/cryptos")
def cryptos():
    with _cache_lock:
        cq = {b: qs[:] for b, qs in crypto_quotes_map.items()}
    payload = {
        "cryptos": [{"base": b, "quotes": qs} for b, qs in sorted(cq.items())],
        "timestamp": time.time(),
    }
    return jsonify(payload)


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


@app.route("/convert")
def convert():
    """Convert fiat ↔ fiat or crypto ↔ crypto/fiat using cached data ONLY."""

    from_cur = request.args.get("from", "").upper()
    to_cur = request.args.get("to", "").upper()
    amount = request.args.get("amount", type=float, default=1.0)

    if not from_cur or not to_cur:
        return jsonify({"error": "missing from/to"}), 400

    # ✅ Load cached copies
    with _cache_lock:
        fiats = all_fiat_rates.copy() if all_fiat_rates else {}
        crypto_map = {b: set(qs) for b, qs in crypto_quotes_map.items()}
        prices = last_price.copy()

    crypto_symbols = set(crypto_map.keys())

    # Helper: lookup cached Binance price
    def price_of(symbol: str) -> float | None:
        return prices.get(symbol.upper())

    # ===========================
    # ✅ Crypto Cases (dynamic!)
    # ===========================
    if from_cur in crypto_symbols or to_cur in crypto_symbols:
        try:
            # Direct route e.g. BTC→USDT => BTCUSDT
            direct = price_of(f"{from_cur}{to_cur}")
            if direct:
                return jsonify({
                    "from": from_cur, "to": to_cur,
                    "amount": amount,
                    "converted": round(amount * direct, 8)
                })

            # Reverse route e.g. USD→BTC => BTCUSD
            reverse = price_of(f"{to_cur}{from_cur}")
            if reverse:
                return jsonify({
                    "from": from_cur, "to": to_cur,
                    "amount": amount,
                    "converted": round(amount / reverse, 8)
                })

            # Crypto ↔ Crypto via USD stable (USDT)
            if from_cur in crypto_symbols and to_cur in crypto_symbols:
                base_usdt = price_of(f"{from_cur}USDT")
                tgt_usdt = price_of(f"{to_cur}USDT")
                if base_usdt and tgt_usdt:
                    converted = amount * (base_usdt / tgt_usdt)
                    return jsonify({
                        "from": from_cur, "to": to_cur,
                        "amount": amount,
                        "converted": round(converted, 8)
                    })

            # Crypto → Fiat via USDT bridge
            if from_cur in crypto_symbols and to_cur in fiats:
                base_usdt = price_of(f"{from_cur}USDT")
                usd_to_target = fiats.get(to_cur, 1)
                if base_usdt and usd_to_target:
                    converted = amount * base_usdt * usd_to_target
                    return jsonify({
                        "from": from_cur, "to": to_cur,
                        "amount": amount,
                        "converted": round(converted, 8)
                    })

            # Fiat → Crypto via USDT bridge
            if from_cur in fiats and to_cur in crypto_symbols:
                target_usdt = price_of(f"{to_cur}USDT")
                usd_to_base = 1 / fiats.get(from_cur, 1)
                if target_usdt and usd_to_base:
                    converted = amount * usd_to_base / target_usdt
                    return jsonify({
                        "from": from_cur, "to": to_cur,
                        "amount": amount,
                        "converted": round(converted, 8)
                    })

            return jsonify({"error": f"no cached price for {from_cur}/{to_cur}"}), 503

        except Exception as e:
            return jsonify({"error": f"crypto convert failed: {e}"}), 500

    # ===========================
    # ✅ Pure Fiat Cases
    # ===========================
    if not fiats:
        return jsonify({"error": "no cached fiat data yet"}), 503

    if from_cur == to_cur:
        return jsonify({
            "from": from_cur,
            "to": to_cur,
            "amount": amount,
            "converted": amount
        })

    # Make both relative to USD
    if from_cur == "USD":
        base_to_usd = 1.0
    elif from_cur in fiats:
        base_to_usd = 1 / fiats[from_cur]
    else:
        return jsonify({"error": f"unsupported base {from_cur}"}), 400

    if to_cur == "USD":
        usd_to_target = 1.0
    elif to_cur in fiats:
        usd_to_target = fiats[to_cur]
    else:
        return jsonify({"error": f"unsupported target {to_cur}"}), 400

    converted = amount * base_to_usd * usd_to_target
    return jsonify({
        "from": from_cur,
        "to": to_cur,
        "amount": amount,
        "converted": round(converted, 6)
    })



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
        yesterday = today - datetime.timedelta(days=1)
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
    _log("INFO", "🚀 Gunicorn/Render environment detected – loading cache only (threads already running).")
    _load_cache()
    _log("INFO", "✅ Using existing cached data; background threads managed externally.")



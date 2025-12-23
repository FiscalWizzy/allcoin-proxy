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
_threads_lock = threading.Lock()

# ---- Fiat name map (server-side cache) ----
fiat_names_map: dict[str, str] = {}   # e.g. {"EUR": "Euro", "USD": "US Dollar"}


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

def _prime_binance_snapshot_once():
    """Fetch Binance exchangeInfo + prices once and build crypto_quotes_map + last_price."""
    global last_price, crypto_quotes_map

    # 1) Get all valid symbols with true base/quote
    info_r = requests.get("https://api.binance.com/api/v3/exchangeInfo", timeout=10)
    info_r.raise_for_status()
    info = info_r.json()

    symbols = info.get("symbols", [])
    if not isinstance(symbols, list):
        raise ValueError("Binance exchangeInfo returned invalid payload")

    cq: Dict[str, set] = {}
    valid_pairs = set()

    for s in symbols:
        if s.get("status") != "TRADING":
            continue
        base = s.get("baseAsset")
        quote = s.get("quoteAsset")
        sym = s.get("symbol")
        if base and quote and sym:
            cq.setdefault(base, set()).add(quote)
            valid_pairs.add(sym)

    # 2) Get latest prices
    price_r = requests.get("https://api.binance.com/api/v3/ticker/price", timeout=10)
    price_r.raise_for_status()
    data = price_r.json()
    if not isinstance(data, list):
        raise ValueError("Binance price endpoint returned non-list payload")

    lp: Dict[str, float] = {}
    for item in data:
        sym = item.get("symbol")
        p = item.get("price")
        if sym in valid_pairs and p is not None:
            try:
                lp[sym] = float(p)
            except Exception:
                pass

    # 3) Store
    with _cache_lock:
        last_price = lp
        crypto_quotes_map = {b: sorted(list(qs)) for b, qs in cq.items()}

    _log("INFO", f"✅ Binance snapshot: {len(last_price)} prices; {len(crypto_quotes_map)} bases")



def _binance_snapshot_loop():
    """Periodic refresh of Binance snapshot."""
    interval = 180  # 3 min
    while True:
        try:
            _prime_binance_snapshot_once()
        except Exception as e:
            _log("INFO", f"⚠️ _binance_snapshot_loop failed: {e}")
        time.sleep(interval)

def _binance_24h(symbol: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Returns (last_price, pct_change_24h) from Binance ticker/24hr.
    """
    try:
        url = f"{BINANCE_API}/api/v3/ticker/24hr"
        r = requests.get(url, params={"symbol": symbol.upper()}, timeout=10)
        r.raise_for_status()
        j = r.json()
        last_price = float(j.get("lastPrice")) if j.get("lastPrice") else None
        pct = float(j.get("priceChangePercent")) if j.get("priceChangePercent") else None
        return last_price, pct
    except Exception:
        return None, None


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
                json.dump(t, f, indent=None, separators=(',', ':'))

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



# NEW: previous poll snapshot for intraday comparison

prev_rates_intraday: Dict[str, float] = {}


def _fiat_board_loop():
    """
    Fetch latest USD rates.
    Compare against previous-day baseline (yesterday or last available trading day).
    Do NOT reset baseline to today's rates unless the 'date' changes on Frankfurter.
    """
    global all_fiat_rates, fiat_board_snapshot, daily_baseline

    last_latest_date = None  # Frankfurter 'date' string we last saw

    while True:
        try:
            latest = http_get_json(f"{EXCHANGE_RATE_API_URL}/latest?from=USD", timeout=10)
            rates = latest.get("rates", {}) or {}
            latest_date = latest.get("date")  # e.g. "2025-11-23"

            if not rates:
                raise ValueError("Empty data from Frankfurter")

            now = time.time()

            # If first run OR Frankfurter date advanced, refresh baseline from previous day
            if (not daily_baseline.get("rates")) or (latest_date != last_latest_date):
                # try to pull previous day from Frankfurter
                from datetime import date, timedelta
                prev_day = (date.fromisoformat(latest_date) - timedelta(days=1)).isoformat()

                try:
                    prev = http_get_json(f"{EXCHANGE_RATE_API_URL}/{prev_day}?from=USD", timeout=10)
                    prev_rates = prev.get("rates", {}) or {}
                    if not prev_rates:
                        raise ValueError("prev day empty")
                except Exception as e:
                    _log("INFO", f"⚠️ Could not fetch prev day {prev_day}, using last baseline if any: {e}")
                    prev_rates = daily_baseline.get("rates") or rates.copy()

                daily_baseline = {
                    "day": prev_day,
                    "timestamp": now,
                    "rates": prev_rates.copy(),
                }
                last_latest_date = latest_date
                _log("INFO", f"🕓 Baseline set to {prev_day} for latest date {latest_date}")

            # Compute % change vs baseline
            changes = {}
            base_rates = daily_baseline["rates"]
            for cur, val in rates.items():
                base_val = base_rates.get(cur, val)
                pct = ((val / base_val) - 1) * 100 if base_val else 0.0
                changes[cur] = {"current": val, "baseline": base_val, "change": pct, "basis": "24h"}

            # Top-10 movers by absolute change
            top10 = sorted(changes.items(), key=lambda kv: abs(kv[1]["change"]), reverse=True)[:10]
            pairs_out = {f"USD_{k}": v for k, v in top10}

            with _cache_lock:
                all_fiat_rates = rates.copy()
                fiat_board_snapshot = {"pairs": pairs_out, "timestamp": now}

            _log("INFO", f"✅ Cached {len(all_fiat_rates)} rates; top-10 movers ready")

        except Exception as e:
            _log("INFO", f"⚠️ Fiat board update failed: {e}")

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

            # --- Crypto (Binance 24h) ---
            btc_usd, btc_chg = _binance_24h("BTCUSDT")
            eth_usd, eth_chg = _binance_24h("ETHUSDT")

            # --- EUR/USD (use your existing cached helper) ---
            # If you prefer the "safe EUR/USD" version, tell me and I’ll paste it here.
            eur_usd = get_exchange_rate_cached("EUR", "USD")

            # --- Core indices & assets from Yahoo (price + % vs prev close) ---
            dax_val, dax_chg = _get_yahoo_price_and_change("^GDAXI")
            sp500_val, sp500_chg = _get_yahoo_price_and_change("^GSPC")
            ftse100_val, ftse100_chg = _get_yahoo_price_and_change("^FTSE")

            apple, apple_chg = _get_yahoo_price_and_change("AAPL")
            nvda, nvda_chg = _get_yahoo_price_and_change("NVDA")
            msft, msft_chg = _get_yahoo_price_and_change("MSFT")
            blackrock, blackrock_chg = _get_yahoo_price_and_change("BLK")

            crude, crude_chg = _get_yahoo_price_and_change("CL=F")
            gold, gold_chg = _get_yahoo_price_and_change("GC=F")

            tesla, tesla_chg = _get_yahoo_price_and_change("TSLA")
            palantir, palantir_chg = _get_yahoo_price_and_change("PLTR")

            # --- Extra watchlist (looped keys: key + key_chg) ---
            extra = {}
            extra_chg = {}
            for key, sym in YAHOO_WATCHLIST.items():
                p, c = _get_yahoo_price_and_change(sym)
                extra[key] = p
                extra_chg[f"{key}_chg"] = c

            # If you're still using the "strict warmup", keep it.
            # I recommend only requiring BTC/ETH so warmup never blocks forever.
            if not btc_usd or not eth_usd:
                raise ValueError("Core warm-up values missing (BTC/ETH)")

            with _cache_lock:
                insights_snapshot = {
                    # prices
                    "btc_usd": btc_usd,
                    "eth_usd": eth_usd,
                    "eur_usd": eur_usd,

                    "dax": dax_val,
                    "sp500": sp500_val,
                    "ftse100": ftse100_val,

                    "apple": apple,
                    "nvda": nvda,
                    "msft": msft,
                    "blackrock": blackrock,

                    "crude": crude,
                    "gold": gold,

                    "tesla": tesla,
                    "palantir": palantir,

                    # % changes
                    "btc_chg": btc_chg,
                    "eth_chg": eth_chg,

                    "dax_chg": dax_chg,
                    "sp500_chg": sp500_chg,
                    "ftse100_chg": ftse100_chg,

                    "apple_chg": apple_chg,
                    "nvda_chg": nvda_chg,
                    "msft_chg": msft_chg,
                    "blackrock_chg": blackrock_chg,

                    "crude_chg": crude_chg,
                    "gold_chg": gold_chg,

                    "tesla_chg": tesla_chg,
                    "palantir_chg": palantir_chg,

                    # extras
                    **extra,
                    **extra_chg,

                    "timestamp": time.time(),
                }

            _log(
                "INFO",
                f"✅ Warm-up complete — BTC={btc_usd} ({btc_chg}%), ETH={eth_usd} ({eth_chg}%), EUR/USD={eur_usd}, DAX={dax_val} ({dax_chg}%)"
            )
            break  # ✅ success → stop retrying

        except Exception as e:
            _log("INFO", f"⚠️ Warm-up failed: {e} — retrying in {retry_delay}s")
            time.sleep(retry_delay)



def warmup_fiat_board():
    """
    Startup warm-up for the fiat board:
    - fetch USD base 'latest' and (if possible) yesterday
    - compute % change vs baseline
    - build Top-10 movers into fiat_board_snapshot['pairs']
    - seed caches (all_fiat_rates, last_fiat_rates, prev_rates_intraday)
    """
    global fiat_board_snapshot, last_fiat_rates, all_fiat_rates, daily_baseline, prev_rates_intraday
    retry_delay = 120  # seconds

    while True:
        try:
            _log("INFO", "⚡ Running startup warm-up for fiat board…")

            # Fetch latest USD base rates
            latest = http_get_json("https://api.frankfurter.app/latest?from=USD", timeout=10)
            latest_rates = latest.get("rates", {}) or {}
            if not latest_rates:
                raise ValueError("Empty latest rates from Frankfurter")

            # Try to fetch yesterday for 24h baseline
            from datetime import date, timedelta
            yest_str = (date.today() - timedelta(days=1)).isoformat()
            try:
                yest = http_get_json(f"https://api.frankfurter.app/{yest_str}?from=USD", timeout=10)
                baseline_rates = yest.get("rates", {}) or {}
            except Exception as e:
                _log("INFO", f"⚠️ Could not fetch yesterday ({yest_str}); using latest as baseline: {e}")
                baseline_rates = latest_rates.copy()

            # Compute 24h % change vs baseline
            entries = []
            for cur, new_val in latest_rates.items():
                old_val = baseline_rates.get(cur, new_val)
                change_pct = ((new_val / old_val) - 1.0) * 100.0 if old_val else 0.0
                entries.append((cur, new_val, change_pct))

            # Top 10 movers by absolute % change
            top10 = sorted(entries, key=lambda x: abs(x[2]), reverse=True)[:10]
            pairs_out = {
                f"USD_{cur}": {"current": val, "change": round(pct, 3), "basis": "24h"}
                for (cur, val, pct) in top10
            }

            now = time.time()
            # Seed caches so later loops & /convert work immediately
            with _cache_lock:
                all_fiat_rates = latest_rates.copy()
                last_fiat_rates = {f"USD_{cur}": latest_rates[cur] for cur in latest_rates}
                prev_rates_intraday = latest_rates.copy()
                daily_baseline = {
                    "day": date.today().isoformat(),
                    "timestamp": now,
                    "rates": baseline_rates.copy(),
                }
                fiat_board_snapshot = {"pairs": pairs_out, "timestamp": now}

            _log("INFO", f"✅ Fiat board warm-up complete — {len(pairs_out)} pairs in snapshot.")
            return  # success

        except Exception as e:
            _log("INFO", f"⚠️ Fiat board warm-up failed: {e} — retrying in {retry_delay}s")
            time.sleep(retry_delay)




def _fiat_names_warmup():
    global fiat_names_map
    try:
        j = http_get_json(f"{EXCHANGE_RATE_API_URL}/currencies", timeout=10)
        if isinstance(j, dict):
            fiat_names_map = {k.upper(): v for k, v in j.items()}
            _log("INFO", f"✅ Loaded {len(fiat_names_map)} fiat names")
    except Exception as e:
        _log("INFO", f"⚠️ _fiat_names_warmup failed: {e}")


def _ensure_fiat_names_loaded():
    global fiat_names_map
    if fiat_names_map:
        return
    try:
        j = http_get_json(f"{EXCHANGE_RATE_API_URL}/currencies", timeout=10)  # {"USD":"US Dollar",...}
        if isinstance(j, dict) and j:
            with _cache_lock:
                fiat_names_map = j.copy()
            _log("INFO", f"✅ Loaded {len(fiat_names_map)} fiat names")
        else:
            _log("INFO", "⚠️ /currencies returned empty; using code-only labels")
    except Exception as e:
        _log("INFO", f"⚠️ Could not load fiat names: {e} — using code-only labels")


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
    """Kick off warmups concurrently, then start loops. Never block import."""
    def bootstrap():
        # run warmups in parallel so a slow one doesn't block the other
        t_ins = threading.Thread(target=warmup_insights, daemon=True)
        t_fiat = threading.Thread(target=warmup_fiat_board, daemon=True)
        t_ins.start(); t_fiat.start()

        # (optional) wait a little, but never block forever
        t_ins.join(timeout=15)
        t_fiat.join(timeout=15)

        # start the recurring loops regardless; endpoints can serve 503 until warm data exists
        threading.Thread(target=_insights_loop, daemon=True).start()
        threading.Thread(target=_fiat_board_loop, daemon=True).start()

        # only start if you actually implemented this loop; otherwise remove this line
        try:
            threading.Thread(target=_binance_snapshot_loop, daemon=True).start()
        except NameError:
            pass

    threading.Thread(target=bootstrap, daemon=True).start()




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

def ensure_background_threads():
    global _threads_started
    if _threads_started:
        return
    with _threads_lock:
        if _threads_started:
            return
        _threads_started = True
        threading.Thread(target=start_threads, daemon=True).start()

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
    global insights_snapshot
    cooldown = 0

    while True:
        try:
            if cooldown > 0:
                _log("INFO", f"🕒 Cooling down {cooldown}s to respect limits")
                time.sleep(cooldown)
                cooldown = 0

            # --- Crypto (Binance 24h) ---
            btc_usd, btc_chg = _binance_24h("BTCUSDT")
            eth_usd, eth_chg = _binance_24h("ETHUSDT")

            # --- EUR/USD ---
            eur_usd = get_exchange_rate_cached("EUR", "USD")

            # --- Yahoo (price + % vs prev close) ---
            dax_val, dax_chg = _get_yahoo_price_and_change("^GDAXI")
            sp500_val, sp500_chg = _get_yahoo_price_and_change("^GSPC")
            ftse100_val, ftse100_chg = _get_yahoo_price_and_change("^FTSE")

            apple, apple_chg = _get_yahoo_price_and_change("AAPL")
            nvda, nvda_chg = _get_yahoo_price_and_change("NVDA")
            msft, msft_chg = _get_yahoo_price_and_change("MSFT")
            blackrock, blackrock_chg = _get_yahoo_price_and_change("BLK")

            crude, crude_chg = _get_yahoo_price_and_change("CL=F")
            gold, gold_chg = _get_yahoo_price_and_change("GC=F")

            tesla, tesla_chg = _get_yahoo_price_and_change("TSLA")
            palantir, palantir_chg = _get_yahoo_price_and_change("PLTR")

            # --- Extras ---
            extra = {}
            extra_chg = {}
            for key, sym in YAHOO_WATCHLIST.items():
                p, c = _get_yahoo_price_and_change(sym)
                extra[key] = p
                extra_chg[f"{key}_chg"] = c

            snap = {
                # prices
                "btc_usd": btc_usd,
                "eth_usd": eth_usd,
                "eur_usd": eur_usd,

                "dax": dax_val,
                "sp500": sp500_val,
                "ftse100": ftse100_val,

                "apple": apple,
                "nvda": nvda,
                "msft": msft,
                "blackrock": blackrock,

                "crude": crude,
                "gold": gold,

                "tesla": tesla,
                "palantir": palantir,

                # % changes
                "btc_chg": btc_chg,
                "eth_chg": eth_chg,

                "dax_chg": dax_chg,
                "sp500_chg": sp500_chg,
                "ftse100_chg": ftse100_chg,

                "apple_chg": apple_chg,
                "nvda_chg": nvda_chg,
                "msft_chg": msft_chg,
                "blackrock_chg": blackrock_chg,

                "crude_chg": crude_chg,
                "gold_chg": gold_chg,

                "tesla_chg": tesla_chg,
                "palantir_chg": palantir_chg,

                # extras
                **extra,
                **extra_chg,

                "timestamp": time.time(),
            }

            with _cache_lock:
                insights_snapshot = snap

            _log("INFO", "✅ Insights updated (price + % change)")

        except Exception as e:
            _log("INFO", f"⚠️ Insights update failed: {e}")
            cooldown = 60

        time.sleep(INSIGHTS_REFRESH)


# --- Extra AI/tech + materials watchlist (Yahoo symbols) ---
YAHOO_WATCHLIST = {
    # Metals / materials influencing tech
    "copper":   "HG=F",
    "silver":   "SI=F",
    "palladium":"PA=F",
    "platinum": "PL=F",
    "rare_earth_etf": "REMX",

    # “Gemini stock” (Alphabet / Google)
    "googl": "GOOGL",

    # US AI / chip supply chain
    "meta": "META",
    "amzn": "AMZN",
    "asml": "ASML",
    "tsm":  "TSM",
    "amd":  "AMD",
    "avgo": "AVGO",
    "smci": "SMCI",
    "snow": "SNOW",

    # China / ADRs that matter for tech + AI
    "baba":  "BABA",
    "tcehy": "TCEHY",
    "bidu":  "BIDU",
    "smic": "0981.HK",  # SMIC ADR (often more stable than HK symbol formatting)
    "nio":   "NIO",
    "byd":   "BYDDY",  # BYD ADR (HK ticker formatting can be annoying)
}


def _get_yahoo_last(symbol: str) -> Optional[float]:
    """Fetch last price for a Yahoo Finance symbol (e.g. ^GSPC, AAPL, CL=F)."""
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={"interval": "1h"},
        headers=headers,
        timeout=8,
    )
    j = r.json()
    try:
        return (
            j.get("chart", {})
             .get("result", [{}])[0]
             .get("meta", {})
             .get("regularMarketPrice")
        )
    except Exception:
        return None

def _get_yahoo_price_and_change(symbol: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Returns:
      (price, pct_change)
    pct_change is computed vs previous close if available.
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={"interval": "1d", "range": "5d"},
        headers=headers,
        timeout=8,
    )
    j = r.json()

    try:
        result = j.get("chart", {}).get("result", [{}])[0]
        meta = result.get("meta", {}) or {}

        price = meta.get("regularMarketPrice")
        prev = meta.get("previousClose")

        # Some tickers use different meta fields sometimes
        if prev is None:
            prev = meta.get("chartPreviousClose")

        if price is None:
            return None, None

        pct = None
        if prev not in (None, 0):
            pct = ((float(price) / float(prev)) - 1.0) * 100.0

        return float(price), (float(pct) if pct is not None else None)

    except Exception:
        return None, None


# -----------
# HTTP routes
# -----------
@app.route("/health")
def health():

    return jsonify({"ok": True, "time": time.time()})


@app.route("/fiats")
def fiats():
    ensure_background_threads()

    global all_fiat_rates, fiat_names_map

    # 1) Ensure we have names (best effort; code still works without)
    _ensure_fiat_names_loaded()

    # 2) Ensure we have the rate cache at least once so we know which fiats exist
    if not all_fiat_rates:
        try:
            data = http_get_json(f"{EXCHANGE_RATE_API_URL}/latest?from=USD", timeout=10)
            rates = data.get("rates", {}) or {}
            with _cache_lock:
                all_fiat_rates = rates.copy()
            _log("INFO", f"✅ Cached {len(all_fiat_rates)} fiat rates")
        except Exception as e:
            return jsonify({"fiats": [], "items": [], "error": f"fetch failed: {e}"}), 503

    # 3) Build response: codes + rich items
    with _cache_lock:
        codes = sorted(list(all_fiat_rates.keys())) or []

    items = []
    for c in codes:
        name = fiat_names_map.get(c, c)
        items.append({
            "code": c,
            "name": name,
            "label": f"{name} ({c})"
        })

    return jsonify({
        "fiats": codes,
        "items": items,
        "timestamp": time.time()
    })



@app.before_request
def _ensure_bg():
    ensure_background_threads()


@app.route("/crypto-bases")
def crypto_bases():
    """List all available crypto base symbols."""
    ensure_background_threads()

    # If the universe is empty, try a one-shot prime
    with _cache_lock:
        empty = not bool(crypto_quotes_map)

    if empty:
        try:
            _prime_binance_snapshot_once()
        except Exception as e:
            return jsonify({"bases": [], "error": f"warmup failed: {e}"}), 503

    with _cache_lock:
        bases = sorted(list(crypto_quotes_map.keys()))
    return jsonify({"bases": bases, "timestamp": time.time()})



@app.route("/crypto-quotes")
def crypto_quotes():
    """List valid quote symbols for a given base."""
    ensure_background_threads()

    base = (request.args.get("base") or "").upper()
    with _cache_lock:
        quotes = crypto_quotes_map.get(base, [])
    if not quotes:
        return jsonify({"base": base, "quotes": [], "error": "unknown base"}), 404
    return jsonify({"base": base, "quotes": quotes, "timestamp": time.time()})



@app.route("/cryptos")
def cryptos():
    ensure_background_threads()

    with _cache_lock:
        cq = {b: qs[:] for b, qs in crypto_quotes_map.items()}
    payload = {
        "cryptos": [{"base": b, "quotes": qs} for b, qs in sorted(cq.items())],
        "timestamp": time.time(),
    }
    return jsonify(payload)


@app.route("/fiat-board")
def fiat_board():
    ensure_background_threads()

    global fiat_board_snapshot, all_fiat_rates

    with _cache_lock:
        snapshot = fiat_board_snapshot.copy() if fiat_board_snapshot else {}

    # ✅ If cache empty → fetch live data now and populate snapshot
    if not snapshot:
        try:
            data = http_get_json(f"{EXCHANGE_RATE_API_URL}/latest?from=USD", timeout=10)
            rates = data.get("rates", {})
            if rates:
                with _cache_lock:
                    all_fiat_rates = rates.copy()
                    fiat_board_snapshot = {
                        "pairs": {f"USD_{k}": {"current": v, "change": 0.0}
                                  for k, v in rates.items()},
                        "timestamp": time.time()
                    }
                snapshot = fiat_board_snapshot.copy()
        except Exception:
            return jsonify({"error": "no cached data yet"}), 503

    return jsonify(snapshot)


@app.route("/insights")
def insights():
    ensure_background_threads()

    with _cache_lock:
        data = insights_snapshot.copy() if insights_snapshot else None
    if not data:
        return jsonify({"error": "no cached data yet"}), 503
    return jsonify(data)


@app.route("/convert")
def convert():
    ensure_background_threads()

    global all_fiat_rates  
    """Convert fiat ↔ fiat or crypto ↔ fiat using cached Frankfurter + Binance prices."""
    from_cur = request.args.get("from", "").upper()
    to_cur = request.args.get("to", "").upper()
    amount = request.args.get("amount", type=float, default=1.0)

    with _cache_lock:
        crypto_bases_set  = set(crypto_quotes_map.keys())
        crypto_quotes_set = set(q for qs in crypto_quotes_map.values() for q in qs)
    crypto_symbols = crypto_bases_set | crypto_quotes_set

    # ✅ Fiat universe from Frankfurter cache (+ USD)
    with _cache_lock:
        fiat_symbols = set(all_fiat_rates.keys()) | {"USD"}

    # Helper: fetch crypto price from Binance
    def get_crypto_price(symbol: str) -> float | None:
        try:
            url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol.upper()}"
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                return float(r.json().get("price", 0))
        except Exception as e:
            _log("INFO", f"⚠️ get_crypto_price failed: {e}")
        return None

    # ✅ Case 1: crypto involved (BUT NOT pure fiat)
    if (from_cur in crypto_symbols or to_cur in crypto_symbols) and not (
        from_cur in fiat_symbols and to_cur in fiat_symbols
    ):
        try:
            # Direct pair attempt
            direct = get_crypto_price(f"{from_cur}{to_cur}")
            if direct:
                return jsonify({"from": from_cur, "to": to_cur,
                                "amount": amount,
                                "converted": round(amount * direct, 8)})

            # Reverse pair attempt
            reverse = get_crypto_price(f"{to_cur}{from_cur}")
            if reverse:
                return jsonify({"from": from_cur, "to": to_cur,
                                "amount": amount,
                                "converted": round(amount / reverse, 8)})

            # Bridge crypto↔crypto through USDT
            if from_cur in crypto_symbols and to_cur in crypto_symbols:
                b2usdt = get_crypto_price(f"{from_cur}USDT")
                usdt2t = get_crypto_price(f"{to_cur}USDT")
                if b2usdt and usdt2t:
                    return jsonify({"from": from_cur, "to": to_cur,
                                    "amount": amount,
                                    "converted": round(amount * (b2usdt / usdt2t), 8)})

            # Bridge crypto↔fiat through USDT
            with _cache_lock:
                rates = all_fiat_rates.copy() if all_fiat_rates else {}

            # Lazy fill fiat cache (for crypto↔fiat)
            if not rates:
                data = http_get_json(f"{EXCHANGE_RATE_API_URL}/latest?from=USD", timeout=10)
                rates = data.get("rates", {})
                if rates:
                    with _cache_lock:
                        all_fiat_rates = rates.copy()

            if from_cur in crypto_symbols:  # crypto → fiat
                b2usdt = get_crypto_price(f"{from_cur}USDT")
                usd_to_t = rates.get(to_cur)
                if b2usdt and usd_to_t:
                    return jsonify({"from": from_cur, "to": to_cur,
                                    "amount": amount,
                                    "converted": round(amount * b2usdt * usd_to_t, 8)})

            if to_cur in crypto_symbols:  # fiat → crypto
                t2usdt = get_crypto_price(f"{to_cur}USDT")
                usd_to_b = 1 / rates.get(from_cur, 1)
                if t2usdt and usd_to_b:
                    return jsonify({"from": from_cur, "to": to_cur,
                                    "amount": amount,
                                    "converted": round(amount * usd_to_b / t2usdt, 8)})

            return jsonify({"error": f"No price available for {from_cur}/{to_cur}"}), 503

        except Exception as e:
            return jsonify({"error": f"Crypto conversion failed: {e}"}), 500

    # ✅ Case 2: pure fiat conversion — guaranteed by fallback below
    with _cache_lock:
        rates = all_fiat_rates.copy() if all_fiat_rates else {}

    # If cache empty → fetch **once** now (non-blocking fallback)
    if not rates:
        try:
            data = http_get_json(f"{EXCHANGE_RATE_API_URL}/latest?from=USD", timeout=10)
            rates = data.get("rates", {})
            if rates:
                with _cache_lock:
                    all_fiat_rates = rates.copy()
        except Exception as e:
            return jsonify({"error": f"fiat unavailable: {e}"}), 503

    if not rates:
        return jsonify({"error": "fiat unavailable"}), 503

    if from_cur == to_cur:
        return jsonify({"from": from_cur, "to": to_cur,
                        "amount": amount, "converted": amount})

    if from_cur != "USD" and from_cur not in rates:
        return jsonify({"error": f"Unknown fiat: {from_cur}"}), 400
    if to_cur != "USD" and to_cur not in rates:
        return jsonify({"error": f"Unknown fiat: {to_cur}"}), 400


    # Reconstruct via USD (Frankfurter format)
    base_to_usd = 1.0 if from_cur == "USD" else (1 / rates.get(from_cur, 1))
    usd_to_target = 1.0 if to_cur == "USD" else rates.get(to_cur, 1)

    converted = amount * base_to_usd * usd_to_target
    return jsonify({"from": from_cur, "to": to_cur,
                    "amount": amount,
                    "converted": round(converted, 6),
                    "timestamp": time.time()})



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
    ensure_background_threads()

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



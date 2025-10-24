import sys
import requests
from urllib.request import urlopen
from io import BytesIO
import pyqtgraph as pg
import datetime
import math
import os
import webview
import subprocess
import time
import feedparser

from PyQt6.QtWebEngineCore import QWebEngineSettings
from PyQt6.QtWebEngineWidgets import QWebEngineView

# os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--enable-logging --v=1"

from PyQt6.QtWidgets import QApplication 
app = QApplication(sys.argv)

from PyQt6.QtWidgets import QCompleter, QAbstractItemView, QToolButton
from pyqtgraph.graphicsItems.DateAxisItem import DateAxisItem
from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtWidgets import QSizePolicy, QStackedWidget, QFrame, QTextBrowser  
from pyqtgraph import InfiniteLine, TextItem
from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QComboBox,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QGroupBox,
    QGridLayout,
    QHeaderView
)

ICON_MAP = {
    "BTC/USD": "₿",
    "ETH/USD": "Ξ",
    "EUR/USD": "€/$",
    "DAX": "📊",
    "S&P 500": "📈",
    "FTSE 100": "🇬🇧",
    "Apple": "",
    "Nvidia": "🟩",
    "Crude Oil": "🛢️",
    "Gold": "🥇",
    "Microsoft": "🪟", 
    "BlackRock": "🏦", 
}


API_KEY = 'e351e54a567119afe9bb037d'
BASE_URL = "https://allcoin-proxy.onrender.com"

symbol_to_id = {}
fiat_list = []
crypto_list = []
coingecko_supported_fiats = []
last_pan_time = 0
pending_scroll_load = False
current_ohlc = []  # store the most recent candle data for locking/scrolling

mode = "fiat"   # "fiat" | "crypto" | "global"

previous_rates = {}      # tracks last fiat board values

current_candle_item = None
last_candle_ts = None     # last candle open time (seconds)
auto_range_needed = True  # only auto-range when pair/interval changes
MAX_CANDLES = 500         # limit for speed


from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLineEdit, QListView
# --- Async worker for background tasks ---
from PyQt6.QtCore import QRunnable, QThreadPool, pyqtSignal, QObject, pyqtSlot
SHUTTING_DOWN = False

class WorkerSignals(QObject):
    result = pyqtSignal(object)
    error  = pyqtSignal(Exception)

class Worker(QRunnable):
    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    @pyqtSlot()
    def run(self):
        """Execute the job in a background thread.

        Guards against late signal emissions when the app is
        shutting down, which prevents the
        'wrapped C/C++ object of type WorkerSignals has been deleted'
        RuntimeError.
        """
        if SHUTTING_DOWN:
            return
        try:
            out = self.fn(*self.args, **self.kwargs)
            if not SHUTTING_DOWN and hasattr(self, 'signals'):
                try:
                    self.signals.result.emit(out)
                except RuntimeError:
                    # signals object was already deleted during shutdown
                    pass
        except Exception as e:
            if not SHUTTING_DOWN and hasattr(self, 'signals'):
                try:
                    self.signals.error.emit(e)
                except RuntimeError:
                    # signals object was already deleted during shutdown
                    pass



thread_pool = QThreadPool.globalInstance()

# Reuse one Requests session for speed
_requests_session = requests.Session()


def boundingRect(self):
    br = self.picture.boundingRect()
    return QRectF(br)

def extract_symbol(combo_text):
    """
    Given a string like 'Bitcoin (BTC)', returns 'BTC'.
    """
    if "(" in combo_text and ")" in combo_text:
        return combo_text.split("(")[-1].replace(")", "").strip()
    return combo_text.strip()


class SearchableComboBox(QComboBox):
    def __init__(self):
        super().__init__()
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setMinimumWidth(200)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMaxVisibleItems(20)
        self.setView(QListView())

        # Fixes search: Match partial text anywhere in item
        completer = self.lineEdit().completer()
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)


def format_amount_input(text):
    try:
        clean = text.replace(",", "")
        if clean:
            if "." in clean:
                whole, frac = clean.split(".", 1)
                formatted = f"{int(whole or 0):,}.{frac}"
            else:
                formatted = f"{int(clean):,}"

            amount_input_fiat.blockSignals(True)
            amount_input_fiat.setText(formatted)
            amount_input_fiat.blockSignals(False)
            amount_input_fiat.setCursorPosition(len(formatted))
    except ValueError:
        pass


def populate_currency_dropdowns():
    global fiat_list
    try:
        resp = requests.get("https://api.frankfurter.app/currencies", timeout=30)
        data = resp.json()
        fiat_list = sorted([f"{name} ({code})" for code, name in data.items()])
    except Exception as e:
        print(f"⚠️ Error loading fiat currencies: {e}")
        fiat_list = []

    from_currency_fiat.addItems(fiat_list)
    to_currency_fiat.addItems(fiat_list)


def fetch_supported_fiats_from_coingecko():
    global coingecko_supported_fiats
    url = "https://api.coingecko.com/api/v3/simple/supported_vs_currencies"
    try:
        response = requests.get(url)
        if response.status_code == 200:
            data = response.json()
            coingecko_supported_fiats = sorted([cur.upper() for cur in data])
        else:
            print("Failed to fetch CoinGecko supported fiats.")
    except Exception as e:
        print("Error fetching CoinGecko fiat list:", e)


def populate_crypto_dropdowns():
    global crypto_list, symbol_to_id
    url = "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page=1"
    try:
        response = requests.get(url)
        data = response.json()
        seen = set()

        for coin in data:
            symbol = coin["symbol"].upper()
            coin_id = coin["id"]
            price = coin.get("current_price")
            image = coin.get("image")  # ✅ <-- NEW: get logo URL

            if symbol not in seen and price and image:  # ✅ ensure image is valid
                seen.add(symbol)
                symbol_to_id[symbol] = {
                    "id": coin_id,
                    "price": price,
                    "image": image,  # ✅ <-- NEW: store logo
                    "name": coin["name"]
                }
                crypto_list.append(f"{coin['name']} ({symbol})")

        crypto_list.sort()
    except Exception as e:
        result_label.setText(f"Error loading crypto data: {e}")


import ctypes
ctypes.windll.shcore.SetProcessDpiAwareness(1)


from PyQt6.QtGui import QFont

# Set clean sans-serif for all UI
app.setFont(QFont("Segoe UI", 10))  # or "Inter" if you install it

# Deep navy theme
app.setStyleSheet("""
    QWidget {
        background-color: #0d1b2a;   /* deep navy background */
        color: #e0e0e0;
        font-family: "Consolas", monospace;
        font-size: 14px;
    }

    /* Input fields and buttons */
    QLineEdit, QComboBox, QPushButton {
        background-color: #14273d;
        color: #e0e0e0;
        border: 1px solid #1f2f47;
        border-radius: 6px;
        padding: 6px;
    }

    QLineEdit:focus, QComboBox:focus, QPushButton:hover {
        border: 1px solid #FFD700;   /* gold highlight */
        background-color: #1b324d;
    }

    /* Buttons hover effect */
    QPushButton:hover {
        color: #FFD700;
    }

    /* Table */
    QTableWidget {
        font-family: "Consolas", monospace;
        font-size: 16px;
        gridline-color: #2e3b4e;
        background-color: #0d1b2a;
        color: #e0e0e0;
        selection-background-color: transparent;
        alternate-background-color: #14273d; /* striped rows */
        border: 1px solid #1a2a3a;
    }

    /* Table header */
    QHeaderView::section {
        background-color: #1b263b;
        color: #FFD700;  /* gold LED */
        font-weight: bold;
        font-size: 14px;
        border: none;
        padding: 6px;
    }

    /* Table rows */
    QTableWidget::item {
        border-bottom: 1px solid #1f2f47;
    }

    /* Hover effect on rows */
    QTableWidget::item:hover {
        background-color: #23344e;
    }

""")

# --- Global headline style (reusable for all section titles) ---
GOLD_HEADER_STYLE = "font-weight: bold; font-size: 16px; color: #FFD700;"


# Result Display
result_label = QLabel("")


result_label.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))


# New: Icon display (initially empty)
icon_label = QLabel()

class CandlestickItem(pg.GraphicsObject):
    """Stable pixel-width candlesticks, independent of zoom and interval."""

    def __init__(self, data, chart=None):
        super().__init__()
        self.data = data or []
        self.chart = chart
        self.picture = None
        self.pixel_width = 6  # target width in screen pixels
        self._last_view_rect = None
        self._cached_coord_per_pixel = 1.0

    def _update_scale(self):
        """Recalculate data-space width per pixel (once per frame)."""
        vb = self.chart.getViewBox() if self.chart else None
        if not vb:
            return
        view_rect = vb.viewRect()
        if self._last_view_rect != view_rect:
            self._last_view_rect = QRectF(view_rect)
            if view_rect.width() > 0 and vb.width() > 0:
                self._cached_coord_per_pixel = view_rect.width() / vb.width()

    def generatePicture(self):
        self._update_scale()
        coord_per_pixel = self._cached_coord_per_pixel
        half = (self.pixel_width / 2) * coord_per_pixel

        self.picture = pg.QtGui.QPicture()
        p = pg.QtGui.QPainter(self.picture)
        upPen = pg.mkPen('#00FF00')
        dnPen = pg.mkPen('#FF3333')
        upBrush = pg.mkBrush('#00FF00')
        dnBrush = pg.mkBrush('#FF3333')

        for (t, o, h, l, c) in self.data:
            rising = c >= o
            pen = upPen if rising else dnPen
            brush = upBrush if rising else dnBrush
            p.setPen(pen)
            p.drawLine(pg.QtCore.QPointF(t, l), pg.QtCore.QPointF(t, h))
            top = c if rising else o
            bottom = o if rising else c
            rect = pg.QtCore.QRectF(t - half, bottom, half * 2, top - bottom)
            p.fillRect(rect, brush)
            p.drawRect(rect)

        p.end()

    def paint(self, p, *args):
        if not self.picture:
            self.generatePicture()
        else:
            # Re-check scale every frame (if zoom changed)
            self._update_scale()
            self.generatePicture()
        p.drawPicture(0, 0, self.picture)

    def updateData(self, data):
        self.data = data or []
        self.generatePicture()
        self.informViewBoundsChanged()

    def boundingRect(self):
        if not self.picture:
            self.generatePicture()
        return pg.QtCore.QRectF(self.picture.boundingRect())









# ---------------------------
# WINDOW + MODE LAYOUT SETUP
# ---------------------------
window = QWidget()
window.setWindowTitle("Currency & Crypto Converter")
window.setGeometry(100, 100, 900, 600)  # x, y, width, height

# Two separate vertical layouts for the two modes (content only)
fiat_layout_content = QVBoxLayout()
crypto_layout_content = QVBoxLayout()

# Global mode variable
mode = "fiat"   # "fiat" | "crypto" | "global"

# (If you still have top buttons elsewhere for a different design, ignore these;
# in the current design we select modes from the welcome screen)
fiat_button = QPushButton("Fiat")
crypto_button = QPushButton("Crypto")
global_button = QPushButton("Global Graphs")
fiat_button.setCheckable(True)
crypto_button.setCheckable(True)
global_button.setCheckable(True)
fiat_button.setChecked(True)  # default

# ---------------------------
# WIDGETS: MAKE TWO COPIES
# ---------------------------
# Labels (one per mode)
result_label_fiat = QLabel("")
result_label_fiat.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
icon_label_fiat = QLabel()

result_label_crypto = QLabel("")
result_label_crypto.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
icon_label_crypto = QLabel()

# Inputs (one set per mode)
amount_input_fiat = QLineEdit()
amount_input_fiat.setPlaceholderText("Enter amount")
amount_input_fiat.textChanged.connect(format_amount_input)

from_currency_fiat = SearchableComboBox()
from_currency_fiat.setEditable(True)
from_currency_fiat.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)

to_currency_fiat = SearchableComboBox()
to_currency_fiat.setEditable(True)
to_currency_fiat.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)

swap_button_fiat = QPushButton("⇄ Swap")
swap_button_fiat.setFixedWidth(70)

# --- fiat input row container ---
input_row_fiat = QHBoxLayout()
input_row_fiat.addWidget(amount_input_fiat)
input_row_fiat.addWidget(from_currency_fiat)
input_row_fiat.addWidget(to_currency_fiat)
input_row_fiat.addWidget(swap_button_fiat)

inputs_container_fiat = QWidget()
inputs_container_fiat.setLayout(input_row_fiat)

convert_button_fiat = QPushButton("Convert")

# Crypto set
amount_input_crypto = QLineEdit()
amount_input_crypto.setPlaceholderText("Enter amount")
amount_input_crypto.textChanged.connect(format_amount_input)

from_currency_crypto = SearchableComboBox()
from_currency_crypto.setEditable(True)
from_currency_crypto.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)

to_currency_crypto = SearchableComboBox()
to_currency_crypto.setEditable(True)
to_currency_crypto.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)

# (No swap in crypto by your earlier design; omit or keep disabled)
swap_button_crypto = QPushButton("⇄ Swap")
swap_button_crypto.setEnabled(False)
swap_button_crypto.setVisible(False)

# --- crypto input row container ---
input_row_crypto = QHBoxLayout()
input_row_crypto.addWidget(amount_input_crypto)
input_row_crypto.addWidget(from_currency_crypto)
input_row_crypto.addWidget(to_currency_crypto)

inputs_container_crypto = QWidget()
inputs_container_crypto.setLayout(input_row_crypto)

convert_button_crypto = QPushButton("Convert")

# wire up the swap/convert handlers to mode-aware wrappers (defined just below)
swap_button_fiat.clicked.connect(lambda: swap_currencies())
convert_button_fiat.clicked.connect(lambda: convert_currency())
convert_button_crypto.clicked.connect(lambda: convert_currency())

# ---------------------------
# MODE-AWARE ACCESS HELPERS
# ---------------------------
def _current_widgets():
    """Return the correct widget set for the active mode."""
    if mode == "fiat":
        return (result_label_fiat, amount_input_fiat, from_currency_fiat, to_currency_fiat)
    elif mode == "crypto":
        return (result_label_crypto, amount_input_crypto, from_currency_crypto, to_currency_crypto)
    else:
        # global mode has no converter widgets
        return (None, None, None, None)

# ---------------------------
# CONVERTER LOGIC (MODE-SAFE)
# ---------------------------
def convert_currency():
    global mode
    # Global graphs: conversion disabled
    if mode == "global":
        if result_label_fiat:  # show message somewhere visible if user clicks mistakenly
            result_label_fiat.setText("Global Graphs mode: conversion disabled.")
        return

    result_label, amount_input, from_currency, to_currency = _current_widgets()
    if result_label is None:
        return

    result_label.setText("Converting...")

    amount_text = amount_input.text()
    from_cur_full = from_currency.currentText()
    to_cur_full = to_currency.currentText()
    from_cur = extract_symbol(from_cur_full)
    to_cur = extract_symbol(to_cur_full)
    print(f"[DEBUG] from_cur_full='{from_cur_full}' → {from_cur}, to_cur_full='{to_cur_full}' → {to_cur}")


    try:
        amount = float(amount_text.replace(",", ""))
    except ValueError:
        result_label.setText("Please enter a valid number.")
        return

    if mode == "fiat":
        url = f"{BASE_URL}/convert?from={from_cur}&to={to_cur}&amount={amount}"
        try:
            response = requests.get(url)
            data = response.json()
            if "converted" in data:
                converted = data["converted"]
                result_label.setText(f"{amount} {from_cur} = {converted:.2f} {to_cur}")
            else:
                result_label.setText("Error: " + data.get("error", "Unknown error"))
        except Exception as e:
            result_label.setText(f"Proxy request failed: {e}")

    elif mode == "crypto":
        try:
            base = from_cur.upper()
            quote = to_cur.upper()
            if quote == "USD":
                quote = "USDT"
            pair = f"{base}{quote}"
            url = f"{BASE_URL}/crypto-price"
            resp = requests.get(url, timeout=8)
            prices = resp.json()
            if pair not in prices:
                result_label.setText(f"No price available for {pair}")
                return
            price = prices[pair]
            amount_val = float(amount_input.text().replace(",", ""))
            converted = amount_val * price
            result_label.setText(f"{amount_val} {from_cur.upper()} = {converted:.2f} {to_cur.upper()}")
        except Exception as e:
            result_label.setText(f"Crypto price fetch failed: {e}")

# ---------------------------
# DROPDOWNS (MODE-SAFE UPDATES)
# ---------------------------
def update_currency_dropdowns():
    global mode
    if mode == "fiat":
        from_currency_fiat.clear();  to_currency_fiat.clear()
        from_currency_fiat.addItems(fiat_list)
        to_currency_fiat.addItems(fiat_list)

    elif mode == "crypto":
        from_currency_crypto.clear();  to_currency_crypto.clear()
        from_currency_crypto.addItems(crypto_list)
        fiats = coingecko_supported_fiats or ["USD", "EUR", "GBP"]
        to_currency_crypto.addItems(fiats)
        from_currency_crypto.setCurrentText("Bitcoin (BTC)")
        to_currency_crypto.setCurrentText("USD")

    elif mode == "global":
        # nothing to show in global mode
        pass


# ---------------------------
# PLACE WIDGETS INTO LAYOUTS
# ---------------------------
# (‼️ IMPORTANT: Do NOT add fiat_board / chart_controls / crypto_chart here yet —
# they must be created FIRST elsewhere, then added. We'll add them after their creation.)

# Fiat: top area
fiat_layout_content.addWidget(result_label_fiat)
fiat_layout_content.addWidget(icon_label_fiat)
fiat_layout_content.addWidget(inputs_container_fiat)
fiat_layout_content.addWidget(convert_button_fiat)
# fiat_board will be added later, after it's created

# Crypto: top area
crypto_layout_content.addWidget(result_label_crypto)
crypto_layout_content.addWidget(icon_label_crypto)
crypto_layout_content.addWidget(inputs_container_crypto)
crypto_layout_content.addWidget(convert_button_crypto)
# chart_controls + crypto_chart will be added later, after they're created

# --- Bottom spacers ---
bottom_row_fiat = QHBoxLayout()
bottom_row_fiat.addStretch()
fiat_layout_content.addLayout(bottom_row_fiat)

bottom_row_crypto = QHBoxLayout()
bottom_row_crypto.addStretch()
crypto_layout_content.addLayout(bottom_row_crypto)

# --- Margins & spacing ---
fiat_layout_content.setSpacing(10)
fiat_layout_content.setContentsMargins(15, 15, 15, 15)

crypto_layout_content.setSpacing(10)
crypto_layout_content.setContentsMargins(15, 15, 15, 15)


# ---------------------------
# INITIAL DATA POPULATION
# ---------------------------
fetch_supported_fiats_from_coingecko()
populate_currency_dropdowns()
populate_crypto_dropdowns()
update_currency_dropdowns()


# --- Swap currencies depending on current mode ---
def swap_currencies():
    global mode  

    if mode == "fiat":
        from_text = from_currency_fiat.currentText()
        to_text = to_currency_fiat.currentText()
        from_currency_fiat.setCurrentText(to_text)
        to_currency_fiat.setCurrentText(from_text)

    elif mode == "crypto":
        from_text = from_currency_crypto.currentText()
        to_text = to_currency_crypto.currentText()

        # Left must be crypto, right must be fiat
        left_is_crypto = from_text in crypto_list
        right_is_fiat = to_text in (coingecko_supported_fiats or ["USD", "EUR", "GBP"])

        if left_is_crypto and right_is_fiat:
            from_currency_crypto.setCurrentText(
                to_text if to_text in crypto_list else from_text
            )
            to_currency_crypto.setCurrentText(
                from_text if from_text in (coingecko_supported_fiats or ["USD", "EUR", "GBP"]) else to_text
            )
        else:
            # reset to defaults
            from_currency_crypto.clear()
            to_currency_crypto.clear()
            from_currency_crypto.addItems(crypto_list)
            to_currency_crypto.addItems(coingecko_supported_fiats or ["USD", "EUR", "GBP"])
            from_currency_crypto.setCurrentText("Bitcoin (BTC)")
            to_currency_crypto.setCurrentText("USD")

    elif mode == "global":
        # No swapping in global mode
        pass


# --- React to currency changes ---
def _maybe_refresh_chart_on_change():
    if mode == "crypto":
        refresh_candles_async()

# connect both fiat and crypto dropdowns
from_currency_fiat.currentTextChanged.connect(_maybe_refresh_chart_on_change)
to_currency_fiat.currentTextChanged.connect(_maybe_refresh_chart_on_change)
from_currency_crypto.currentTextChanged.connect(_maybe_refresh_chart_on_change)
to_currency_crypto.currentTextChanged.connect(_maybe_refresh_chart_on_change)

# connect buttons AFTER swap_currencies is defined
swap_button_fiat.clicked.connect(swap_currencies)


# --- Crypto chart (time axis) ---
time_axis = DateAxisItem(orientation='bottom')
crypto_chart = pg.PlotWidget(axisItems={'bottom': time_axis})


crypto_chart.setBackground("#0d1b2a")
crypto_chart.showGrid(x=True, y=True, alpha=0.25)
crypto_chart.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
crypto_chart.getAxis('bottom').setHeight(40)


# --- Crosshair (vertical + horizontal lines) ---
v_line = InfiniteLine(angle=90, movable=False, pen=pg.mkPen(color='#888', style=pg.QtCore.Qt.PenStyle.DotLine))
h_line = InfiniteLine(angle=0,  movable=False, pen=pg.mkPen(color='#888', style=pg.QtCore.Qt.PenStyle.DotLine))
crypto_chart.addItem(v_line, ignoreBounds=True)
crypto_chart.addItem(h_line, ignoreBounds=True)

# Label for crosshair
cross_label = TextItem("", anchor=(0,1), color='#FFD700')
crypto_chart.addItem(cross_label)

# --- Interval + Pair controls ---
interval_row = QHBoxLayout()
interval_row.setSpacing(12)
interval_row.setContentsMargins(0, 6, 0, 6)

pair_row = QHBoxLayout()
pair_row.addWidget(QLabel("Chart pair:"))
chart_pair_box = QComboBox()
chart_pair_box.addItems([
    "BTC/USDT", "ETH/USDT", "XRP/USDT", "DOGE/USDT", "AR/USDT",
    "LINK/USDT", "RENDER/USDT", "LTC/USDT", "SOL/USDT",
    "BCH/USDT", "ETC/USDT", "ADA/USDT", "TRX/USDT", "DOT/USDT",
])
pair_row.addWidget(chart_pair_box)

chart_controls = QWidget()
chart_controls_layout = QVBoxLayout(chart_controls)
chart_controls_layout.setContentsMargins(0, 0, 0, 0)
chart_controls_layout.addLayout(interval_row)
chart_controls_layout.addLayout(pair_row)

# --- Add to crypto layout ---
crypto_layout_content.addWidget(chart_controls)
crypto_layout_content.addWidget(crypto_chart)


def _on_pair_changed(_):
    global auto_range_needed
    auto_range_needed = True      # tell the chart to auto-range next draw
    refresh_candles_async()

chart_pair_box.currentTextChanged.connect(_on_pair_changed)

def refresh_candles_async():
    """Fetch candles in background and update the chart safely via the proxy server."""
    pair = chart_pair_box.currentText().replace("/", "").upper()
    interval = selected_interval    

    def _fetch_and_emit():
        try:
            # ✅ Always call your proxy server, not Binance directly
            url = f"{BASE_URL}/crypto-chart?symbol={pair}&interval={interval}&limit={MAX_CANDLES}"
            resp = _requests_session.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            candles = data.get("candles") or []

            # ✅ Handle missing or malformed data
            if not candles:
                print(f"⚠️ No candle data for {pair} {interval}")
                return []

            # Normalize candles into float tuples for plotting
            ohlc = []
            for c in candles[-200:]:
                try:
                    ts, o, h, l, cl = float(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4])
                    ohlc.append((ts / 1000.0, o, h, l, cl))
                except Exception:
                    continue

            # ✅ Smooth out big timestamp gaps (avoid broken lines)
            ohlc.sort(key=lambda x: x[0])  # ensure chronological
            return ohlc

        except Exception as e:
            print(f"❌ Candle fetch failed for {pair} {interval}: {e}")
            return []

    def _update_main_thread(ohlc):
        global current_candle_item, last_candle_ts, auto_range_needed
        if not ohlc:
            return

        if current_candle_item is None:
            current_candle_item = CandlestickItem(ohlc, chart=crypto_chart)
            crypto_chart.addItem(current_candle_item)
        else:
            # Always update with fresh data, even if already created
            current_candle_item.updateData(ohlc)

        # Force repaint
        crypto_chart.repaint()

        # Ensure autoscaling and proper visual centering
        if auto_range_needed:
            crypto_chart.enableAutoRange(axis=pg.ViewBox.XYAxes, enable=True)
            auto_range_needed = False

    # ✅ Use your thread pool safely
    worker = Worker(_fetch_and_emit)
    worker.signals.result.connect(_update_main_thread)
    thread_pool.start(worker)

    # --- Optional helper retained for consistent visuals ---
    def _compute_candle_width(ohlc):
        """Compute visually consistent candle width regardless of time interval."""
        if len(ohlc) < 2:
            return 0.0005

        # Get time difference between candles (in seconds)
        deltas = [ohlc[i + 1][0] - ohlc[i][0] for i in range(len(ohlc) - 1)]
        median_delta = sorted(deltas)[len(deltas) // 2]
        width = median_delta * 0.8
        width = max(0.0001, min(width, 3600 * 12))  # clamp between 0.0001s and 12h equivalent
        return width



def _load_more_candles_async():
    """Fetch older candles when user scrolls left."""
    global current_ohlc, pending_scroll_load

    if not current_ohlc:
        pending_scroll_load = False
        return

    pair = chart_pair_box.currentText().replace("/", "").upper()
    interval = selected_interval
    oldest_ts = current_ohlc[0][0]  # leftmost timestamp
    end_time = int(oldest_ts * 1000)
    limit = 200

    def _fetch_older():
        try:
            url = (
                f"{BASE_URL}/crypto-chart"
                f"?symbol={pair}&interval={interval}&endTime={end_time}&limit={limit}"
            )
            resp = _requests_session.get(url, timeout=10)
            resp.raise_for_status()
            raw = resp.json()
            older = [
                (float(c[0]) / 1000.0, float(c[1]), float(c[2]), float(c[3]), float(c[4]))
                for c in raw.get("candles", [])  # safer: expect the server to return {"candles": [...]}
            ]
            return older
        except Exception as e:
            print("⚠️ Error loading older candles:", e)
            return []

    def _append_older_on_main(older):
        global current_ohlc, pending_scroll_load
        if older:
            # prepend older data (remove overlaps)
            combined = older + [c for c in current_ohlc if c[0] > older[-1][0]]
            current_ohlc = combined[-MAX_CANDLES:]
            _update_chart_on_main(current_ohlc)
        pending_scroll_load = False

    worker = Worker(_fetch_older)
    worker.signals.result.connect(_append_older_on_main)
    thread_pool.start(worker)




def _lock_chart_bounds(ohlc):
    """Limit panning and trigger more data when user scrolls left."""
    global last_pan_time, pending_scroll_load, current_ohlc

    if not ohlc:
        return

    current_ohlc = ohlc  # keep latest for other functions
    vb = crypto_chart.getViewBox()
    view_rect = vb.viewRect()
    x_min, x_max = ohlc[0][0], ohlc[-1][0]
    y_values = [c[2] for c in ohlc] + [c[3] for c in ohlc]
    y_min, y_max = min(y_values), max(y_values)

    # Hard boundaries
    vb.setLimits(
        xMin=x_min - (x_max - x_min) * 0.05,
        xMax=x_max + (x_max - x_min) * 0.05,
        yMin=y_min * 0.98,
        yMax=y_max * 1.02,
    )

    # --- Lazy load: if the user scrolls near the left edge, fetch older candles ---
    now = time.time()
    if view_rect.left() <= x_min + (x_max - x_min) * 0.15:
        if not pending_scroll_load and (now - last_pan_time > 2.0):  # debounce 2 s
            pending_scroll_load = True
            last_pan_time = now
            print("⏳ Loading older candles trigger fired")
            _load_more_candles_async()

# --- Connect chart view change to bounds lock & lazy loader ---
crypto_chart.sigRangeChanged.connect(
    lambda *_: _lock_chart_bounds(globals().get("current_ohlc", []))
)

def _adjust_chart_view():
    """Ensure candles stay visible and proportionate even after resizing."""
    vb = crypto_chart.getViewBox()
    vb.setAspectLocked(False)  # allow flexible X/Y scaling

    # Keep a consistent padding around visible candles
    vb.setAutoVisible(y=True, x=True)
    vb.setLimits(yMin=0)



intervals = ["1m", "5m", "1h", "1d"]
interval_buttons = {}        # keep a reference if you want to style active one
selected_interval = "1m"     # default

def on_interval_click():
    global selected_interval
    btn = window.sender()
    selected_interval = btn.text()

    # --- NEW: visually highlight the active button ---
    for l, button in interval_buttons.items():
        if l == selected_interval:
            button.setStyleSheet("background-color: #FFD700; color: black; font-weight: bold;")
        else:
            button.setStyleSheet("")   # back to default
    
    global auto_range_needed
    auto_range_needed = True
    refresh_candles_async()


def _update_crosshair(evt):
    if crypto_chart.sceneBoundingRect().contains(evt):
        mouse_point = crypto_chart.getViewBox().mapSceneToView(evt)
        x = mouse_point.x()
        y = mouse_point.y()
        v_line.setPos(x)
        h_line.setPos(y)

        if 0 < x < 4102444800:  # between 1970 and ~2100
            time_str = datetime.datetime.fromtimestamp(x).strftime('%Y-%m-%d %H:%M')
        else:
            time_str = "—"

        cross_label.setHtml(
            f"<span style='color:#FFD700;'>Time: {time_str}<br>Price: {y:.2f}</span>"
        )
        cross_label.setPos(x, y)


# Connect the handler
crypto_chart.scene().sigMouseMoved.connect(_update_crosshair)


for label in intervals:
    b = QPushButton(label)
    b.setCheckable(True)
    b.clicked.connect(on_interval_click)
    interval_row.addWidget(b)
    interval_buttons[label] = b

interval_buttons[selected_interval].setChecked(True)
interval_buttons[selected_interval].setStyleSheet("background-color: #FFD700; color: black; font-weight: bold;")


# mark the default one
interval_buttons[selected_interval].setChecked(True)



# --- Enable interactive zoom & pan ---
crypto_chart.setMouseEnabled(x=True, y=True)  # drag to pan, wheel to zoom
crypto_chart.setMenuEnabled(True)             # right-click context menu


# State for chart updates
crypto_timer = QTimer()
crypto_timer.setInterval(5000)  # syncs perfectly with server refresh
current_candle_item = None
crypto_timer.timeout.connect(refresh_candles_async)
crypto_timer.start()



# --- Fiat Exchange Board ---
fiat_board = QTableWidget()
fiat_board.setFont(QFont("Consolas", 14))
fiat_board.setAlternatingRowColors(True)
fiat_board.setFont(QFont("Courier New", 14))
fiat_board.setColumnCount(2)
fiat_board.setHorizontalHeaderLabels(["Pair", "Rate"])
fiat_board.verticalHeader().setVisible(False)
fiat_board.horizontalHeader().setStretchLastSection(True)
fiat_board.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
fiat_board.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)

# stretch all columns evenly across width
from PyQt6.QtWidgets import QHeaderView
fiat_board.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
fiat_board.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
fiat_board.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

# ✅ Add to fiat layout (not main_layout anymore)
fiat_layout_content.addWidget(fiat_board)


# ----------------------------
# Fiat Top Movers (replaces old board)
# ----------------------------
def _fetch_fiat_board_bg():
    """Background fetch of fiat-board data from the proxy."""
    resp = _requests_session.get(f"{BASE_URL}/fiat-board", timeout=5)
    resp.raise_for_status()
    return resp.json()


def _update_fiat_board_on_main(data):
    """Update the fiat_board table with Top 10 movers by % change."""
    global previous_rates
    pairs = data.get("pairs", {})

    # --- Compute movers (sorted by absolute percentage change) ---
    movers = sorted(
        [(pair, vals.get("current"), vals.get("change", 0.0))
         for pair, vals in pairs.items()
         if vals.get("current") is not None],
        key=lambda x: abs(x[2]),
        reverse=True
    )[:10]  # top 10 only

    fiat_board.setRowCount(len(movers))
    fiat_board.setColumnCount(3)
    fiat_board.setHorizontalHeaderLabels(["Pair", "Rate", "Change %"])
    fiat_board.horizontalHeader().setStretchLastSection(True)

    from PyQt6.QtGui import QColor

    for row, (pair, rate, change) in enumerate(movers):
        # Format and color rows
        pair_item = QTableWidgetItem(pair)
        pair_item.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))

        rate_item = QTableWidgetItem(f"{float(rate):,.4f}")
        rate_item.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))

        change_item = QTableWidgetItem(f"{change:+.3f}%")
        change_item.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))

        if change > 0:
            change_item.setForeground(QColor("#00FF00"))
            rate_item.setForeground(QColor("#00FF00"))
        elif change < 0:
            change_item.setForeground(QColor("#FF3333"))
            rate_item.setForeground(QColor("#FF3333"))
        else:
            change_item.setForeground(QColor("#AAAAAA"))
            rate_item.setForeground(QColor("#AAAAAA"))

        fiat_board.setItem(row, 0, pair_item)
        fiat_board.setItem(row, 1, rate_item)
        fiat_board.setItem(row, 2, change_item)



def update_fiat_board_async():
    """Run the fiat board update in the background."""
    worker = Worker(_fetch_fiat_board_bg)
    worker.signals.result.connect(_update_fiat_board_on_main)
    thread_pool.start(worker)


news_browser = QTextBrowser()
news_browser.setOpenExternalLinks(True)
news_browser.setStyleSheet("background:transparent; border:none; color:#e0e0e0;")

def update_financial_news():
    global news_browser  # ensure we’re updating the correct widget
    try:
        rss = feedparser.parse("https://finance.yahoo.com/news/rssindex")
        entries = rss.entries[:10]
        if not entries:
            news_browser.setHtml("<span style='color:#888'>No news available.</span>")
            return

        html = "<h3 style='color:#FFD700;'>Latest Headlines</h3><ul>"
        for e in entries:
            title = e.title
            link = e.link
            html += f"<li><a href='{link}' style='color:#e0e0e0;text-decoration:none;'>{title}</a></li>"
        html += "</ul>"
        news_browser.setHtml(html)

    except Exception as e:
        print("⚠️ News update failed:", e)
        news_browser.setHtml(f"<span style='color:#888'>⚠️ Could not fetch news: {e}</span>")



def update_financial_insights():
    try:
        # --- Get all data from your backend (cached Frankfurter + Binance) ---
        resp = requests.get(f"{BASE_URL}/insights", timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            eur_usd = data.get("eur_usd", 0)
            btc_usd = data.get("btc_usd", 0)
            eth_usd = data.get("eth_usd", 0)
            dax_val = data.get("dax", 0)
        else:
            print("⚠️ /insights endpoint unavailable:", resp.text)
            eur_usd = btc_usd = eth_usd = dax_val = 0

        # --- Stock / index quotes from Yahoo Finance ---
        tickers = {
            "S&P 500": "^GSPC",
            "FTSE 100": "^FTSE",
            "Apple": "AAPL",
            "Nvidia": "NVDA",
            "Microsoft": "MSFT",
            "BlackRock": "BLK",
            "Crude Oil": "CL=F",
            "Gold": "GC=F",
        }

        y_url = "https://query1.finance.yahoo.com/v8/finance/chart/"
        headers = {"User-Agent": "Mozilla/5.0"}
        quotes = {}

        for name, symbol in tickers.items():
            r = requests.get(f"{y_url}{symbol}", params={"interval": "1h"}, headers=headers, timeout=30)
            j = r.json()
            quotes[name] = (
                j.get("chart", {})
                .get("result", [{}])[0]
                .get("meta", {})
                .get("regularMarketPrice")
            )

        # --- OpenAI: placeholder (no public ticker yet) ---
        quotes["OpenAI"] = "—"

        # --- Update UI labels (with icons) ---
        btc_label.setText(f"{ICON_MAP['BTC/USD']}  BTC/USD: {btc_usd:,.0f} $")
        eth_label.setText(f"{ICON_MAP['ETH/USD']}  ETH/USD: {eth_usd:,.0f} $")
        eurusd_label.setText(f"{ICON_MAP['EUR/USD']}  EUR/USD: {eur_usd:.4f}")
        dax_label.setText(f"{ICON_MAP['DAX']}  DAX: {dax_val:,.0f}")  # from backend insights
        sp_label.setText(f"{ICON_MAP['S&P 500']}  S&P 500: {quotes.get('S&P 500', 0):,.0f}")
        ftse_label.setText(f"{ICON_MAP['FTSE 100']}  FTSE 100: {quotes.get('FTSE 100', 0):,.0f}")
        apple_label.setText(f"{ICON_MAP['Apple']}  Apple: {quotes.get('Apple', 0):,.0f}")
        nvda_label.setText(f"{ICON_MAP['Nvidia']}  Nvidia: {quotes.get('Nvidia', 0):,.0f}")
        oil_label.setText(f"{ICON_MAP['Crude Oil']}  Crude Oil: {quotes.get('Crude Oil', 0):,.2f}")
        gold_label.setText(f"{ICON_MAP['Gold']}  Gold: {quotes.get('Gold', 0):,.2f}")
        microsoft_label.setText(f"{ICON_MAP['Microsoft']}  Microsoft: {quotes.get('Microsoft', 0):,.0f}")
        blackrock_label.setText(f"{ICON_MAP['BlackRock']}  BlackRock: {quotes.get('BlackRock', 0):,.0f}")

    except Exception as e:
        print("⚠️ update_financial_insights failed:", e)




def update_top_movers():
    """Fetch top gainers and losers from Binance and update the Movers page."""
    try:
        print("🔁 update_top_movers running...")
        url = "https://api.binance.com/api/v3/ticker/24hr"
        data = requests.get(url, timeout=30).json()

        # keep only USDT pairs
        usdt_pairs = [d for d in data if d["symbol"].endswith("USDT")]

        # sort by percent change
        top_gainers = sorted(usdt_pairs, key=lambda x: float(x["priceChangePercent"]), reverse=True)[:5]
        top_losers  = sorted(usdt_pairs, key=lambda x: float(x["priceChangePercent"]))[:5]

        # build display strings
        gain_text = "\n".join(
            [f"▲ {g['symbol']}: {float(g['priceChangePercent']):.2f}%  (${float(g['lastPrice']):,.3f})"
             for g in top_gainers]
        )
        lose_text = "\n".join(
            [f"▼ {l['symbol']}: {float(l['priceChangePercent']):.2f}%  (${float(l['lastPrice']):,.3f})"
             for l in top_losers]
        )

        top_gainers_label.setText(gain_text)
        top_losers_label.setText(lose_text)

    except Exception as e:
        print("⚠️ update_top_movers failed:", e)


# Refresh every 60s
board_timer = QTimer()
board_timer.timeout.connect(update_fiat_board_async)
board_timer.start(60000)
update_fiat_board_async()  # first run immediately


# --- News auto-refresh ---
news_timer = QTimer()
news_timer.timeout.connect(update_financial_news)
news_timer.start(180000)  # every 3 minutes
update_financial_news()   # initial load


# --- Final setup with startup mode selector ---

# Page 0: Welcome screen
select_page = QWidget()
select_layout = QVBoxLayout(select_page)
select_layout.setSpacing(30)

welcome = QLabel("Your Global Currency & Crypto Companion")
welcome.setAlignment(Qt.AlignmentFlag.AlignCenter)
welcome.setStyleSheet("font-size: 20px; font-weight: 600; font-family: 'Roboto Condensed'; letter-spacing: 1px;")

select_layout.addWidget(welcome)

# --- Financial Insights Panel (multi-page) ---
insights_box = QGroupBox("Financial Insights")
insights_layout = QVBoxLayout(insights_box)

# Create stacked pages
insights_stack = QStackedWidget()
insights_layout.addWidget(insights_stack)

def make_insight_label(name, value_text="loading…"):
    """Create a unified label with icon, name, and value placeholder."""
    icon = ICON_MAP.get(name, "")
    label = QLabel(f"{icon}  {name}: {value_text}")
    # neutral light gray text for content
    label.setStyleSheet("font-size: 14px; color: #e0e0e0;")
    return label


# --- Page 1: Market snapshot ---
page1 = QWidget()
p1_layout = QGridLayout(page1)

financial_title = QLabel("The big Boys")
financial_title.setStyleSheet(GOLD_HEADER_STYLE)
p1_layout.addWidget(financial_title, 0, 0, 1, 2)  # span across two columns

btc_label = make_insight_label("BTC/USD")
eth_label = make_insight_label("ETH/USD")
eurusd_label = make_insight_label("EUR/USD")
dax_label = make_insight_label("DAX")
sp_label = make_insight_label("S&P 500")
ftse_label = make_insight_label("FTSE 100")
apple_label = make_insight_label("Apple")
nvda_label = make_insight_label("Nvidia")
oil_label = make_insight_label("Crude Oil")
gold_label = make_insight_label("Gold")
microsoft_label = make_insight_label("Microsoft")
blackrock_label = make_insight_label("BlackRock")

p1_layout.addWidget(btc_label, 1, 0)
p1_layout.addWidget(eth_label, 1, 1)
p1_layout.addWidget(eurusd_label, 2, 0)
p1_layout.addWidget(dax_label, 2, 1)
p1_layout.addWidget(sp_label, 3, 0)
p1_layout.addWidget(ftse_label, 3, 1)
p1_layout.addWidget(apple_label, 4, 0)
p1_layout.addWidget(nvda_label, 4, 1)
p1_layout.addWidget(oil_label, 5, 0)
p1_layout.addWidget(gold_label, 5, 1)
p1_layout.addWidget(microsoft_label, 6, 0)
p1_layout.addWidget(blackrock_label, 6, 1)

insights_stack.addWidget(page1)

# --- Financial Insights auto-refresh ---
insights_timer = QTimer()
insights_timer.timeout.connect(update_financial_insights)
insights_timer.start(180000)  # every 3 minutes
update_financial_insights()   # first run

# --- Page 2: Financial News ---
page2 = QWidget()
p2_layout = QVBoxLayout(page2)
p2_layout.addWidget(news_browser)
insights_stack.addWidget(page2)

# --- Page 3: Top Crypto Movers ---
page3 = QWidget()

# outer layout so the title sits on top
outer_layout = QVBoxLayout(page3)

movers_title = QLabel("Top Crypto Movers")
movers_title.setStyleSheet(GOLD_HEADER_STYLE)
outer_layout.addWidget(movers_title)

movers_layout = QHBoxLayout()
movers_layout.setContentsMargins(20, 20, 20, 20)
movers_layout.setSpacing(40)

# Labels for gainers and losers
top_gainers_label = QLabel("Loading top gainers…")
top_losers_label = QLabel("Loading top losers…")

# --- Timer to refresh Top Movers every 3 minutes ---
movers_timer = QTimer()
movers_timer.timeout.connect(update_top_movers)
movers_timer.start(180000)  # every 3 minutes
update_top_movers()         # run once on startup

# Basic alignment and styling
top_gainers_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
top_losers_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
top_gainers_label.setStyleSheet("font-size: 14px; color: #00ff00;")  # bright green
top_losers_label.setStyleSheet("font-size: 14px; color: #ff5555;")   # soft red

movers_layout.addWidget(top_gainers_label)
movers_layout.addWidget(top_losers_label)

outer_layout.addLayout(movers_layout)

insights_stack.addWidget(page3)



# --- Navigation buttons ---
nav_row = QHBoxLayout()
prev_btn = QPushButton("◀")
next_btn = QPushButton("▶")
nav_row.addStretch()
nav_row.addWidget(prev_btn)
nav_row.addWidget(next_btn)
nav_row.addStretch()
insights_layout.addLayout(nav_row)

# --- Page rotation logic ---
current_index = 0
rotation_timer = QTimer()
rotation_timer.setInterval(15000)  # 15s
rotation_timer.timeout.connect(lambda: insights_stack.setCurrentIndex((insights_stack.currentIndex() + 1) % insights_stack.count()))
rotation_timer.start()

def pause_rotation():
    rotation_timer.stop()

def resume_rotation():
    rotation_timer.start()

insights_box.enterEvent = lambda e: pause_rotation()
insights_box.leaveEvent = lambda e: resume_rotation()
prev_btn.clicked.connect(lambda: insights_stack.setCurrentIndex((insights_stack.currentIndex() - 1) % insights_stack.count()))
next_btn.clicked.connect(lambda: insights_stack.setCurrentIndex((insights_stack.currentIndex() + 1) % insights_stack.count()))



# Add to page (above mode buttons)
select_layout.addWidget(insights_box)


btn_go_fiat = QPushButton("Fiat Mode")
btn_go_crypto = QPushButton("Crypto Mode")
btn_go_global = QPushButton("Global Graphs")

row = QHBoxLayout()
row.addStretch()
row.addWidget(btn_go_fiat)
row.addWidget(btn_go_crypto)
row.addWidget(btn_go_global)
row.addStretch()
select_layout.addLayout(row)

# Page 1: Fiat Mode
fiat_page = QWidget()
fiat_layout = QVBoxLayout(fiat_page)

back_btn_fiat = QPushButton("← Back")
back_btn_fiat.setFixedWidth(80)
fiat_layout.addWidget(back_btn_fiat)

fiat_layout.addLayout(fiat_layout_content)   # ✅ fiat UI only


# Page 2: Crypto Mode
crypto_page = QWidget()
crypto_layout = QVBoxLayout(crypto_page)

back_btn_crypto = QPushButton("← Back")
back_btn_crypto.setFixedWidth(80)
crypto_layout.addWidget(back_btn_crypto)

crypto_layout.addLayout(crypto_layout_content)  # ✅ crypto UI only

# Page 3: Global Graphs (TradingView)
global_page = QWidget()
global_layout = QVBoxLayout(global_page)

back_btn_global = QPushButton("← Back")
back_btn_global.setFixedWidth(80)
global_layout.addWidget(back_btn_global)

tradingview = QWebEngineView()

# allow file:// to load remote scripts
tradingview.settings().setAttribute(
    QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True
)


html_path = os.path.abspath("tradingview.html")
with open(html_path, "r", encoding="utf-8") as f:
    html = f.read()

# load with https base origin
tradingview.setHtml(html, baseUrl=QUrl("https://local.tradingview/"))
print("Loading TradingView from:", html_path)

global_layout.addWidget(tradingview)
global_layout.setContentsMargins(0, 0, 0, 0)
tradingview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)



# --- Build the stacked pages ---
stack = QStackedWidget()
stack.addWidget(select_page)   # index 0 = welcome
stack.addWidget(fiat_page)     # index 1 = fiat
stack.addWidget(crypto_page)   # index 2 = crypto
stack.addWidget(global_page)   # index 3 = global

# --- Top-level layout for window ---
outer_layout = QVBoxLayout()
outer_layout.addWidget(stack)
window.setLayout(outer_layout)

# --- Navigation logic ---
def go_fiat():
    global mode
    mode = "fiat"
    update_currency_dropdowns()
    update_fiat_board_async()
    stack.setCurrentIndex(1)  # fiat page

def go_crypto():
    global mode
    mode = "crypto"
    update_currency_dropdowns()
    refresh_candles_async()
    stack.setCurrentIndex(2)  # crypto page

def go_global():
    global mode
    mode = "global"
    stack.setCurrentIndex(3)  # global page


def go_back():
    stack.setCurrentIndex(0)

# --- Button actions for welcome and back buttons ---
btn_go_fiat.clicked.connect(go_fiat)
btn_go_crypto.clicked.connect(go_crypto)
btn_go_global.clicked.connect(go_global)

back_btn_fiat.clicked.connect(go_back)
back_btn_crypto.clicked.connect(go_back)
back_btn_global.clicked.connect(go_back)



# Show window & run app
window.show()

import signal
signal.signal(signal.SIGINT, signal.SIG_DFL)
app.aboutToQuit.connect(lambda: globals().__setitem__('SHUTTING_DOWN', True))

def clean_up_before_exit():
    # tell all background workers to stop
    globals()['SHUTTING_DOWN'] = True
    # stop the repeating timers so they don’t fire again
    try:
        crypto_timer.stop()
        board_timer.stop()
    except NameError:
        pass   # in case one of them isn’t defined yet

app.aboutToQuit.connect(clean_up_before_exit)

sys.exit(app.exec())

import os, sys, webview

# Optional: allow passing a symbol via args later
symbol = sys.argv[1] if len(sys.argv) > 1 else None

html_path = os.path.abspath("tradingview_embed.html")
url = "file:///" + html_path.replace("\\", "/")

webview.create_window("TradingView Pro Chart", url)
webview.start()

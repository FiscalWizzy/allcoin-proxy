from cefpython3 import cefpython as cef
import sys, os

def main():
    # Absolute path to the HTML file we just created
    html_path = os.path.abspath("tradingview_embed.html")
    url = "file:///" + html_path.replace("\\", "/")

    sys.excepthook = cef.ExceptHook  # To shutdown all CEF processes on error
    cef.Initialize()
    cef.CreateBrowserSync(
        url=url,
        window_title="TradingView Pro Mode"
    )
    cef.MessageLoop()
    cef.Shutdown()

if __name__ == "__main__":
    main()

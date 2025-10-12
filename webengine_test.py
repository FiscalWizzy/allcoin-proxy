
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtCore import QUrl
import sys

app = QApplication(sys.argv)

view = QWebEngineView()
view.load(QUrl("https://www.example.com"))  # any simple site
view.show()

sys.exit(app.exec())

# AllCoin Proxy

A lightweight Flask-based backend for the **Monkey Business** currency & crypto converter app.  
This service fetches, caches, and serves data for fiat and crypto markets in real-time.

## 🌍 Features
- Real-time fiat currency rates via ExchangeRate API + Frankfurter.
- Live crypto data and candles from Binance.
- Global insights (BTC, ETH, EUR/USD, DAX) from CoinGecko and Yahoo Finance.
- Background caching and instant refresh to reduce API load.
- Flask REST endpoints for app integration.

## 🔗 Endpoints
| Endpoint | Description |
|-----------|--------------|
| `/fiat-board` | Latest fiat exchange rates and deltas |
| `/crypto-chart` | Candle data for crypto pairs |
| `/crypto-price` | Live crypto prices |
| `/insights` | Market snapshots (BTC, ETH, DAX, EUR/USD) |
| `/health` | Simple health check |

## 🧠 Tech Stack
- Python 3.12  
- Flask 3  
- Requests  
- Gunicorn (for deployment)
- Feedparser

## 🚀 Deployment
The app runs seamlessly on Render or Google Cloud Run.  
Start command:
```bash
gunicorn -b 0.0.0.0:8080 proxy_server:app

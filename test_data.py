import requests, json
import feedparser
rss = feedparser.parse("https://finance.yahoo.com/news/rssindex")
print(len(rss.entries))
print(rss.entries[0].title)

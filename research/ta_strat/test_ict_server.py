"""Standalone server to preview the /ict page (so we don't touch the live dashboard)."""
import os, sys, json, urllib.request
from aiohttp import web
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
import ict_sm_markers, ict_chart_page


def fetch(interval, limit=1000):
    u = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={interval}&limit={limit}"
    data = json.loads(urllib.request.urlopen(u, timeout=15).read())
    return [{"time": int(k[0]) // 1000, "open": float(k[1]), "high": float(k[2]),
             "low": float(k[3]), "close": float(k[4])} for k in data]


async def data(request):
    itv = request.query.get("interval", "1m")
    if itv not in ("1m", "5m", "15m", "1h", "4h"):
        itv = "1m"
    candles = fetch(itv)
    m = ict_sm_markers.compute(candles)
    return web.json_response({"candles": candles, **m})


async def page(request):
    return web.Response(text=ict_chart_page.PAGE_HTML, content_type="text/html")


app = web.Application()
app.add_routes([web.get("/ict", page), web.get("/api/ictsm", data)])
web.run_app(app, host="127.0.0.1", port=8010)

import asyncio
import os
import time
from dotenv import load_dotenv
import ccxt.async_support as ccxt
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

# =========================
# CONFIG
# =========================
load_dotenv()
app = FastAPI()

TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", "20"))
MIN_PROFIT = float(os.getenv("MIN_PROFIT_PCT", "0.05"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
FEE = 0.002

exchange = ccxt.gateio({
    "apiKey": os.getenv("GATEIO_API_KEY"),
    "secret": os.getenv("GATEIO_SECRET"),
    "enableRateLimit": True,
    "options": {"defaultType": "spot"},
})

# =========================
# STATE
# =========================
state = {
    "running": False,
    "stop": False,
    "logs": [],
    "best": None,
    "all_opportunities": [],
    "trades": 0,
    "total_profit": 0.0,
    "prices": {}
}

def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line)
    state["logs"].append(line)
    state["logs"] = state["logs"][-100:]

# =========================
# INIT MARKETS
# =========================
async def init():
    await exchange.load_markets()
    log("📡 Markets loaded")

# =========================
# PRICE FETCH (BID/ASK)
# =========================
async def get_ticker(symbol):
    t = await exchange.fetch_ticker(symbol)
    return {
        "bid": t.get("bid") or t.get("last") or 0,
        "ask": t.get("ask") or t.get("last") or 0
    }

# =========================
# REAL TRIANGLE SIMULATION
# =========================
def apply_trade(asset, amount, price, side):
    if side == "buy":
        base = amount / price
        return base * (1 - FEE), None
    else:
        quote = amount * price
        return quote * (1 - FEE), None

async def simulate_triangle(path):
    asset = "USDT"
    amount = TRADE_AMOUNT

    for step in path:
        symbol = step["symbol"]
        side = step["side"]

        ticker = state["prices"].get(symbol)
        if not ticker:
            return None

        price = ticker["ask"] if side == "buy" else ticker["bid"]

        if price <= 0:
            return None

        amount, _ = apply_trade(asset, amount, price, side)

        # asset changes logically
        asset = step["to"]

    profit = amount - TRADE_AMOUNT
    pct = (profit / TRADE_AMOUNT) * 100

    if pct >= MIN_PROFIT:
        return {
            "route": path,
            "profit": profit,
            "pct": pct
        }

    return None

# =========================
# TRIANGLES (FIXED)
# =========================
TRIANGLES = [
    {
        "name": "USDT → BTC → ETH → USDT",
        "path": [
            {"symbol": "BTC/USDT", "side": "buy", "to": "BTC"},
            {"symbol": "ETH/BTC", "side": "buy", "to": "ETH"},
            {"symbol": "ETH/USDT", "side": "sell", "to": "USDT"},
        ]
    },
    {
        "name": "USDT → BTC → BNB → USDT",
        "path": [
            {"symbol": "BTC/USDT", "side": "buy", "to": "BTC"},
            {"symbol": "BNB/BTC", "side": "buy", "to": "BNB"},
            {"symbol": "BNB/USDT", "side": "sell", "to": "USDT"},
        ]
    },
    {
        "name": "USDT → BTC → XRP → USDT",
        "path": [
            {"symbol": "BTC/USDT", "side": "buy", "to": "BTC"},
            {"symbol": "XRP/BTC", "side": "buy", "to": "XRP"},
            {"symbol": "XRP/USDT", "side": "sell", "to": "USDT"},
        ]
    }
]

# =========================
# SCAN LOOP
# =========================
async def scan():
    opportunities = []

    try:
        for t in TRIANGLES:
            # update prices for symbols used
            symbols = {step["symbol"] for step in t["path"]}
            for s in symbols:
                state["prices"][s] = await get_ticker(s)

            result = await simulate_triangle(t["path"])
            if result:
                result["route"] = t["name"]
                opportunities.append(result)

    except Exception as e:
        log(f"Scan error: {e}")

    opportunities.sort(key=lambda x: x["pct"], reverse=True)
    return opportunities

# =========================
# ENGINE
# =========================
async def engine():
    state["running"] = True
    log("🚀 Bot started")

    while not state["stop"]:
        ops = await scan()

        state["all_opportunities"] = ops[:10]
        state["best"] = ops[0] if ops else None

        if state["best"]:
            log(f"🎯 {state['best']['route']} | {state['best']['pct']:.4f}%")

            if DRY_RUN:
                state["trades"] += 1
                state["total_profit"] += state["best"]["profit"]

        await asyncio.sleep(2)

# =========================
# API
# =========================
@app.on_event("startup")
async def startup():
    await init()

@app.get("/start")
async def start():
    if not state["running"]:
        asyncio.create_task(engine())
    state["stop"] = False
    return {"status": "started"}

@app.get("/stop")
async def stop():
    state["stop"] = True
    return {"status": "stopped"}

@app.get("/api/status")
async def status():
    return state

@app.get("/", response_class=HTMLResponse)
async def home():
    return "<h2>Arbitrage Bot Running (Fixed Version)</h2>"

# =========================
# RUN
# =========================
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

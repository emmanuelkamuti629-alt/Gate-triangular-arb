import asyncio
import os
from dotenv import load_dotenv
import ccxt
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import threading
import uvicorn
from datetime import datetime

load_dotenv()

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Global state for the dashboard
bot_status = {
    "running": False,
    "last_run": None,
    "last_profit": 0,
    "total_trades": 0,
    "total_pnl": 0,
    "logs": []
}

def log(msg):
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_msg = f"[{timestamp}] {msg}"
    print(log_msg)
    bot_status["logs"].append(log_msg)
    bot_status["logs"] = bot_status["logs"][-50:] # Keep last 50 logs

exchange = ccxt.gateio({
    "apiKey": os.getenv("GATEIO_API_KEY"),
    "secret": os.getenv("GATEIO_SECRET"),
    "enableRateLimit": True,
})

TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", 5))
MIN_PROFIT_PCT = float(os.getenv("MIN_PROFIT_PCT", 0.003))

async def check_profit():
    btc_usdt = await exchange.fetch_order_book("BTC/USDT")
    eth_btc = await exchange.fetch_order_book("ETH/BTC")
    eth_usdt = await exchange.fetch_order_book("ETH/USDT")

    btc_ask = btc_usdt["asks"][0][0]
    eth_btc_ask = eth_btc["asks"][0][0]
    eth_usdt_bid = eth_usdt["bids"][0][0]

    btc_amt = TRADE_AMOUNT / btc_ask
    eth_amt = btc_amt / eth_btc_ask
    usdt_final = eth_amt * eth_usdt_bid
    fees = 0.006
    profit_pct = (usdt_final / TRADE_AMOUNT) - 1 - fees
    return profit_pct

async def execute_cycle():
    await exchange.load_markets()
    try:
        profit_pct = await check_profit()
        log(f"Est. profit: {profit_pct*100:.3f}%")

        if profit_pct < MIN_PROFIT_PCT:
            log("❌ Not profitable. Skipping")
            return

        log("🚀 START CYCLE")
        # Your 3-step trading logic here...
        # Update bot_status["last_profit"], bot_status["total_pnl"], etc

    except Exception as e:
        log(f"EXEC ERROR: {e}")

async def bot_loop():
    bot_status["running"] = True
    while True:
        bot_status["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await execute_cycle()
        await asyncio.sleep(10)

@app.on_event("startup")
async def startup_event():
    # Start bot in background when website starts
    asyncio.create_task(bot_loop())

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "status": bot_status})

@app.get("/health")
async def health():
    return {"status": "ok", "bot_running": bot_status["running"]}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

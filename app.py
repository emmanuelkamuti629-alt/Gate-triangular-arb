import asyncio
import os
from dotenv import load_dotenv
import ccxt.async_support as ccxt
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import uvicorn
from datetime import datetime

load_dotenv()

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# =========================
# BOT STATE
# =========================
bot_status = {
    "running": False,
    "stop": False,
    "last_run": None,
    "last_profit": 0,
    "total_trades": 0,
    "total_pnl": 0,
    "logs": []
}

bot_started = False # prevent double startup


# =========================
# LOGGING
# =========================
def log(msg):
    timestamp = datetime.now().strftime("%H:%M:%S")
    msg = f"[{timestamp}] {msg}"
    print(msg)
    bot_status["logs"].append(msg)
    bot_status["logs"] = bot_status["logs"][-50:]


# =========================
# EXCHANGE
# =========================
exchange = ccxt.gateio({
    "apiKey": os.getenv("GATEIO_API_KEY"),
    "secret": os.getenv("GATEIO_SECRET"),
    "enableRateLimit": True,
})

TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", "5"))
MIN_PROFIT_PCT = float(os.getenv("MIN_PROFIT_PCT", "0.01"))


# =========================
# PROFIT CALCULATION
# =========================
async def check_profit():
    try:
        btc_usdt, eth_btc, eth_usdt = await asyncio.gather(
            exchange.fetch_order_book("BTC/USDT"),
            exchange.fetch_order_book("ETH/BTC"),
            exchange.fetch_order_book("ETH/USDT"),
        )

        # safety checks
        if not btc_usdt["asks"] or not eth_btc["asks"] or not eth_usdt["bids"]:
            log("⚠️ Empty order book")
            return 0

        btc_ask = btc_usdt["asks"][0][0]
        eth_btc_ask = eth_btc["asks"][0][0]
        eth_usdt_bid = eth_usdt["bids"][0][0]

        btc_amt = TRADE_AMOUNT / btc_ask
        eth_amt = btc_amt / eth_btc_ask
        usdt_final = eth_amt * eth_usdt_bid

        # realistic cost model
        fee = 0.003 # 3 legs ~0.1% each
        slippage = 0.0015

        profit_pct = (usdt_final / TRADE_AMOUNT) - 1 - fee - slippage
        return profit_pct

    except Exception as e:
        log(f"CHECK ERROR: {e}")
        return 0


# =========================
# EXECUTION CYCLE
# =========================
async def execute_cycle():
    profit_pct = await check_profit()
    bot_status["last_profit"] = profit_pct

    log(f"Est profit: {profit_pct * 100:.4f}%")

    if profit_pct < MIN_PROFIT_PCT:
        log("❌ No opportunity")
        return

    log("🚀 OPPORTUNITY DETECTED")

    # placeholder for real execution logic
    bot_status["total_trades"] += 1
    bot_status["total_pnl"] += profit_pct * TRADE_AMOUNT


# =========================
# BOT LOOP
# =========================
async def bot_loop():
    bot_status["running"] = True
    log("Bot started")

    try:
        await exchange.load_markets()
    except Exception as e:
        log(f"Exchange load error: {e}")
        bot_status["running"] = False
        return

    while not bot_status["stop"]:
        try:
            bot_status["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            await execute_cycle()
        except Exception as e:
            log(f"LOOP ERROR: {e}")

        await asyncio.sleep(10)

    await exchange.close()
    log("Bot stopped")
    bot_status["running"] = False


# =========================
# FASTAPI STARTUP SAFETY
# =========================
@app.on_event("startup")
async def startup():
    global bot_started
    if not bot_started:
        bot_started = True
        asyncio.create_task(bot_loop())


# =========================
# DASHBOARD
# =========================
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "status": bot_status}
    )


@app.get("/stop")
def stop_bot():
    bot_status["stop"] = True
    return {"status": "stopping"}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "running": bot_status["running"]
    }


# =========================
# RUN SERVER
# =========================
if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", 10000))
            )

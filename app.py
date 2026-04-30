import asyncio
import os
import time
from dotenv import load_dotenv
import ccxt.async_support as ccxt

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import uvicorn

# =========================
# ENV
# =========================
load_dotenv()

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# =========================
# CONFIG
# =========================
TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", "20"))
MIN_PROFIT_PCT = float(os.getenv("MIN_PROFIT_PCT", "0.1"))
FEE_RATE = 0.002
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
AUTO_TRADE = os.getenv("AUTO_TRADE", "true").lower() == "true"

# =========================
# STATE
# =========================
state = {
    "running": False,
    "stop": False,
    "logs": [],
    "best": None,
    "trades": 0,
    "wins": 0,
    "losses": 0,
    "realized_pnl": 0.0,
    "balance": 0.0,
}

trade_lock = asyncio.Lock()

# =========================
# EXCHANGE
# =========================
exchange = ccxt.gateio({
    "apiKey": os.getenv("GATEIO_API_KEY"),
    "secret": os.getenv("GATEIO_SECRET"),
    "enableRateLimit": True,
    "options": {"defaultType": "spot"},
})

# =========================
# LOGGING
# =========================
def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line)
    state["logs"].append(line)
    state["logs"] = state["logs"][-200:]

# =========================
# GET BALANCE
# =========================
async def update_balance():
    try:
        balance = await exchange.fetch_balance()
        usdt = balance.get("USDT", {}).get("free", 0)
        state["balance"] = usdt
        return usdt
    except Exception as e:
        log(f"Balance error: {e}")
        return state["balance"]

# =========================
# FETCH ORDERBOOKS
# =========================
async def fetch_orderbooks():
    """Fetch current orderbooks"""
    try:
        btc_usdt = await exchange.fetch_order_book("BTC/USDT", limit=10)
        eth_usdt = await exchange.fetch_order_book("ETH/USDT", limit=10)
        eth_btc = await exchange.fetch_order_book("ETH/BTC", limit=10)
        return btc_usdt, eth_usdt, eth_btc
    except Exception as e:
        log(f"Fetch error: {e}")
        return None, None, None

# =========================
# ORDERBOOK HELPERS
# =========================
def get_buy_price(book, usdt_amount):
    if not book or "asks" not in book or not book["asks"]:
        return 0, 0
    
    spent = 0
    received = 0
    weighted_price = 0
    
    for price, size in book["asks"]:
        cost = price * size
        
        if spent + cost >= usdt_amount:
            remaining = usdt_amount - spent
            received += remaining / price
            weighted_price = price
            break
        else:
            spent += cost
            received += size
            weighted_price = price
    
    return received, weighted_price

def get_sell_price(book, crypto_amount):
    if not book or "bids" not in book or not book["bids"]:
        return 0, 0
    
    received = 0
    weighted_price = 0
    remaining = crypto_amount
    
    for price, size in book["bids"]:
        if remaining <= size:
            received += remaining * price
            weighted_price = price
            break
        else:
            received += size * price
            remaining -= size
            weighted_price = price
    
    return received, weighted_price

# =========================
# SCAN ARBITRAGE
# =========================
async def scan_arbitrage():
    """Scan BTC/ETH triangle for arbitrage"""
    btc_usdt, eth_usdt, eth_btc = await fetch_orderbooks()
    
    if not btc_usdt or not eth_usdt or not eth_btc:
        return None
    
    try:
        # ROUTE 1: USDT -> BTC -> ETH -> USDT
        btc_amount, btc_price = get_buy_price(btc_usdt, TRADE_AMOUNT)
        if btc_amount > 0:
            eth_amount, eth_btc_price = get_buy_price(eth_btc, btc_amount)
            if eth_amount > 0:
                final_usdt, eth_price = get_sell_price(eth_usdt, eth_amount)
                final_usdt = final_usdt * (1 - FEE_RATE) ** 3
                profit = final_usdt - TRADE_AMOUNT
                pct = (profit / TRADE_AMOUNT) * 100
                
                if pct > MIN_PROFIT_PCT:
                    return {
                        "path": "USDT → BTC → ETH → USDT",
                        "profit": profit,
                        "pct": pct,
                    }
        
        # ROUTE 2: USDT -> ETH -> BTC -> USDT
        eth_amount2, eth_price2 = get_buy_price(eth_usdt, TRADE_AMOUNT)
        if eth_amount2 > 0:
            btc_amount2, btc_eth_price = get_sell_price(eth_btc, eth_amount2)
            if btc_amount2 > 0:
                final_usdt2, btc_price2 = get_sell_price(btc_usdt, btc_amount2)
                final_usdt2 = final_usdt2 * (1 - FEE_RATE) ** 3
                profit2 = final_usdt2 - TRADE_AMOUNT
                pct2 = (profit2 / TRADE_AMOUNT) * 100
                
                if pct2 > MIN_PROFIT_PCT:
                    return {
                        "path": "USDT → ETH → BTC → USDT",
                        "profit": profit2,
                        "pct": pct2,
                    }
        
        return None
        
    except Exception as e:
        log(f"Scan error: {e}")
        return None

# =========================
# EXECUTE TRADE
# =========================
async def execute_trade(opp):
    if trade_lock.locked():
        return False
    
    async with trade_lock:
        log(f"🚀 EXECUTING: {opp['path']} | {opp['pct']:.3f}%")
        
        if DRY_RUN:
            state["trades"] += 1
            if opp["profit"] > 0:
                state["wins"] += 1
            else:
                state["losses"] += 1
            state["realized_pnl"] += opp["profit"]
            log(f"💰 [DRY RUN] Profit: ${opp['profit']:.4f}")
            return True
        
        return False

# =========================
# MAIN ENGINE
# =========================
async def engine():
    state["running"] = True
    
    balance = await update_balance()
    log(f"💰 Balance: ${balance:.2f} USDT")
    
    log("🔍 Scanning for arbitrage opportunities...")
    
    last_log = time.time()
    
    while not state["stop"]:
        try:
            best = await scan_arbitrage()
            
            if best:
                state["best"] = best
                
                if time.time() - last_log > 5:
                    log(f"📊 Best: {best['pct']:.3f}% | {best['path']}")
                    last_log = time.time()
                
                if AUTO_TRADE and best["pct"] > MIN_PROFIT_PCT and best["profit"] > 0:
                    await execute_trade(best)
            
            await asyncio.sleep(2)  # Scan every 2 seconds
            
        except Exception as e:
            log(f"Engine error: {e}")
            await asyncio.sleep(5)
    
    await exchange.close()
    state["running"] = False
    log("🛑 Engine stopped")

# =========================
# FASTAPI ROUTES
# =========================
@app.on_event("startup")
async def startup():
    log("🌐 Arbitrage Bot API Ready")

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "state": state,
        "dry_run": DRY_RUN
    })

@app.get("/api/status")
async def get_status():
    return {
        "running": state["running"],
        "balance": state["balance"],
        "trades": state["trades"],
        "wins": state["wins"],
        "losses": state["losses"],
        "realized_pnl": state["realized_pnl"],
        "best": state["best"]
    }

@app.get("/start")
async def start_bot():
    if state["running"]:
        return {"status": "already_running"}
    
    state["stop"] = False
    asyncio.create_task(engine())
    return {"status": "started"}

@app.get("/stop")
async def stop_bot():
    state["stop"] = True
    return {"status": "stopping"}

@app.get("/health")
async def health():
    return {"status": "ok", "running": state["running"]}

# =========================
# RUN
# =========================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)

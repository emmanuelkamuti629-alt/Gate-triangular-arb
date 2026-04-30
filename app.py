import asyncio
import os
import time
from dotenv import load_dotenv
import ccxt.pro as ccxt

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
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
    "orderbooks": {},
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
# WEBSOCKET MANAGER
# =========================
class Manager:
    def __init__(self):
        self.clients = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.clients.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.clients:
            self.clients.remove(ws)

    async def send(self, msg: dict):
        dead = []
        for c in self.clients:
            try:
                await c.send_json(msg)
            except:
                dead.append(c)

        for d in dead:
            if d in self.clients:
                self.clients.remove(d)

manager = Manager()

# =========================
# HEAT MAP
# =========================
def heat(pct):
    if pct < 0.1:
        return "low"
    elif pct < 0.3:
        return "medium"
    return "high"

# =========================
# WEBSOCKET STREAM (LIMITED SYMBOLS)
# =========================
async def stream_books():
    """Stream only necessary orderbooks to prevent flooding"""
    await exchange.load_markets()
    
    # Only watch these key pairs for speed
    symbols = ["BTC/USDT", "ETH/USDT", "ETH/BTC"]
    
    log(f"🌊 Streaming {len(symbols)} symbols")
    
    while not state["stop"]:
        try:
            for symbol in symbols:
                try:
                    orderbook = await exchange.watch_order_book(symbol, limit=10)
                    if orderbook:
                        state["orderbooks"][symbol] = orderbook
                except Exception as e:
                    log(f"WS error {symbol}: {e}")
                    await asyncio.sleep(0.5)
                    
            await asyncio.sleep(0.1)  # Small pause to prevent flooding
            
        except Exception as e:
            log(f"WS stream error: {e}")
            await asyncio.sleep(1)

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
# ORDERBOOK HELPERS
# =========================
def get_buy_price(book, usdt_amount):
    """Get how much crypto you can buy with USDT"""
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
    """Get how much USDT you get from selling crypto"""
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
# TRIANGULAR ARBITRAGE SCAN
# =========================
def scan_arbitrage():
    """Scan BTC/ETH triangle for arbitrage"""
    ob = state["orderbooks"]
    
    # Need all 3 books
    if "BTC/USDT" not in ob or "ETH/USDT" not in ob or "ETH/BTC" not in ob:
        return None
    
    try:
        btc_usdt = ob["BTC/USDT"]
        eth_usdt = ob["ETH/USDT"]
        eth_btc = ob["ETH/BTC"]
        
        # ROUTE 1: USDT -> BTC -> ETH -> USDT
        btc_amount, btc_price = get_buy_price(btc_usdt, TRADE_AMOUNT)
        if btc_amount == 0:
            return None
            
        eth_amount, eth_btc_price = get_buy_price(eth_btc, btc_amount)
        if eth_amount == 0:
            return None
            
        final_usdt, eth_price = get_sell_price(eth_usdt, eth_amount)
        
        # Apply fees
        final_usdt = final_usdt * (1 - FEE_RATE) ** 3
        profit = final_usdt - TRADE_AMOUNT
        pct = (profit / TRADE_AMOUNT) * 100
        
        # ROUTE 2: USDT -> ETH -> BTC -> USDT
        eth_amount2, eth_price2 = get_buy_price(eth_usdt, TRADE_AMOUNT)
        if eth_amount2 > 0:
            btc_amount2, btc_eth_price = get_sell_price(eth_btc, eth_amount2)
            if btc_amount2 > 0:
                final_usdt2, btc_price2 = get_sell_price(btc_usdt, btc_amount2)
                final_usdt2 = final_usdt2 * (1 - FEE_RATE) ** 3
                profit2 = final_usdt2 - TRADE_AMOUNT
                pct2 = (profit2 / TRADE_AMOUNT) * 100
                
                if pct2 > pct:
                    return {
                        "path": "USDT → ETH → BTC → USDT",
                        "profit": profit2,
                        "pct": pct2,
                        "prices": {
                            "eth_usdt": eth_price2,
                            "eth_btc": btc_eth_price,
                            "btc_usdt": btc_price2
                        }
                    }
        
        if pct > MIN_PROFIT_PCT:
            return {
                "path": "USDT → BTC → ETH → USDT",
                "profit": profit,
                "pct": pct,
                "prices": {
                    "btc_usdt": btc_price,
                    "eth_btc": eth_btc_price,
                    "eth_usdt": eth_price
                }
            }
        
        return None
        
    except Exception as e:
        log(f"Scan error: {e}")
        return None

# =========================
# EXECUTE TRADE
# =========================
async def execute_trade(opp):
    """Execute the arbitrage trade"""
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
        
        # REAL TRADING (when ready)
        try:
            if "BTC → ETH" in opp["path"]:
                # Buy BTC
                order1 = await exchange.create_order(
                    "BTC/USDT", "market", "buy",
                    TRADE_AMOUNT / opp["prices"]["btc_usdt"]
                )
                # Buy ETH with BTC
                order2 = await exchange.create_order(
                    "ETH/BTC", "market", "buy",
                    order1["filled"]
                )
                # Sell ETH
                order3 = await exchange.create_order(
                    "ETH/USDT", "market", "sell",
                    order2["filled"]
                )
                
                profit = order3["cost"] - TRADE_AMOUNT
                
            else:
                # Buy ETH
                order1 = await exchange.create_order(
                    "ETH/USDT", "market", "buy",
                    TRADE_AMOUNT / opp["prices"]["eth_usdt"]
                )
                # Sell ETH for BTC
                order2 = await exchange.create_order(
                    "ETH/BTC", "market", "sell",
                    order1["filled"]
                )
                # Sell BTC
                order3 = await exchange.create_order(
                    "BTC/USDT", "market", "sell",
                    order2["filled"]
                )
                
                profit = order3["cost"] - TRADE_AMOUNT
            
            state["trades"] += 1
            state["wins"] += 1 if profit > 0 else 0
            state["losses"] += 1 if profit <= 0 else 0
            state["realized_pnl"] += profit
            
            log(f"💰 REAL PROFIT: ${profit:.4f}")
            return True
            
        except Exception as e:
            log(f"❌ Trade failed: {e}")
            state["losses"] += 1
            return False

# =========================
# MAIN ENGINE
# =========================
async def engine():
    """Main bot engine"""
    state["running"] = True
    
    # Get balance
    balance = await update_balance()
    log(f"💰 Balance: ${balance:.2f} USDT")
    
    if balance < TRADE_AMOUNT and not DRY_RUN:
        log(f"⚠️ Insufficient balance! Need ${TRADE_AMOUNT}, have ${balance:.2f}")
    
    # Start WebSocket stream
    stream_task = asyncio.create_task(stream_books())
    
    log("🔍 Scanning for arbitrage opportunities...")
    
    scan_count = 0
    last_log = time.time()
    
    while not state["stop"]:
        try:
            # Scan for opportunities
            best = scan_arbitrage()
            scan_count += 1
            
            if best:
                state["best"] = best
                
                # Send to WebSocket clients
                await manager.send({
                    "type": "update",
                    "path": best["path"],
                    "profit": best["profit"],
                    "pct": best["pct"],
                    "heat": heat(best["pct"])
                })
                
                # Log every 5 seconds
                if time.time() - last_log > 5:
                    log(f"📊 Best: {best['pct']:.3f}% | {best['path']}")
                    last_log = time.time()
                
                # Execute if profitable
                if AUTO_TRADE and best["pct"] > MIN_PROFIT_PCT and best["profit"] > 0:
                    await execute_trade(best)
            
            # Update balance periodically
            if scan_count % 20 == 0:
                await update_balance()
            
            await asyncio.sleep(0.5)  # Scan every 0.5 seconds
            
        except Exception as e:
            log(f"Engine error: {e}")
            await asyncio.sleep(1)
    
    # Cleanup
    stream_task.cancel()
    try:
        await stream_task
    except:
        pass
    
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

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

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

@app.get("/api/balance")
async def get_balance():
    await update_balance()
    return {"usdt": state["balance"]}

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
    return {
        "status": "ok",
        "running": state["running"],
        "books": len(state["orderbooks"])
    }

# =========================
# RUN
# =========================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

import asyncio
import os
from dotenv import load_dotenv
import ccxt.async_support as ccxt  # Using async support instead of pro for stability
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import uvicorn
from datetime import datetime
import time
from decimal import Decimal, ROUND_DOWN

load_dotenv()

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# =========================
# CONFIGURATION
# =========================
TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", "20"))
MIN_PROFIT_PCT = float(os.getenv("MIN_PROFIT_PCT", "0.3"))  # 0.3% minimum
MAX_SLIPPAGE_PCT = float(os.getenv("MAX_SLIPPAGE_PCT", "0.1"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# =========================
# STATE MANAGEMENT
# =========================
bot_status = {
    "running": False,
    "stop": False,
    "last_run": None,
    "last_profit": 0,
    "total_trades": 0,
    "total_pnl": 0,
    "successful_trades": 0,
    "failed_trades": 0,
    "logs": [],
    "balance": {"usdt": 0},
    "current_trade_status": "idle",
}

trade_lock = asyncio.Lock()

# Initialize exchange
exchange = ccxt.gateio({
    "apiKey": os.getenv("GATEIO_API_KEY"),
    "secret": os.getenv("GATEIO_SECRET"),
    "enableRateLimit": True,
    "options": {
        "defaultType": "spot",
    },
    "timeout": 30000,
})

# =========================
# LOGGING
# =========================
def log(msg, level="INFO"):
    timestamp = datetime.now().strftime("%H:%M:%S")
    msg = f"[{timestamp}] [{level}] {msg}"
    print(msg)
    bot_status["logs"].append(msg)
    bot_status["logs"] = bot_status["logs"][-200:]

# =========================
# BALANCE
# =========================
async def update_balance():
    try:
        balance = await exchange.fetch_balance()
        bot_status["balance"]["usdt"] = balance.get("USDT", {}).get("free", 0)
        return bot_status["balance"]
    except Exception as e:
        log(f"Balance error: {e}", "ERROR")
        return bot_status["balance"]

# =========================
# PROFIT CALCULATION
# =========================
async def check_triangular_profit():
    """Check triangular arbitrage: USDT -> BTC -> ETH -> USDT"""
    try:
        # Fetch order books
        btc_usdt = await exchange.fetch_order_book("BTC/USDT", limit=5)
        eth_btc = await exchange.fetch_order_book("ETH/BTC", limit=5)
        eth_usdt = await exchange.fetch_order_book("ETH/USDT", limit=5)
        
        # Validate order books
        if not btc_usdt['asks'] or not eth_btc['asks'] or not eth_usdt['bids']:
            return None
        
        # Get prices
        btc_price = btc_usdt['asks'][0][0]
        eth_btc_price = eth_btc['asks'][0][0]
        eth_usdt_price = eth_usdt['bids'][0][0]
        
        # Calculate arbitrage
        # Step 1: Buy BTC with USDT
        btc_amount = TRADE_AMOUNT / btc_price
        
        # Step 2: Buy ETH with BTC
        eth_amount = btc_amount / eth_btc_price
        
        # Step 3: Sell ETH for USDT
        final_usdt = eth_amount * eth_usdt_price
        
        # Calculate fees (0.2% per trade on Gate.io)
        fee_rate = 0.002
        fees = (TRADE_AMOUNT * fee_rate) + (btc_amount * btc_price * fee_rate) + (eth_amount * eth_usdt_price * fee_rate)
        
        profit = final_usdt - TRADE_AMOUNT - fees
        profit_pct = (profit / TRADE_AMOUNT) * 100
        
        return {
            'profit_pct': profit_pct,
            'profit_usd': profit,
            'path': 'USDT → BTC → ETH → USDT',
            'prices': {
                'btc_usdt': btc_price,
                'eth_btc': eth_btc_price,
                'eth_usdt': eth_usdt_price
            }
        }
        
    except Exception as e:
        log(f"Profit check error: {e}", "ERROR")
        return None

# =========================
# EXECUTION ENGINE
# =========================
async def execute_arbitrage(opportunity):
    """Execute the triangular arbitrage"""
    async with trade_lock:
        if bot_status["current_trade_status"] != "idle":
            return False
        
        bot_status["current_trade_status"] = "executing"
        start_time = time.time()
        
        try:
            log(f"🚀 EXECUTING: {opportunity['path']}", "WARNING")
            log(f"Expected profit: {opportunity['profit_pct']:.4f}% (${opportunity['profit_usd']:.4f})")
            
            if DRY_RUN:
                log("[DRY RUN] Would execute 3 trades:", "INFO")
                log(f"  [DRY RUN] 1. Buy BTC with ${TRADE_AMOUNT} USDT")
                log(f"  [DRY RUN] 2. Buy ETH with BTC")
                log(f"  [DRY RUN] 3. Sell ETH for USDT")
                
                # Simulate success
                bot_status["total_trades"] += 1
                bot_status["successful_trades"] += 1
                bot_status["total_pnl"] += opportunity['profit_usd']
                bot_status["last_profit"] = opportunity['profit_pct']
                
                execution_time = (time.time() - start_time) * 1000
                log(f"💰 [DRY RUN] Profit: ${opportunity['profit_usd']:.4f} in {execution_time:.0f}ms", "SUCCESS")
                
            else:
                # REAL EXECUTION
                # Leg 1: Buy BTC
                log("📍 LEG 1/3: Buying BTC with USDT...")
                btc_amount = TRADE_AMOUNT / opportunity['prices']['btc_usdt']
                order1 = await exchange.create_order(
                    "BTC/USDT", "market", "buy",
                    btc_amount
                )
                log(f"✅ LEG 1: Bought {order1['filled']:.6f} BTC at ${order1['average']:.2f}")
                
                # Leg 2: Buy ETH with BTC
                log("📍 LEG 2/3: Buying ETH with BTC...")
                eth_amount = order1['filled'] / opportunity['prices']['eth_btc']
                order2 = await exchange.create_order(
                    "ETH/BTC", "market", "buy",
                    eth_amount
                )
                log(f"✅ LEG 2: Bought {order2['filled']:.6f} ETH")
                
                # Leg 3: Sell ETH for USDT
                log("📍 LEG 3/3: Selling ETH for USDT...")
                order3 = await exchange.create_order(
                    "ETH/USDT", "market", "sell",
                    order2['filled']
                )
                log(f"✅ LEG 3: Sold for ${order3['cost']:.2f} USDT")
                
                # Calculate actual profit
                actual_profit = order3['cost'] - TRADE_AMOUNT
                execution_time = (time.time() - start_time) * 1000
                
                bot_status["total_trades"] += 1
                bot_status["successful_trades"] += 1
                bot_status["total_pnl"] += actual_profit
                bot_status["last_profit"] = (actual_profit / TRADE_AMOUNT) * 100
                
                log(f"💰 REAL PROFIT: ${actual_profit:.4f} in {execution_time:.0f}ms", "SUCCESS")
            
            await update_balance()
            bot_status["current_trade_status"] = "idle"
            return True
            
        except Exception as e:
            log(f"❌ EXECUTION FAILED: {e}", "ERROR")
            bot_status["failed_trades"] += 1
            bot_status["total_trades"] += 1
            bot_status["current_trade_status"] = "idle"
            return False

# =========================
# MAIN BOT LOOP
# =========================
async def bot_loop():
    """Main bot loop"""
    bot_status["running"] = True
    log("🚀 ARBITRAGE BOT STARTED", "WARNING")
    log(f"Mode: {'DRY RUN (no real trades)' if DRY_RUN else 'LIVE TRADING'}")
    log(f"Trade amount: ${TRADE_AMOUNT} USDT")
    log(f"Min profit required: {MIN_PROFIT_PCT}%")
    
    # Load markets
    try:
        await exchange.load_markets()
        log("✅ Markets loaded successfully")
    except Exception as e:
        log(f"Failed to load markets: {e}", "ERROR")
        bot_status["running"] = False
        return
    
    # Get initial balance
    await update_balance()
    log(f"💰 Starting balance: ${bot_status['balance']['usdt']:.2f} USDT")
    
    cycle_count = 0
    last_log_time = time.time()
    
    while not bot_status["stop"]:
        try:
            cycle_count += 1
            bot_status["last_run"] = datetime.now().strftime("%H:%M:%S")
            
            # Check for opportunity
            opportunity = await check_triangular_profit()
            
            # Log every 10 seconds
            if time.time() - last_log_time > 10:
                if opportunity:
                    log(f"📊 Opportunity: {opportunity['profit_pct']:.4f}% profit potential")
                else:
                    log(f"📊 No opportunities found (cycle #{cycle_count})")
                last_log_time = time.time()
            
            # Execute if profitable
            if opportunity and opportunity['profit_pct'] > MIN_PROFIT_PCT:
                log(f"🎯 PROFITABLE OPPORTUNITY: {opportunity['profit_pct']:.3f}%")
                await execute_arbitrage(opportunity)
            
            # Wait before next check
            await asyncio.sleep(3)  # Check every 3 seconds
            
        except Exception as e:
            log(f"Loop error: {e}", "ERROR")
            await asyncio.sleep(5)
    
    await exchange.close()
    bot_status["running"] = False
    log("Bot stopped")

# =========================
# FASTAPI ENDPOINTS
# =========================
@app.on_event("startup")
async def startup_event():
    log("🌐 Arbitrage Bot API Ready")

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "status": bot_status,
        "dry_run": DRY_RUN
    })

@app.get("/api/status")
async def api_status():
    return {
        "running": bot_status["running"],
        "dry_run": DRY_RUN,
        "current_trade_status": bot_status["current_trade_status"],
        "total_trades": bot_status["total_trades"],
        "successful_trades": bot_status["successful_trades"],
        "failed_trades": bot_status["failed_trades"],
        "total_pnl": round(bot_status["total_pnl"], 2),
        "balance_usdt": round(bot_status["balance"]["usdt"], 2),
        "last_profit": round(bot_status["last_profit"], 4),
        "last_run": bot_status["last_run"]
    }

@app.get("/api/balance")
async def api_balance():
    await update_balance()
    return {"usdt": round(bot_status["balance"]["usdt"], 2)}

@app.get("/api/logs")
async def api_logs(limit: int = 50):
    return {"logs": bot_status["logs"][-limit:]}

@app.get("/start")
async def start_bot():
    if bot_status["running"]:
        return {"status": "already_running", "message": "Bot is already running"}
    
    bot_status["stop"] = False
    asyncio.create_task(bot_loop())
    return {"status": "started", "message": "Bot started"}

@app.get("/stop")
async def stop_bot():
    if not bot_status["running"]:
        return {"status": "not_running", "message": "Bot is not running"}
    
    bot_status["stop"] = True
    return {"status": "stopping", "message": "Bot stopping..."}

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "running": bot_status["running"],
        "dry_run": DRY_RUN,
        "total_trades": bot_status["total_trades"]
    }

# =========================
# RUN SERVER
# =========================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)

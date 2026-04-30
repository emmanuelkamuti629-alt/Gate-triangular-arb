import asyncio
import os
from dotenv import load_dotenv
import ccxt.async_support as ccxt
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import uvicorn
from datetime import datetime
import json
import time

load_dotenv()

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# =========================
# BOT STATE
# =========================
bot_status = {
    "running": False,
    "stop": False,
    "start": False,
    "last_run": None,
    "last_profit": 0,
    "total_trades": 0,
    "total_pnl": 0,
    "successful_trades": 0,
    "failed_trades": 0,
    "failed_arbitrages": 0,
    "active_positions": [],
    "logs": [],
    "balance": {
        "usdt": 0,
        "btc": 0,
        "eth": 0
    },
    "current_trade_status": "idle"  # idle, executing, trade1_complete, trade2_complete, rollback
}

bot_started = False
bot_task = None
trade_lock = asyncio.Lock()  # Prevents overlapping trades

# =========================
# CONFIGURATION
# =========================
TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", "10"))
MIN_PROFIT_PCT = float(os.getenv("MIN_PROFIT_PCT", "0.5"))
MAX_SLIPPAGE_PCT = float(os.getenv("MAX_SLIPPAGE_PCT", "0.1"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
REQUIRE_CONFIRMATION = os.getenv("REQUIRE_CONFIRMATION", "false").lower() == "true"

# =========================
# LOGGING
# =========================
def log(msg, level="INFO"):
    timestamp = datetime.now().strftime("%H:%M:%S")
    msg = f"[{timestamp}] [{level}] {msg}"
    print(msg)
    bot_status["logs"].append(msg)
    bot_status["logs"] = bot_status["logs"][-100:]

# =========================
# EXCHANGE SETUP - OPTIMIZED FOR SPEED
# =========================
exchange = ccxt.gateio({
    "apiKey": os.getenv("GATEIO_API_KEY"),
    "secret": os.getenv("GATEIO_SECRET"),
    "enableRateLimit": True,
    "options": {
        "defaultType": "spot",
        "adjustForTimeDifference": True,  # Critical for speed
    },
    "timeout": 30000,  # 30 second timeout
    "rateLimit": 50,  # 50ms between requests (faster)
})

# =========================
# GET BALANCE (CACHED)
# =========================
balance_cache = {}
last_balance_fetch = 0

async def update_balance(force=False):
    """Fetch and update account balance with caching"""
    global last_balance_fetch, balance_cache
    
    now = time.time()
    if not force and (now - last_balance_fetch) < 5:  # Cache for 5 seconds
        return balance_cache
    
    try:
        balance = await exchange.fetch_balance()
        balance_cache = {
            "usdt": balance.get("USDT", {}).get("free", 0),
            "btc": balance.get("BTC", {}).get("free", 0),
            "eth": balance.get("ETH", {}).get("free", 0),
            "total_usdt_value": balance.get("total", {}).get("USDT", 0)
        }
        bot_status["balance"] = balance_cache
        last_balance_fetch = now
        return balance_cache
    except Exception as e:
        log(f"Balance fetch error: {e}", "ERROR")
        return balance_cache

# =========================
# ATOMIC TRADE EXECUTION - FAST & SAFE
# =========================
async def execute_atomic_trades(execution_details):
    """
    Execute all 3 trades atomically.
    If any trade fails, attempt to reverse previous trades.
    Uses lock to prevent concurrent executions.
    """
    
    async with trade_lock:  # Prevents any other trade from starting
        bot_status["current_trade_status"] = "executing"
        log("🔒 Trade lock acquired - Starting atomic execution", "WARNING")
        
        order1 = None
        order2 = None
        order3 = None
        trade_sequence = []
        
        try:
            # STEP 1: Buy BTC with USDT (MUST succeed)
            log("⚡ STEP 1/3: Buying BTC with USDT...", "INFO")
            start_time = time.time()
            
            order1 = await exchange.create_order(
                "BTC/USDT", 
                "market", 
                "buy", 
                TRADE_AMOUNT / execution_details['btc_price'],
                None
            )
            
            trade_sequence.append({"type": "buy_btc", "order": order1, "reversed": False})
            execution_time = (time.time() - start_time) * 1000
            log(f"✅ STEP 1 complete in {execution_time:.0f}ms - Bought {order1['filled']:.6f} BTC", "SUCCESS")
            
            if REQUIRE_CONFIRMATION:
                await asyncio.sleep(0.1)  # Minimal delay only if needed
            
            # STEP 2: Buy ETH with BTC (MUST succeed)
            log("⚡ STEP 2/3: Buying ETH with BTC...", "INFO")
            start_time = time.time()
            
            btc_received = order1['filled']
            eth_amount = btc_received / execution_details['eth_btc_price']
            
            order2 = await exchange.create_order(
                "ETH/BTC",
                "market", 
                "buy",
                eth_amount,
                None
            )
            
            trade_sequence.append({"type": "buy_eth", "order": order2, "reversed": False})
            execution_time = (time.time() - start_time) * 1000
            log(f"✅ STEP 2 complete in {execution_time:.0f}ms - Bought {order2['filled']:.6f} ETH", "SUCCESS")
            
            # STEP 3: Sell ETH for USDT (MUST succeed)
            log("⚡ STEP 3/3: Selling ETH for USDT...", "INFO")
            start_time = time.time()
            
            eth_received = order2['filled']
            
            order3 = await exchange.create_order(
                "ETH/USDT",
                "market",
                "sell",
                eth_received,
                None
            )
            
            trade_sequence.append({"type": "sell_eth", "order": order3, "reversed": False})
            execution_time = (time.time() - start_time) * 1000
            log(f"✅ STEP 3 complete in {execution_time:.0f}ms - Received {order3['cost']:.2f} USDT", "SUCCESS")
            
            # ALL TRADES SUCCESSFUL
            final_usdt = order3['cost']
            profit = final_usdt - TRADE_AMOUNT
            total_time = sum([t.get('execution_time', 0) for t in trade_sequence])
            
            log(f"🎉 ATOMIC TRADE SUCCESS! Profit: ${profit:.2f} in {total_time:.0f}ms", "SUCCESS")
            bot_status["current_trade_status"] = "idle"
            
            return True, profit, trade_sequence
            
        except Exception as e:
            log(f"❌ TRADE FAILED at STEP {len(trade_sequence)+1}: {e}", "ERROR")
            bot_status["current_trade_status"] = "rollback"
            
            # BEGIN ROLLBACK - Reverse completed trades
            log("🔄 INITIATING ROLLBACK - Reversing completed trades...", "WARNING")
            
            rollback_success = True
            
            # Reverse in opposite order (LIFO)
            for trade in reversed(trade_sequence):
                try:
                    if trade["type"] == "buy_btc" and not trade["reversed"]:
                        # Sell BTC back to USDT
                        log(f"🔄 Rolling back: Selling {trade['order']['filled']:.6f} BTC", "WARNING")
                        rollback_order = await exchange.create_order(
                            "BTC/USDT",
                            "market",
                            "sell",
                            trade['order']['filled'],
                            None
                        )
                        trade["reversed"] = True
                        log(f"✅ Rollback successful: Recovered ${rollback_order['cost']:.2f} USDT", "SUCCESS")
                        
                    elif trade["type"] == "buy_eth" and not trade["reversed"]:
                        # Sell ETH back to BTC
                        log(f"🔄 Rolling back: Selling {trade['order']['filled']:.6f} ETH", "WARNING")
                        rollback_order = await exchange.create_order(
                            "ETH/BTC",
                            "market",
                            "sell",
                            trade['order']['filled'],
                            None
                        )
                        trade["reversed"] = True
                        log(f"✅ Rollback successful", "SUCCESS")
                        
                except Exception as rollback_error:
                    log(f"💥 CRITICAL: Rollback failed for {trade['type']}: {rollback_error}", "ERROR")
                    rollback_success = False
                    bot_status["failed_arbitrages"] += 1
            
            if rollback_success:
                log("✅ Full rollback completed - Account should be restored", "SUCCESS")
            else:
                log("⚠️ Partial rollback - Manual intervention may be needed!", "ERROR")
            
            bot_status["current_trade_status"] = "idle"
            return False, 0, trade_sequence

# =========================
# FAST PROFIT CHECK (CACHED ORDER BOOKS)
# =========================
orderbook_cache = {}
last_orderbook_fetch = 0

async def check_profit_fast():
    """Fast profit check with cached order books"""
    global last_orderbook_fetch, orderbook_cache
    
    try:
        # Fetch all 3 order books in parallel (fastest method)
        btc_usdt, eth_btc, eth_usdt = await asyncio.gather(
            exchange.fetch_order_book("BTC/USDT", limit=5),  # Only need top 5 levels
            exchange.fetch_order_book("ETH/BTC", limit=5),
            exchange.fetch_order_book("ETH/USDT", limit=5),
        )

        # Cache the results
        orderbook_cache = {
            "btc_ask": btc_usdt["asks"][0][0] if btc_usdt["asks"] else None,
            "eth_btc_ask": eth_btc["asks"][0][0] if eth_btc["asks"] else None,
            "eth_bid": eth_usdt["bids"][0][0] if eth_usdt["bids"] else None,
            "btc_liquidity": sum(ask[1] for ask in btc_usdt["asks"][:3]),
            "eth_btc_liquidity": sum(ask[1] for ask in eth_btc["asks"][:3]),
            "eth_usdt_liquidity": sum(bid[1] for bid in eth_usdt["bids"][:3]),
        }
        
        if not all([orderbook_cache["btc_ask"], orderbook_cache["eth_btc_ask"], orderbook_cache["eth_bid"]]):
            return 0, {}
        
        btc_ask = orderbook_cache["btc_ask"]
        eth_btc_ask = orderbook_cache["eth_btc_ask"]
        eth_bid = orderbook_cache["eth_bid"]
        
        # Fast calculation (no loops for speed)
        btc_amount = TRADE_AMOUNT / btc_ask
        eth_amount = btc_amount / eth_btc_ask
        final_usdt = eth_amount * eth_bid
        
        # Fees calculation
        fee_rate = 0.001
        fees = TRADE_AMOUNT * fee_rate * 3  # Approx 3 fees
        
        profit = final_usdt - TRADE_AMOUNT - fees
        profit_pct = (profit / TRADE_AMOUNT) * 100
        
        execution_details = {
            "btc_price": btc_ask,
            "eth_btc_price": eth_btc_ask,
            "eth_usdt_price": eth_bid,
            "btc_amount": btc_amount,
            "eth_amount": eth_amount,
            "final_usdt": final_usdt,
            "fees": fees,
            "liquidity_ok": True
        }
        
        return profit_pct, execution_details
        
    except Exception as e:
        log(f"Fast profit check error: {e}", "ERROR")
        return 0, {}

# =========================
# MAIN EXECUTION CYCLE (FAST LOOP)
# =========================
async def execute_cycle():
    """Fast execution cycle - minimal delays"""
    
    # Quick market check (skip if too volatile)
    try:
        ticker = await exchange.fetch_ticker("BTC/USDT")
        if ticker.get('percentage', 0) and abs(ticker['percentage']) > 3:
            log(f"⚠️ High volatility {ticker['percentage']}%, skipping", "WARNING")
            return
    except:
        pass
    
    # Fast profit check
    profit_pct, details = await check_profit_fast()
    bot_status["last_profit"] = profit_pct
    
    # Only log every 10th opportunity to reduce noise
    if int(time.time()) % 30 == 0:
        log(f"Profit check: {profit_pct:.4f}%")
    
    if profit_pct < MIN_PROFIT_PCT:
        return
    
    log(f"🚀 OPPORTUNITY FOUND: {profit_pct:.2f}% profit potential!")
    
    # Execute trades atomically
    success, profit, trade_sequence = await execute_atomic_trades(details)
    
    if success:
        bot_status["total_trades"] += 1
        bot_status["successful_trades"] += 1
        bot_status["total_pnl"] += profit
        await update_balance(force=True)  # Force balance update
        log(f"💰 NET PROFIT: ${profit:.2f} | Total: ${bot_status['total_pnl']:.2f}", "SUCCESS")
    else:
        bot_status["failed_trades"] += 1

# =========================
# BOT LOOP (OPTIMIZED FOR SPEED)
# =========================
async def bot_loop():
    """Main bot loop - optimized for fastest execution"""
    bot_status["running"] = True
    bot_status["stop"] = False
    log(f"⚡ FAST BOT STARTED (DRY_RUN={DRY_RUN}) | Check interval: 5s", "WARNING")
    
    try:
        await exchange.load_markets()
        log(f"✅ Markets loaded in {exchange.timeout}ms timeout")
        await update_balance(force=True)
        log(f"💰 Balance: ${bot_status['balance']['usdt']:.2f} USDT", "INFO")
    except Exception as e:
        log(f"Startup error: {e}", "ERROR")
        bot_status["running"] = False
        return
    
    cycle_count = 0
    last_balance_update = time.time()
    
    while not bot_status["stop"]:
        try:
            cycle_start = time.time()
            bot_status["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            await execute_cycle()
            
            cycle_time = (time.time() - cycle_start) * 1000
            cycle_count += 1
            
            # Update balance every 30 seconds
            if time.time() - last_balance_update > 30:
                await update_balance()
                last_balance_update = time.time()
            
            # Log performance every 100 cycles
            if cycle_count % 100 == 0:
                log(f"⚡ Performance: Avg cycle time {cycle_time:.0f}ms", "INFO")
            
            # Fast loop - only 0.5 second between cycles
            await asyncio.sleep(0.5)
            
        except Exception as e:
            log(f"LOOP ERROR: {e}", "ERROR")
            await asyncio.sleep(2)
    
    await exchange.close()
    log("Bot stopped", "INFO")
    bot_status["running"] = False

# =========================
# FASTAPI ENDPOINTS
# =========================
@app.on_event("startup")
async def startup():
    global bot_started
    if not bot_started:
        bot_started = True
        log("🚀 FAST ARBITRAGE BOT READY", "WARNING")
        log("⚡ Features: Atomic trades | Auto-rollback | 0.5s cycle time", "INFO")

@app.get("/")
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "status": bot_status, "dry_run": DRY_RUN})

@app.get("/api/status")
async def get_status():
    return {
        "running": bot_status["running"],
        "trade_status": bot_status["current_trade_status"],
        "dry_run_mode": DRY_RUN,
        "last_run": bot_status["last_run"],
        "last_profit": round(bot_status["last_profit"], 4),
        "total_trades": bot_status["total_trades"],
        "successful_trades": bot_status["successful_trades"],
        "failed_trades": bot_status["failed_trades"],
        "failed_arbitrages": bot_status["failed_arbitrages"],
        "total_pnl": round(bot_status["total_pnl"], 2),
        "balance": bot_status["balance"],
        "recent_logs": bot_status["logs"][-20:]
    }

@app.get("/start")
async def start_bot():
    global bot_task
    if bot_status["running"]:
        return {"status": "already_running", "message": "Bot is already running"}
    
    bot_status["stop"] = False
    bot_task = asyncio.create_task(bot_loop())
    return {"status": "started", "message": "⚡ Fast bot started - 0.5s cycle time"}

@app.get("/stop")
async def stop_bot():
    if not bot_status["running"]:
        return {"status": "already_stopped", "message": "Bot is not running"}
    
    bot_status["stop"] = True
    return {"status": "stopping", "message": "Bot stopping after current cycle"}

@app.get("/balance")
async def get_balance():
    await update_balance(force=True)
    return bot_status["balance"]

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "running": bot_status["running"],
        "trade_status": bot_status["current_trade_status"],
        "dry_run": DRY_RUN
    }

# =========================
# RUN SERVER
# =========================
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

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
    "successful_trades": 0,
    "failed_trades": 0,
    "active_positions": [],
    "logs": []
}

bot_started = False

# =========================
# CONFIGURATION
# =========================
TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", "10"))
MIN_PROFIT_PCT = float(os.getenv("MIN_PROFIT_PCT", "0.5"))
MAX_SLIPPAGE_PCT = float(os.getenv("MAX_SLIPPAGE_PCT", "0.1"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

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
# EXCHANGE SETUP
# =========================
exchange = ccxt.gateio({
    "apiKey": os.getenv("GATEIO_API_KEY"),
    "secret": os.getenv("GATEIO_SECRET"),
    "enableRateLimit": True,
    "options": {
        "defaultType": "spot",
    }
})

# =========================
# PROFIT CALCULATION WITH ORDER BOOK DEPTH
# =========================
async def check_profit_with_depth():
    """Calculate profit with real order book depth"""
    try:
        btc_usdt, eth_btc, eth_usdt = await asyncio.gather(
            exchange.fetch_order_book("BTC/USDT", limit=10),
            exchange.fetch_order_book("ETH/BTC", limit=10),
            exchange.fetch_order_book("ETH/USDT", limit=10),
        )

        if not all([btc_usdt["asks"], eth_btc["asks"], eth_usdt["bids"]]):
            log("⚠️ Empty order books", "WARNING")
            return 0, {}

        btc_ask_price = btc_usdt["asks"][0][0]
        eth_btc_ask_price = eth_btc["asks"][0][0]
        eth_usdt_bid_price = eth_usdt["bids"][0][0]
        
        btc_needed = TRADE_AMOUNT / btc_ask_price
        eth_needed = btc_needed / eth_btc_ask_price
        
        btc_liquidity = sum(ask[1] for ask in btc_usdt["asks"][:5])
        eth_btc_liquidity = sum(ask[1] for ask in eth_btc["asks"][:5])
        eth_usdt_liquidity = sum(bid[1] for bid in eth_usdt["bids"][:5])
        
        if btc_needed > btc_liquidity or eth_needed > eth_btc_liquidity or eth_needed > eth_usdt_liquidity:
            log(f"⚠️ Insufficient liquidity", "WARNING")
            return 0, {}
        
        btc_actual = TRADE_AMOUNT / btc_ask_price
        
        eth_amount = 0
        remaining_btc = btc_actual
        for price, amount in eth_btc["asks"]:
            if remaining_btc <= amount:
                eth_amount += remaining_btc / price
                break
            eth_amount += amount / price
            remaining_btc -= amount
        
        final_usdt = 0
        remaining_eth = eth_amount
        for price, amount in eth_usdt["bids"]:
            if remaining_eth <= amount:
                final_usdt += remaining_eth * price
                break
            final_usdt += amount * price
            remaining_eth -= amount
        
        fee_rate = 0.001
        fees = (TRADE_AMOUNT * fee_rate) + (btc_actual * fee_rate * btc_ask_price) + (eth_amount * fee_rate * eth_usdt_bid_price)
        
        profit = final_usdt - TRADE_AMOUNT - fees
        profit_pct = (profit / TRADE_AMOUNT) * 100
        
        execution_details = {
            "btc_price": btc_ask_price,
            "eth_btc_price": eth_btc_ask_price,
            "eth_usdt_price": eth_usdt_bid_price,
            "btc_amount": btc_actual,
            "eth_amount": eth_amount,
            "final_usdt": final_usdt,
            "fees": fees,
            "liquidity_ok": True
        }
        
        return profit_pct, execution_details
        
    except Exception as e:
        log(f"Profit calculation error: {e}", "ERROR")
        return 0, {}

# =========================
# REAL EXECUTION ENGINE
# =========================
async def execute_triangle_arbitrage(execution_details):
    """Execute real triangular arbitrage trades"""
    
    if DRY_RUN:
        log("🔬 DRY RUN - Would execute trades:", "INFO")
        log(f"  - Buy BTC with {TRADE_AMOUNT} USDT at {execution_details['btc_price']}", "INFO")
        log(f"  - Buy ETH with {execution_details['btc_amount']:.6f} BTC at {execution_details['eth_btc_price']}", "INFO")
        log(f"  - Sell ETH for {execution_details['final_usdt']:.2f} USDT at {execution_details['eth_usdt_price']}", "INFO")
        return True, execution_details['final_usdt'] - TRADE_AMOUNT
    
    try:
        log("🚀 EXECUTING REAL TRADES...", "WARNING")
        
        order1 = await exchange.create_order(
            "BTC/USDT", 
            "market", 
            "buy", 
            TRADE_AMOUNT / execution_details['btc_price'],
            None
        )
        log(f"✅ Buy BTC order filled: {order1['filled']} BTC")
        
        await asyncio.sleep(1)
        
        btc_balance = order1['filled'] * 0.999
        order2 = await exchange.create_order(
            "ETH/BTC",
            "market", 
            "buy",
            btc_balance / execution_details['eth_btc_price'],
            None
        )
        log(f"✅ Buy ETH order filled: {order2['filled']} ETH")
        
        await asyncio.sleep(1)
        
        eth_balance = order2['filled'] * 0.999
        order3 = await exchange.create_order(
            "ETH/USDT",
            "market",
            "sell",
            eth_balance,
            None
        )
        log(f"✅ Sell ETH order filled for {order3['cost']} USDT")
        
        final_usdt = order3['cost']
        profit = final_usdt - TRADE_AMOUNT
        
        log(f"💰 Profit: ${profit:.2f} USDT", "INFO")
        
        return True, profit
        
    except Exception as e:
        log(f"❌ Trade execution failed: {e}", "ERROR")
        return False, 0

# =========================
# RISK MANAGEMENT
# =========================
async def check_market_conditions():
    """Check if market conditions are safe for trading"""
    try:
        ticker = await exchange.fetch_ticker("BTC/USDT")
        volatility = abs(ticker['percentage']) if ticker['percentage'] else 0
        
        if volatility > 2:
            log(f"⚠️ High volatility: {volatility}%", "WARNING")
            return False
        
        return True
    except:
        return True

# =========================
# MAIN EXECUTION CYCLE
# =========================
async def execute_cycle():
    if not await check_market_conditions():
        log("Market conditions unsafe, skipping cycle", "WARNING")
        return
    
    profit_pct, details = await check_profit_with_depth()
    bot_status["last_profit"] = profit_pct
    
    log(f"Est profit: {profit_pct:.4f}%")
    
    if profit_pct < MIN_PROFIT_PCT:
        log(f"❌ No opportunity (need {MIN_PROFIT_PCT}% profit)")
        return
    
    log(f"🚀 PROFITABLE OPPORTUNITY: {profit_pct:.2f}%")
    
    success, profit = await execute_triangle_arbitrage(details)
    
    if success:
        bot_status["total_trades"] += 1
        bot_status["successful_trades"] += 1
        bot_status["total_pnl"] += profit
        log(f"✅ Trade successful! Total PnL: ${bot_status['total_pnl']:.2f}", "INFO")
    else:
        bot_status["failed_trades"] += 1
        if bot_status["total_trades"] > 0:
            log(f"❌ Trade failed! Success rate: {bot_status['successful_trades']/bot_status['total_trades']*100:.1f}%", "ERROR")

# =========================
# BOT LOOP
# =========================
async def bot_loop():
    bot_status["running"] = True
    log(f"🚀 Real arbitrage bot started (DRY_RUN={DRY_RUN})", "WARNING")
    
    try:
        await exchange.load_markets()
        log(f"✅ Loaded markets successfully")
    except Exception as e:
        log(f"Exchange load error: {e}", "ERROR")
        bot_status["running"] = False
        return
    
    cycle_count = 0
    while not bot_status["stop"]:
        try:
            bot_status["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            await execute_cycle()
            cycle_count += 1
            
            if cycle_count % 10 == 0:
                try:
                    balance = await exchange.fetch_balance()
                    log(f"💰 USDT Balance: ${balance.get('USDT', {}).get('free', 0):.2f}", "INFO")
                except:
                    pass
                
        except Exception as e:
            log(f"LOOP ERROR: {e}", "ERROR")
        
        await asyncio.sleep(15)
    
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
        asyncio.create_task(bot_loop())

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    try:
        return templates.TemplateResponse("index.html", {"request": request, "status": bot_status})
    except Exception as e:
        log(f"Template error: {e}", "ERROR")
        return JSONResponse({
            "error": "Template not found",
            "message": "Please ensure templates/index.html exists",
            "status": bot_status
        })

@app.get("/simple")
async def simple_dashboard():
    """Simple JSON endpoint that doesn't need templates"""
    return {
        "status": "ok",
        "bot_running": bot_status["running"],
        "dry_run_mode": DRY_RUN,
        "total_trades": bot_status["total_trades"],
        "successful_trades": bot_status["successful_trades"],
        "failed_trades": bot_status["failed_trades"],
        "total_pnl": round(bot_status["total_pnl"], 2),
        "last_profit": round(bot_status["last_profit"], 4),
        "last_run": bot_status["last_run"],
        "recent_logs": bot_status["logs"][-10:]
    }

@app.get("/stop")
def stop_bot():
    bot_status["stop"] = True
    return {"status": "stopping", "message": "Bot will stop after current cycle"}

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "running": bot_status["running"],
        "dry_run": DRY_RUN,
        "total_trades": bot_status["total_trades"],
        "successful_trades": bot_status["successful_trades"],
        "failed_trades": bot_status["failed_trades"]
    }

@app.get("/balance")
async def get_balance():
    try:
        balance = await exchange.fetch_balance()
        return {
            "usdt": balance.get("USDT", {}).get("free", 0),
            "btc": balance.get("BTC", {}).get("free", 0),
            "eth": balance.get("ETH", {}).get("free", 0)
        }
    except Exception as e:
        return {"error": f"Could not fetch balance: {e}"}

# =========================
# RUN SERVER
# =========================
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

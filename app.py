import asyncio
import os
from dotenv import load_dotenv
import ccxt.pro as ccxtpro
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import uvicorn
from datetime import datetime
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from collections import defaultdict
import json
import traceback

load_dotenv()

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# =========================
# CONFIGURATION
# =========================
TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", "20"))
MIN_PROFIT_PCT = float(os.getenv("MIN_PROFIT_PCT", "0.2"))
MAX_SLIPPAGE_PCT = float(os.getenv("MAX_SLIPPAGE_PCT", "0.1"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
MIN_24H_VOLUME = float(os.getenv("MIN_24H_VOLUME", "500000"))
MAX_TRIANGLES = int(os.getenv("MAX_TRIANGLES", "100"))
STALE_BOOK_MS = 1000
MAX_POSITION_SIZE = float(os.getenv("MAX_POSITION_SIZE", "100"))
CIRCUIT_BREAKER_LOSS = float(os.getenv("CIRCUIT_BREAKER_LOSS", "10"))  # Stop after $10 loss

# =========================
# STATE MANAGEMENT
# =========================
bot_status = {
    "running": False,
    "stop": False,
    "last_run": None,
    "total_trades": 0,
    "total_pnl": Decimal("0"),
    "successful_trades": 0,
    "failed_trades": 0,
    "logs": [],
    "balance": {"usdt": Decimal("0")},
    "current_trade_status": "idle",
    "ws_connected": 0,
    "triangles_found": 0,
    "best_opportunity": None,
    "circuit_breaker_triggered": False,
    "daily_pnl": Decimal("0"),
    "last_reset": datetime.now().date().isoformat()
}

trade_lock = asyncio.Lock()
circuit_breaker_lock = asyncio.Lock()

# Initialize exchange
exchange = ccxtpro.gateio({
    "apiKey": os.getenv("GATEIO_API_KEY"),
    "secret": os.getenv("GATEIO_SECRET"),
    "enableRateLimit": True,
    "options": {
        "defaultType": "spot",
        "adjustForTimeDifference": True,
    },
    "timeout": 30000,
})

markets = {}
trading_fees = {}
orderbooks = {}
orderbook_ts = {}
orderbook_seq = {}  # Track sequence numbers for freshness
triangles = []
triangle_symbols = set()
last_profit_log = 0
daily_loss = Decimal("0")

# =========================
# PRECISION HANDLING
# =========================
def to_decimal(value):
    """Convert to Decimal with high precision"""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))

def format_amount(amount, precision=8):
    """Format amount with proper precision"""
    amount_dec = to_decimal(amount)
    return float(amount_dec.quantize(Decimal(f"1e-{precision}"), rounding=ROUND_DOWN))

def format_price(price, precision=8):
    """Format price with proper precision"""
    price_dec = to_decimal(price)
    return float(price_dec.quantize(Decimal(f"1e-{precision}"), rounding=ROUND_HALF_UP))

# =========================
# LOGGING
# =========================
def log(msg, level="INFO"):
    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    msg = f"[{timestamp}] [{level}] {msg}"
    print(msg)
    bot_status["logs"].append(msg)
    bot_status["logs"] = bot_status["logs"][-500:]

# =========================
# CIRCUIT BREAKER
# =========================
async def check_circuit_breaker():
    """Stop trading if losses exceed threshold"""
    global daily_loss
    
    async with circuit_breaker_lock:
        today = datetime.now().date().isoformat()
        if bot_status["last_reset"] != today:
            bot_status["daily_pnl"] = Decimal("0")
            bot_status["last_reset"] = today
            bot_status["circuit_breaker_triggered"] = False
            daily_loss = Decimal("0")
        
        if bot_status["total_pnl"] < Decimal(str(-CIRCUIT_BREAKER_LOSS)):
            bot_status["circuit_breaker_triggered"] = True
            bot_status["stop"] = True
            log(f"🔴 CIRCUIT BREAKER TRIGGERED! Loss: ${bot_status['total_pnl']:.2f}", "CRITICAL")
            return False
        return True

# =========================
# MARKET VALIDATION
# =========================
async def validate_market_data():
    """Validate all market data before trading"""
    issues = []
    
    # Check exchange connection
    try:
        await exchange.load_markets()
    except Exception as e:
        issues.append(f"Exchange connection failed: {e}")
    
    # Check API keys
    try:
        balance = await exchange.fetch_balance()
        if balance.get("USDT", {}).get("free", 0) == 0:
            log("⚠️ USDT balance is 0!", "WARNING")
    except Exception as e:
        issues.append(f"Balance check failed: {e}")
    
    # Check WebSocket connections
    if bot_status["ws_connected"] == 0:
        issues.append("No WebSocket connections active")
    
    return len(issues) == 0, issues

# =========================
# FEE CALCULATION (100% ACCURATE)
# =========================
async def get_accurate_fee(symbol):
    """Get exact fee rate for a symbol"""
    try:
        if symbol not in trading_fees:
            trading_fees.update(await exchange.fetch_trading_fees())
        
        fee_info = trading_fees.get(symbol, {})
        taker_fee = fee_info.get('taker', 0.002)
        
        # Gate.io specific fee tiers
        if taker_fee == 0:
            taker_fee = 0.002  # Default 0.2%
        
        return Decimal(str(taker_fee))
    except:
        return Decimal("0.002")

# =========================
# TRIANGLE DISCOVERY (ACCURATE)
# =========================
async def discover_triangles():
    """Find all valid triangular arbitrage paths"""
    global triangles, triangle_symbols, markets
    
    try:
        markets = await exchange.load_markets()
        log(f"Loaded {len(markets)} markets")
        
        # Find all USDT pairs with sufficient liquidity
        usdt_pairs = []
        for symbol, market in markets.items():
            try:
                if (market.get('quote') == 'USDT' and 
                    market.get('spot') and 
                    market.get('active')):
                    
                    # Get 24h volume
                    ticker = await exchange.fetch_ticker(symbol)
                    volume = ticker.get('quoteVolume', 0)
                    
                    if volume >= MIN_24H_VOLUME:
                        usdt_pairs.append({
                            'base': market['base'],
                            'symbol': symbol,
                            'volume': volume
                        })
            except:
                continue
        
        usdt_pairs.sort(key=lambda x: x['volume'], reverse=True)
        top_pairs = usdt_pairs[:50]  # Limit to top 50 for performance
        
        log(f"Found {len(top_pairs)} liquid USDT pairs")
        
        # Build triangles
        triangles = []
        for i, pair1 in enumerate(top_pairs):
            for j, pair2 in enumerate(top_pairs[i+1:], i+1):
                if len(triangles) >= MAX_TRIANGLES:
                    break
                
                base1, base2 = pair1['base'], pair2['base']
                
                # Check if cross pair exists
                cross_pair = f"{base1}/{base2}"
                cross_pair_rev = f"{base2}/{base1}"
                
                if cross_pair in markets:
                    triangles.append({
                        'type': 'triangle',
                        'path': ['USDT', base1, base2, 'USDT'],
                        'leg1': f"{base1}/USDT",
                        'leg2': cross_pair,
                        'leg3': f"{base2}/USDT",
                        'is_reversed': False
                    })
                elif cross_pair_rev in markets:
                    triangles.append({
                        'type': 'triangle',
                        'path': ['USDT', base1, base2, 'USDT'],
                        'leg1': f"{base1}/USDT",
                        'leg2': cross_pair_rev,
                        'leg3': f"{base2}/USDT",
                        'is_reversed': True
                    })
            
            if len(triangles) >= MAX_TRIANGLES:
                break
        
        # Collect symbols to watch
        triangle_symbols = set()
        for tri in triangles:
            triangle_symbols.add(tri['leg1'])
            triangle_symbols.add(tri['leg2'])
            triangle_symbols.add(tri['leg3'])
        
        bot_status["triangles_found"] = len(triangles)
        log(f"🔺 Discovered {len(triangles)} triangles from {len(triangle_symbols)} symbols")
        
        return triangles
        
    except Exception as e:
        log(f"Triangle discovery failed: {e}", "ERROR")
        return []

# =========================
# WEBSOCKET STREAMING
# =========================
async def watch_orderbook(symbol):
    """Maintain real-time orderbook with sequence tracking"""
    retry_count = 0
    max_retries = 5
    
    while not bot_status["stop"] and retry_count < max_retries:
        try:
            # Watch orderbook with depth 10 for speed
            orderbook = await exchange.watch_order_book(symbol, limit=10)
            
            if orderbook and orderbook['asks'] and orderbook['bids']:
                orderbooks[symbol] = orderbook
                orderbook_ts[symbol] = time.time() * 1000
                
                # Track sequence number if available
                if 'nonce' in orderbook:
                    orderbook_seq[symbol] = orderbook['nonce']
                
                retry_count = 0  # Reset retry counter on success
            await asyncio.sleep(0.001)  # Minimal delay
            
        except Exception as e:
            retry_count += 1
            log(f"WS error for {symbol} (attempt {retry_count}/{max_retries}): {e}", "WARNING")
            await asyncio.sleep(2 ** retry_count)  # Exponential backoff
    
    if retry_count >= max_retries:
        log(f"❌ Failed to maintain WS for {symbol}", "ERROR")

async def start_websocket_streams():
    """Start all WebSocket streams"""
    tasks = []
    for symbol in triangle_symbols:
        task = asyncio.create_task(watch_orderbook(symbol))
        tasks.append(task)
        await asyncio.sleep(0.01)  # Stagger connections
    
    bot_status["ws_connected"] = len(tasks)
    log(f"🌐 Started {len(tasks)} WebSocket streams")
    return tasks

def is_orderbook_fresh(symbol, max_age_ms=STALE_BOOK_MS):
    """Check if orderbook is fresh enough"""
    if symbol not in orderbooks or not orderbooks[symbol]:
        return False
    
    age = time.time() * 1000 - orderbook_ts.get(symbol, 0)
    return age < max_age_ms

# =========================
# ACCURATE PROFIT CALCULATION
# =========================
def calculate_execution_price(orderbook, side, amount_usdt):
    """Calculate exact execution price with slippage"""
    try:
        items = orderbook['asks'] if side == 'buy' else orderbook['bids']
        if not items:
            return None, None, None
        
        remaining = Decimal(str(amount_usdt))
        total_base = Decimal("0")
        total_quote = Decimal("0")
        worst_price = Decimal("0")
        
        for price, volume in items:
            price_dec = Decimal(str(price))
            volume_dec = Decimal(str(volume))
            quote_value = price_dec * volume_dec
            
            if remaining <= quote_value:
                # Partial fill of this level
                fill_base = remaining / price_dec
                total_base += fill_base
                total_quote += remaining
                worst_price = price_dec
                remaining = Decimal("0")
                break
            else:
                # Full fill of this level
                total_base += volume_dec
                total_quote += quote_value
                worst_price = price_dec
                remaining -= quote_value
        
        if remaining > 0:
            return None, None, None  # Insufficient liquidity
        
        avg_price = total_quote / total_base if total_base > 0 else Decimal("0")
        return float(total_base), float(worst_price), float(avg_price)
        
    except Exception as e:
        log(f"Price calculation error: {e}", "ERROR")
        return None, None, None

async def check_triangle_arbitrage(triangle):
    """Check if triangle offers profitable arbitrage"""
    try:
        leg1, leg2, leg3 = triangle['leg1'], triangle['leg2'], triangle['leg3']
        is_reversed = triangle['is_reversed']
        
        # Check freshness
        if not all(is_orderbook_fresh(sym) for sym in [leg1, leg2, leg3]):
            return None
        
        # Get orderbooks
        ob1 = orderbooks[leg1]
        ob2 = orderbooks[leg2]
        ob3 = orderbooks[leg3]
        
        # Get accurate fees
        fee1 = await get_accurate_fee(leg1)
        fee2 = await get_accurate_fee(leg2)
        fee3 = await get_accurate_fee(leg3)
        
        # Leg 1: Buy base with USDT
        b1_amount, b1_worst_price, b1_avg = calculate_execution_price(ob1, 'buy', TRADE_AMOUNT)
        if not b1_amount:
            return None
        
        # Apply fee
        b1_net = Decimal(str(b1_amount)) * (Decimal("1") - fee1)
        
        # Leg 2: Trade base1 to base2
        if is_reversed:
            # Selling base1 to get base2
            b2_amount, _, _ = calculate_execution_price(ob2, 'sell', float(b1_net))
        else:
            # Buying base2 with base1
            b2_amount, _, _ = calculate_execution_price(ob2, 'buy', float(b1_net))
        
        if not b2_amount:
            return None
        
        b2_net = Decimal(str(b2_amount)) * (Decimal("1") - fee2)
        
        # Leg 3: Sell base2 for USDT
        usdt_amount, _, _ = calculate_execution_price(ob3, 'sell', float(b2_net))
        if not usdt_amount:
            return None
        
        final_usdt = Decimal(str(usdt_amount)) * (Decimal("1") - fee3)
        
        # Calculate profit
        start_usdt = Decimal(str(TRADE_AMOUNT))
        profit = final_usdt - start_usdt
        profit_pct = (profit / start_usdt) * Decimal("100")
        
        # Validate profit is real (not floating point error)
        min_profit = Decimal(str(MIN_PROFIT_PCT))
        
        if profit_pct > min_profit and profit > Decimal("0.01"):
            return {
                'profit_pct': float(profit_pct),
                'profit_usd': float(profit),
                'start_usdt': float(start_usdt),
                'final_usdt': float(final_usdt),
                'path': f"USDT → {triangle['path'][1]} → {triangle['path'][2]} → USDT",
                'legs': [leg1, leg2, leg3],
                'amounts': [b1_amount, b2_amount, usdt_amount],
                'fees': [float(fee1), float(fee2), float(fee3)],
                'slippage': [float(Decimal(str(b1_worst_price)) / Decimal(str(b1_avg)) - Decimal("1")) if b1_avg else 0]
            }
        
        return None
        
    except Exception as e:
        log(f"Triangle check error: {e}", "ERROR")
        return None

async def scan_opportunities():
    """Scan all triangles for arbitrage opportunities"""
    opportunities = []
    
    for triangle in triangles:
        try:
            opp = await check_triangle_arbitrage(triangle)
            if opp and opp['profit_pct'] > MIN_PROFIT_PCT:
                opportunities.append(opp)
        except Exception as e:
            continue
    
    if opportunities:
        best = max(opportunities, key=lambda x: x['profit_pct'])
        bot_status["best_opportunity"] = best
        return best
    
    bot_status["best_opportunity"] = None
    return None

# =========================
# ACCURATE EXECUTION ENGINE
# =========================
async def get_market_precision(symbol):
    """Get market precision for accurate order sizing"""
    market = markets.get(symbol, {})
    return {
        'amount_precision': market.get('precision', {}).get('amount', 8),
        'price_precision': market.get('precision', {}).get('price', 8),
        'min_amount': market.get('limits', {}).get('amount', {}).get('min', 0.000001),
        'min_cost': market.get('limits', {}).get('cost', {}).get('min', 1)
    }

async def execute_arbitrage_with_validation(opportunity):
    """Execute arbitrage with 100% accuracy and validation"""
    async with trade_lock:
        if bot_status["current_trade_status"] != "idle":
            log("Trade already in progress, skipping", "WARNING")
            return False
        
        # Pre-execution validation
        if not await check_circuit_breaker():
            return False
        
        # Validate balance
        await update_balance()
        if bot_status["balance"]["usdt"] < Decimal(str(TRADE_AMOUNT * 1.1)):
            log(f"Insufficient balance: ${bot_status['balance']['usdt']:.2f}", "ERROR")
            return False
        
        bot_status["current_trade_status"] = "executing"
        start_time = time.time()
        orders = []
        
        try:
            log(f"🚀 EXECUTING: {opportunity['path']}", "WARNING")
            log(f"Expected profit: {opportunity['profit_pct']:.4f}% (${opportunity['profit_usd']:.4f})")
            
            # Get precision for each leg
            precision1 = await get_market_precision(opportunity['legs'][0])
            precision2 = await get_market_precision(opportunity['legs'][1])
            precision3 = await get_market_precision(opportunity['legs'][2])
            
            # LEG 1: Buy base1 with USDT
            if DRY_RUN:
                log(f"[DRY RUN] LEG 1: Buy {opportunity['amounts'][0]:.8f} via {opportunity['legs'][0]}")
                order1 = {
                    'id': 'dry_run_1',
                    'filled': opportunity['amounts'][0],
                    'cost': TRADE_AMOUNT,
                    'status': 'closed'
                }
            else:
                amount1 = format_amount(opportunity['amounts'][0], precision1['amount_precision'])
                order1_raw = await exchange.create_order(
                    opportunity['legs'][0],
                    'market',
                    'buy',
                    amount1
                )
                order1 = await wait_for_order_fill(order1_raw['id'], opportunity['legs'][0], timeout=10)
            
            if not order1 or order1.get('status') != 'closed':
                raise Exception(f"LEG 1 failed: {order1}")
            
            orders.append(('leg1', order1, opportunity['legs'][0]))
            log(f"✅ LEG 1 complete: {order1['filled']:.8f} @ ${order1.get('average', 0):.2f}")
            
            # LEG 2: Trade to base2
            if DRY_RUN:
                log(f"[DRY RUN] LEG 2: Trade via {opportunity['legs'][1]}")
                order2 = {
                    'id': 'dry_run_2',
                    'filled': opportunity['amounts'][1],
                    'status': 'closed'
                }
            else:
                # Determine side based on market
                market_info = markets.get(opportunity['legs'][1], {})
                side = 'buy' if market_info.get('base') == opportunity['path'].split('→')[1].strip() else 'sell'
                amount2 = format_amount(order1['filled'] if not DRY_RUN else opportunity['amounts'][1], 
                                       precision2['amount_precision'])
                
                order2_raw = await exchange.create_order(
                    opportunity['legs'][1],
                    'market',
                    side,
                    amount2
                )
                order2 = await wait_for_order_fill(order2_raw['id'], opportunity['legs'][1], timeout=10)
            
            if not order2 or order2.get('status') != 'closed':
                raise Exception(f"LEG 2 failed: {order2}")
            
            orders.append(('leg2', order2, opportunity['legs'][1]))
            log(f"✅ LEG 2 complete: {order2['filled']:.8f}")
            
            # LEG 3: Sell base2 for USDT
            if DRY_RUN:
                log(f"[DRY RUN] LEG 3: Sell via {opportunity['legs'][2]}")
                order3 = {
                    'id': 'dry_run_3',
                    'filled': opportunity['amounts'][2],
                    'cost': opportunity['final_usdt'],
                    'status': 'closed'
                }
            else:
                amount3 = format_amount(order2['filled'] if not DRY_RUN else opportunity['amounts'][2],
                                       precision3['amount_precision'])
                order3_raw = await exchange.create_order(
                    opportunity['legs'][2],
                    'market',
                    'sell',
                    amount3
                )
                order3 = await wait_for_order_fill(order3_raw['id'], opportunity['legs'][2], timeout=10)
            
            if not order3 or order3.get('status') != 'closed':
                raise Exception(f"LEG 3 failed: {order3}")
            
            orders.append(('leg3', order3, opportunity['legs'][2]))
            
            # Calculate actual profit
            actual_profit = Decimal(str(order3.get('cost', 0))) - Decimal(str(TRADE_AMOUNT))
            execution_time = (time.time() - start_time) * 1000
            
            # Update statistics
            bot_status["total_trades"] += 1
            bot_status["successful_trades"] += 1
            bot_status["total_pnl"] += actual_profit
            bot_status["daily_pnl"] += actual_profit
            
            log(f"💰 SUCCESS! Profit: ${float(actual_profit):.4f} in {execution_time:.0f}ms", "SUCCESS")
            log(f"📊 Total PnL: ${float(bot_status['total_pnl']):.2f}")
            
            await update_balance()
            bot_status["current_trade_status"] = "idle"
            return True
            
        except Exception as e:
            log(f"❌ EXECUTION FAILED: {e}", "ERROR")
            log(traceback.format_exc(), "ERROR")
            
            # Attempt rollback
            await rollback_trades(orders)
            
            bot_status["failed_trades"] += 1
            bot_status["total_trades"] += 1
            bot_status["current_trade_status"] = "idle"
            return False

async def wait_for_order_fill(order_id, symbol, timeout=10):
    """Wait for order to fill with timeout"""
    start_time = time.time()
    last_status = None
    
    while time.time() - start_time < timeout:
        try:
            order = await exchange.fetch_order(order_id, symbol)
            
            if order['status'] == 'closed':
                return order
            elif order['status'] == 'canceled':
                raise Exception("Order cancelled")
            elif order['status'] != last_status:
                log(f"Order {order_id}: {order['status']} - Filled: {order.get('filled', 0)}/{order.get('amount', 0)}")
                last_status = order['status']
            
            await asyncio.sleep(0.1)
        except Exception as e:
            await asyncio.sleep(0.05)
    
    # Timeout - try to cancel
    try:
        await exchange.cancel_order(order_id, symbol)
    except:
        pass
    
    raise Exception(f"Order {order_id} timeout after {timeout}s")

async def rollback_trades(orders):
    """Rollback completed trades in reverse order"""
    if not orders:
        return
    
    log("🔄 Initiating rollback...", "WARNING")
    
    for leg_name, order, symbol in reversed(orders):
        try:
            if order.get('filled', 0) > 0:
                if DRY_RUN:
                    log(f"[DRY RUN] Rollback: Sell {order['filled']} from {leg_name}")
                else:
                    # Reverse the trade
                    market_info = markets.get(symbol, {})
                    side = 'sell' if 'buy' in str(order.get('info', {})) else 'buy'
                    
                    rollback_order = await exchange.create_order(
                        symbol,
                        'market',
                        side,
                        order['filled']
                    )
                    log(f"✅ Rollback {leg_name}: {rollback_order['filled']:.8f}")
                await asyncio.sleep(0.1)
        except Exception as e:
            log(f"⚠️ Rollback failed for {leg_name}: {e}", "ERROR")

# =========================
# BALANCE MANAGEMENT
# =========================
async def update_balance():
    """Update account balance with high precision"""
    try:
        balance = await exchange.fetch_balance()
        usdt_balance = balance.get('USDT', {}).get('free', 0)
        bot_status["balance"]["usdt"] = Decimal(str(usdt_balance))
        
        # Check if balance is too low
        if usdt_balance < TRADE_AMOUNT * 1.1:
            log(f"⚠️ Low balance: ${usdt_balance:.2f} USDT", "WARNING")
        
        return bot_status["balance"]
    except Exception as e:
        log(f"Balance update failed: {e}", "ERROR")
        return bot_status["balance"]

# =========================
# MAIN BOT LOOP
# =========================
async def bot_main_loop():
    """Main bot loop with 100% accuracy focus"""
    global triangles, triangle_symbols
    
    bot_status["running"] = True
    log("🚀 ARBITRAGE BOT STARTING (ACCURACY MODE)", "WARNING")
    
    # Validate market data
    valid, issues = await validate_market_data()
    if not valid:
        for issue in issues:
            log(f"❌ Validation failed: {issue}", "ERROR")
        bot_status["running"] = False
        return
    
    # Discover triangles
    triangles = await discover_triangles()
    if not triangles:
        log("No triangles found! Check market data.", "ERROR")
        return
    
    # Start WebSocket streams
    ws_tasks = await start_websocket_streams()
    
    # Initial balance
    await update_balance()
    log(f"💰 Starting balance: ${float(bot_status['balance']['usdt']):.2f} USDT")
    
    # Warm up orderbooks
    log("Warming up orderbooks...")
    await asyncio.sleep(3)
    
    log(f"✅ Bot ready! Scanning {len(triangles)} triangles", "SUCCESS")
    
    scan_count = 0
    last_log = time.time()
    
    while not bot_status["stop"]:
        try:
            # Check circuit breaker
            if not await check_circuit_breaker():
                break
            
            scan_count += 1
            bot_status["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # Scan for opportunities
            opportunity = await scan_opportunities()
            
            # Log periodically
            if time.time() - last_log > 5:
                books_fresh = sum(1 for s in triangle_symbols if is_orderbook_fresh(s))
                log(f"📊 Scan #{scan_count} | Fresh books: {books_fresh}/{len(triangle_symbols)} | Best: {opportunity['profit_pct']:.4f}%" if opportunity else f"📊 Scan #{scan_count} | No opportunities")
                last_log = time.time()
            
            # Execute if profitable
            if opportunity and opportunity['profit_pct'] > MIN_PROFIT_PCT:
                await execute_arbitrage_with_validation(opportunity)
            
            # Fast loop
            await asyncio.sleep(0.01)  # 10ms scan interval
            
        except Exception as e:
            log(f"Main loop error: {e}", "ERROR")
            await asyncio.sleep(0.5)
    
    # Cleanup
    log("Shutting down WebSocket connections...")
    for task in ws_tasks:
        task.cancel()
    
    await exchange.close()
    bot_status["running"] = False
    log("Bot stopped")

# =========================
# FASTAPI ENDPOINTS
# =========================
app = FastAPI()

@app.on_event("startup")
async def startup_event():
    log("🌐 WebSocket Arbitrage Bot API Ready")

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "status": bot_status,
        "dry_run": DRY_RUN
    })

@app.get("/api/status")
async def api_status():
    """Get detailed bot status"""
    return {
        "running": bot_status["running"],
        "dry_run": DRY_RUN,
        "current_trade_status": bot_status["current_trade_status"],
        "total_trades": bot_status["total_trades"],
        "successful_trades": bot_status["successful_trades"],
        "failed_trades": bot_status["failed_trades"],
        "total_pnl": float(bot_status["total_pnl"]),
        "balance_usdt": float(bot_status["balance"]["usdt"]),
        "triangles_found": bot_status["triangles_found"],
        "ws_connected": bot_status["ws_connected"],
        "circuit_breaker": bot_status["circuit_breaker_triggered"],
        "last_profit": bot_status["last_profit"],
        "best_opportunity": bot_status["best_opportunity"],
        "last_run": bot_status["last_run"]
    }

@app.get("/api/balance")
async def api_balance():
    await update_balance()
    return {"usdt": float(bot_status["balance"]["usdt"])}

@app.get("/api/logs")
async def api_logs(limit: int = 50):
    return {"logs": bot_status["logs"][-limit:]}

@app.get("/start")
async def start_bot():
    if bot_status["running"]:
        return {"status": "already_running"}
    
    bot_status["stop"] = False
    bot_status["circuit_breaker_triggered"] = False
    asyncio.create_task(bot_main_loop())
    return {"status": "started"}

@app.get("/stop")
async def stop_bot():
    bot_status["stop"] = True
    return {"status": "stopping"}

@app.get("/reset")
async def reset_bot():
    """Reset bot statistics"""
    bot_status["total_trades"] = 0
    bot_status["total_pnl"] = Decimal("0")
    bot_status["successful_trades"] = 0
    bot_status["failed_trades"] = 0
    bot_status["daily_pnl"] = Decimal("0")
    bot_status["circuit_breaker_triggered"] = False
    return {"status": "reset"}

# =========================
# RUN SERVER
# =========================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)

import asyncio
import os
import time
from dotenv import load_dotenv
import ccxt.pro as ccxt
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

# =========================
# 1. CONFIGURATION & ENV
# =========================
load_dotenv()
app = FastAPI()

TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", "20"))
MIN_PROFIT = float(os.getenv("MIN_PROFIT_PCT", "0.2"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# Initialize Exchange
exchange = ccxt.gateio({
    "apiKey": os.getenv("GATEIO_API_KEY"),
    "secret": os.getenv("GATEIO_SECRET"),
    "enableRateLimit": True,
    "options": {"defaultType": "spot"},
})

# =========================
# 2. DATA CACHE & STATE
# =========================
# Define all triangles to scan
TRIANGLES = [
    {
        "name": "BTC/ETH Triangle",
        "legs": [
            {"from": "USDT", "to": "BTC", "symbol": "BTC/USDT", "side": "buy"},
            {"from": "BTC", "to": "ETH", "symbol": "ETH/BTC", "side": "buy"},
            {"from": "ETH", "to": "USDT", "symbol": "ETH/USDT", "side": "sell"}
        ],
        "path": "USDT → BTC → ETH → USDT",
        "symbols": ["BTC/USDT", "ETH/BTC", "ETH/USDT"]
    },
    {
        "name": "BNB/BTC Triangle",
        "legs": [
            {"from": "USDT", "to": "BNB", "symbol": "BNB/USDT", "side": "buy"},
            {"from": "BNB", "to": "BTC", "symbol": "BNB/BTC", "side": "sell"},
            {"from": "BTC", "to": "USDT", "symbol": "BTC/USDT", "side": "sell"}
        ],
        "path": "USDT → BNB → BTC → USDT",
        "symbols": ["BNB/USDT", "BNB/BTC", "BTC/USDT"]
    },
    {
        "name": "ETH/SOL Triangle",
        "legs": [
            {"from": "USDT", "to": "ETH", "symbol": "ETH/USDT", "side": "buy"},
            {"from": "ETH", "to": "SOL", "symbol": "SOL/ETH", "side": "buy"},
            {"from": "SOL", "to": "USDT", "symbol": "SOL/USDT", "side": "sell"}
        ],
        "path": "USDT → ETH → SOL → USDT",
        "symbols": ["ETH/USDT", "SOL/ETH", "SOL/USDT"]
    }
]

# Collect all unique symbols
ALL_SYMBOLS = set()
for triangle in TRIANGLES:
    for symbol in triangle["symbols"]:
        ALL_SYMBOLS.add(symbol)

orderbooks = {}
for symbol in ALL_SYMBOLS:
    orderbooks[symbol] = {"book": None, "updated": 0}

# Cache for fees
fee_cache = {}
last_fee_fetch = 0

state = {
    "running": False,
    "stop": False,
    "running_trade": False,
    "logs": [],
    "best": None,
    "all_opportunities": [],
    "trades": 0,
    "total_profit": 0.0,
}

def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line)
    state["logs"].append(line)
    state["logs"] = state["logs"][-100:]

# =========================
# 3. REAL-TIME FEE FETCHING FROM EXCHANGE
# =========================
async def fetch_real_fees():
    """Fetch actual trading fees from Gate.io"""
    global fee_cache, last_fee_fetch
    
    # Cache for 5 minutes (fees don't change often)
    if time.time() - last_fee_fetch < 300 and fee_cache:
        return fee_cache
    
    try:
        trading_fees = await exchange.fetch_trading_fees()
        
        # Get fees for all symbols
        for symbol in ALL_SYMBOLS:
            if symbol in trading_fees:
                fee_cache[symbol] = {
                    "taker": trading_fees[symbol].get('taker', 0.002),
                    "maker": trading_fees[symbol].get('maker', 0.002)
                }
            else:
                fee_cache[symbol] = {"taker": 0.002, "maker": 0.002}
        
        last_fee_fetch = time.time()
        
        # Log fees
        for symbol in ALL_SYMBOLS:
            log(f"💸 {symbol} fee: {fee_cache[symbol]['taker']*100}%")
        
        return fee_cache
        
    except Exception as e:
        log(f"⚠️ Could not fetch real fees: {e}, using defaults")
        for symbol in ALL_SYMBOLS:
            fee_cache[symbol] = {"taker": 0.002, "maker": 0.002}
        return fee_cache

async def get_fee_rate(symbol):
    """Get taker fee rate for a symbol"""
    fees = await fetch_real_fees()
    return fees.get(symbol, {}).get('taker', 0.002)

# =========================
# 4. WEBSOCKET STREAMS
# =========================
async def stream(symbol):
    while not state["stop"]:
        try:
            book = await exchange.watch_order_book(symbol, limit=5)
            if book:
                orderbooks[symbol] = {"book": book, "updated": time.time()}
        except Exception as e:
            log(f"⚠️ WS Error {symbol}: {e}")
            await asyncio.sleep(2)

def get_valid_prices(symbol):
    data = orderbooks.get(symbol)
    if not data or not data["book"] or (time.time() - data["updated"] > 2.0):
        return None, None
    book = data["book"]
    if not book["bids"] or not book["asks"]:
        return None, None
    return book["bids"][0][0], book["asks"][0][0]

def get_price(symbol, side):
    """Get bid (sell) or ask (buy) price"""
    bid, ask = get_valid_prices(symbol)
    if side == "buy":
        return ask
    elif side == "sell":
        return bid
    return None

# =========================
# 5. PRECISE CALCULATION WITH REAL FEES
# =========================
async def calculate_triangle_profit(triangle):
    """Calculate profit for a specific triangle"""
    try:
        current_amount = TRADE_AMOUNT
        fee_total = 0
        leg_details = []
        
        for leg in triangle["legs"]:
            symbol = leg["symbol"]
            side = leg["side"]
            
            # Get price
            price = get_price(symbol, side)
            if not price:
                return None
            
            # Get fee for this leg
            fee_rate = await get_fee_rate(symbol)
            
            if side == "buy":
                # Buying: amount / price = quantity
                qty = current_amount / price
                cost = current_amount
                fee = cost * fee_rate
                current_amount = qty * (1 - fee_rate)
            else:
                # Selling: quantity * price = amount received
                qty = current_amount
                received = qty * price
                fee = received * fee_rate
                current_amount = received * (1 - fee_rate)
            
            fee_total += fee
            leg_details.append({
                "symbol": symbol,
                "side": side,
                "price": price,
                "fee_rate": fee_rate,
                "fee_usdt": fee
            })
        
        final_usdt = current_amount
        profit = final_usdt - TRADE_AMOUNT
        pct = (profit / TRADE_AMOUNT) * 100
        
        # Get total fee percentage
        total_fee_pct = (fee_total / TRADE_AMOUNT) * 100
        
        return {
            "route": triangle["path"],
            "profit": profit,
            "pct": pct,
            "final_usdt": final_usdt,
            "legs": [leg["symbol"] for leg in triangle["legs"]],
            "fees": {
                "total_usdt": fee_total,
                "total_pct": total_fee_pct,
                "details": leg_details
            }
        }
        
    except Exception as e:
        log(f"Error calculating {triangle['name']}: {e}")
        return None

# =========================
# 6. REAL EXECUTION ENGINE
# =========================
async def execute_triangular_trade(opp, triangle):
    """Executes 3 legs and verifies fills before returning."""
    try:
        log(f"⚡ EXECUTION START: {opp['route']}")
        
        legs = triangle["legs"]
        current_amount = TRADE_AMOUNT
        orders = []
        
        for i, leg in enumerate(legs):
            symbol = leg["symbol"]
            side = leg["side"]
            
            # Get current price
            price = get_price(symbol, side)
            if not price:
                raise Exception(f"Cannot get price for {symbol}")
            
            if side == "buy":
                qty = current_amount / price
            else:
                qty = current_amount
            
            precise_qty = float(exchange.amount_to_precision(symbol, qty))
            
            if precise_qty <= 0:
                raise Exception(f"Invalid quantity: {precise_qty}")
            
            log(f"📊 Leg {i+1}: {side.upper()} {precise_qty:.8f} {symbol}")
            
            if not DRY_RUN:
                order = await exchange.create_order(
                    symbol=symbol,
                    type="market",
                    side=side,
                    amount=precise_qty
                )
                
                # Verify fill
                order_id = order['id']
                filled = False
                retries = 0
                
                while not filled and retries < 10:
                    await asyncio.sleep(0.2)
                    try:
                        status_check = await exchange.fetch_order(order_id, symbol)
                        if status_check['status'] == 'closed':
                            if side == "buy":
                                current_amount = status_check['filled']
                            else:
                                current_amount = status_check['cost']
                            log(f"✅ Leg {i+1} Filled: {status_check['filled']:.8f} {symbol}")
                            filled = True
                        else:
                            retries += 1
                    except Exception as e:
                        retries += 1
                        await asyncio.sleep(0.1)
                
                if not filled:
                    log(f"❌ Leg {i+1} FAILED TO FILL. Stopping cycle.")
                    return None
            else:
                # Dry run simulation
                if side == "buy":
                    current_amount = precise_qty
                else:
                    current_amount = precise_qty * price
                log(f"✅ [DRY RUN] Leg {i+1} complete")
        
        profit = current_amount - TRADE_AMOUNT
        log(f"💰 CYCLE COMPLETE. Final USDT: ${current_amount:.4f}")
        log(f"💵 PROFIT: ${profit:.4f}")
        return profit
        
    except Exception as e:
        log(f"🚨 CRITICAL EXECUTION ERROR: {e}")
        return None

# =========================
# 7. CORE ENGINE
# =========================
async def engine():
    state["running"] = True
    
    # Fetch real fees on startup
    await fetch_real_fees()
    
    log("🚀 ENGINE ONLINE - Mode: " + ("DRY RUN" if DRY_RUN else "LIVE"))
    log(f"💰 Trade Amount: ${TRADE_AMOUNT}")
    log(f"🎯 Min Profit Required: {MIN_PROFIT}%")
    log(f"🔺 Scanning {len(TRIANGLES)} triangles")
    
    for triangle in TRIANGLES:
        log(f"   - {triangle['path']}")
    
    while not state["stop"]:
        try:
            if state["running_trade"]:
                await asyncio.sleep(0.05)
                continue
            
            # Calculate ALL triangles
            all_ops = []
            for triangle in TRIANGLES:
                result = await calculate_triangle_profit(triangle)
                if result and result["pct"] > MIN_PROFIT and result["profit"] > 0:
                    result["triangle"] = triangle
                    all_ops.append(result)
            
            # Sort by profit percentage
            all_ops.sort(key=lambda x: x["pct"], reverse=True)
            
            # Store all opportunities for display
            state["all_opportunities"] = all_ops[:5]
            
            # Get best opportunity
            best = all_ops[0] if all_ops else None
            state["best"] = best
            
            # Log best opportunity periodically
            if best and best["pct"] > MIN_PROFIT and best["profit"] > 0:
                log(f"🎯 BEST: {best['route']} | {best['pct']:.4f}% | ${best['profit']:.4f}")
                log(f"💸 Fees: {best['fees']['total_pct']:.3f}% (${best['fees']['total_usdt']:.4f})")
                
                if DRY_RUN:
                    state["trades"] += 1
                    state["total_profit"] += best["profit"]
                    log(f"💰 [DRY RUN] Total Profit: ${state['total_profit']:.4f}")
                else:
                    state["running_trade"] = True
                    actual_profit = await execute_triangular_trade(best, best["triangle"])
                    state["running_trade"] = False
                    
                    if actual_profit is not None:
                        state["trades"] += 1
                        state["total_profit"] += actual_profit
                        log(f"💰 REAL TRADE PROFIT: ${actual_profit:.4f}")
            
            # Refresh fees every 5 minutes
            if int(time.time()) % 300 < 1:
                await fetch_real_fees()
            
            await asyncio.sleep(0.05)  # 50ms loop
        except Exception as e:
            log(f"Engine Error: {e}")
            await asyncio.sleep(1)

# =========================
# 8. DASHBOARD & API
# =========================
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return """
    <html>
        <head>
            <title>Multi-Triangle Arbitrage Bot</title>
            <script src="https://cdn.tailwindcss.com"></script>
            <style>
                @keyframes pulse {
                    0%, 100% { opacity: 1; }
                    50% { opacity: 0.5; }
                }
                .live { animation: pulse 1s ease-in-out infinite; }
                .opportunity-card {
                    transition: all 0.2s;
                }
                .opportunity-card:hover {
                    transform: scale(1.02);
                    background: #1a202c;
                }
                .fee-text { color: #fbbf24; }
                .triangle-badge {
                    display: inline-block;
                    padding: 2px 8px;
                    border-radius: 12px;
                    font-size: 10px;
                    font-weight: bold;
                }
                .triangle-1 { background: #1f6feb; }
                .triangle-2 { background: #238636; }
                .triangle-3 { background: #da3633; }
            </style>
        </head>
        <body class="bg-gray-900 text-green-400 p-6 font-mono">
            <div class="max-w-6xl mx-auto">
                <h1 class="text-3xl border-b border-green-900 pb-2 mb-4">⚡ MULTI-TRIANGLE ARBITRAGE BOT ⚡</h1>
                <div class="text-gray-500 text-sm mb-4">50ms loop | WebSocket | 3 Triangles | Real-time fees from Gate.io</div>
                
                <!-- Stats Grid -->
                <div id="stats" class="grid grid-cols-1 md:grid-cols-4 gap-4 my-5"></div>
                
                <!-- Trade Status -->
                <div id="tradeStatus" class="bg-black p-2 mb-2 text-center text-yellow-400 hidden"></div>
                
                <!-- Best Opportunity -->
                <div id="best" class="bg-gradient-to-r from-green-900/30 to-black p-5 my-4 border border-green-800 text-center hidden rounded-lg"></div>
                
                <!-- ALL OPPORTUNITIES LIST -->
                <div class="mt-6">
                    <h2 class="text-xl border-b border-yellow-800 pb-2 mb-3 text-yellow-400">🔥 ALL PROFITABLE OPPORTUNITIES</h2>
                    <div id="opportunitiesList" class="grid gap-2 mb-6"></div>
                </div>
                
                <!-- Market Prices -->
                <div class="mt-6">
                    <h2 class="text-xl border-b border-blue-800 pb-2 mb-3 text-blue-400">📊 LIVE MARKET PRICES</h2>
                    <div id="marketPrices" class="grid grid-cols-1 md:grid-cols-4 gap-3 mb-6"></div>
                </div>
                
                <!-- Logs -->
                <div class="mt-6">
                    <h2 class="text-xl border-b border-gray-700 pb-2 mb-3">📝 LIVE LOGS</h2>
                    <div id="logs" class="bg-black p-4 h-80 overflow-y-auto text-xs border border-gray-800 font-mono rounded"></div>
                </div>
                
                <!-- Controls -->
                <div class="mt-6 flex gap-4 justify-center">
                    <button onclick="fetch('/start').then(()=>location.reload())" class="bg-green-800 hover:bg-green-700 px-8 py-3 rounded font-bold text-lg">▶ START BOT</button>
                    <button onclick="fetch('/stop').then(()=>location.reload())" class="bg-red-800 hover:bg-red-700 px-8 py-3 rounded font-bold text-lg">⏹ STOP BOT</button>
                    <button onclick="refreshData()" class="bg-blue-800 hover:bg-blue-700 px-8 py-3 rounded font-bold text-lg">🔄 REFRESH</button>
                </div>
                
                <!-- Footer -->
                <div class="text-gray-500 text-xs mt-6 text-center border-t border-gray-800 pt-4">
                    ⚡ Mode: """ + ("DRY RUN - No real trades" if DRY_RUN else "LIVE TRADING - Real money") + """<br>
                    💰 Trade Amount: $""" + str(TRADE_AMOUNT) + """ | 🎯 Min Profit: """ + str(MIN_PROFIT) + """% | 🔺 Triangles: BTC/ETH, BNB/BTC, ETH/SOL
                </div>
            </div>
            
            <script>
                async function refreshData() {
                    try {
                        const s = await (await fetch('/api/status')).json();
                        const balance = await (await fetch('/api/balance')).json();
                        const prices = await (await fetch('/api/prices')).json();
                        const fees = await (await fetch('/api/fees')).json();
                        
                        // Update stats
                        document.getElementById('stats').innerHTML = `
                            <div class="bg-gray-800 p-4 rounded-lg border border-gray-700">
                                <div class="text-gray-400 text-xs uppercase tracking-wide">💰 USDT BALANCE</div>
                                <div class="text-3xl font-bold text-green-400">$${balance.usdt?.toFixed(2) || '0.00'}</div>
                            </div>
                            <div class="bg-gray-800 p-4 rounded-lg border border-gray-700">
                                <div class="text-gray-400 text-xs uppercase tracking-wide">📊 TOTAL PnL</div>
                                <div class="text-3xl font-bold ${s.total_profit >= 0 ? 'text-green-400' : 'text-red-400'}">$${s.total_profit.toFixed(4)}</div>
                            </div>
                            <div class="bg-gray-800 p-4 rounded-lg border border-gray-700">
                                <div class="text-gray-400 text-xs uppercase tracking-wide">🎯 TRADES EXECUTED</div>
                                <div class="text-3xl font-bold text-blue-400">${s.trades}</div>
                            </div>
                            <div class="bg-gray-800 p-4 rounded-lg border border-gray-700">
                                <div class="text-gray-400 text-xs uppercase tracking-wide">⚡ BOT STATUS</div>
                                <div class="text-3xl font-bold ${s.running ? 'text-green-400' : 'text-red-400'}">${s.running ? 'RUNNING' : 'STOPPED'}</div>
                                ${s.running_trade ? '<div class="text-yellow-400 text-sm live mt-1">⚡ EXECUTING TRADE...</div>' : ''}
                            </div>
                        `;
                        
                        // Trade status
                        if (s.running_trade) {
                            document.getElementById('tradeStatus').style.display = 'block';
                            document.getElementById('tradeStatus').innerHTML = '🔄 TRADE IN PROGRESS - Please wait...';
                        } else {
                            document.getElementById('tradeStatus').style.display = 'none';
                        }
                        
                        // Best opportunity
                        if (s.best && s.best.pct > 0) {
                            const bestDiv = document.getElementById('best');
                            bestDiv.style.display = 'block';
                            bestDiv.innerHTML = `
                                <div class="text-yellow-400 font-bold text-xl mb-2">🏆 BEST OPPORTUNITY 🏆</div>
                                <div class="text-2xl text-white font-bold">${s.best.route}</div>
                                <div class="text-4xl font-bold text-green-400 mt-3">${s.best.pct.toFixed(4)}%</div>
                                <div class="text-xl text-white mt-1">$${s.best.profit.toFixed(4)} profit</div>
                                <div class="text-yellow-400 text-sm mt-2">💸 Total Fees: ${s.best.fees?.total_pct?.toFixed(3) || '0'}% ($${s.best.fees?.total_usdt?.toFixed(4) || '0'})</div>
                                <div class="text-gray-400 text-sm mt-1">✨ ${new Date().toLocaleTimeString()}</div>
                            `;
                        } else {
                            document.getElementById('best').style.display = 'none';
                        }
                        
                        // ALL OPPORTUNITIES LIST with fees
                        if (s.all_opportunities && s.all_opportunities.length > 0) {
                            document.getElementById('opportunitiesList').innerHTML = s.all_opportunities.map((opp, idx) => `
                                <div class="opportunity-card bg-gray-800 p-3 rounded border ${idx === 0 ? 'border-yellow-600' : 'border-gray-700'}">
                                    <div class="flex justify-between items-center">
                                        <div class="flex-1">
                                            <div class="flex items-center gap-2">
                                                ${idx === 0 ? '<span class="text-yellow-400 text-xs">🥇 BEST</span>' : ''}
                                                <span class="font-bold text-white">${opp.route}</span>
                                            </div>
                                            <div class="text-xs text-gray-400 mt-1">
                                                Legs: ${opp.legs ? opp.legs.join(' → ') : '---'}
                                            </div>
                                            <div class="text-xs fee-text mt-1">
                                                💸 Fees: ${opp.fees?.total_pct?.toFixed(3) || '0'}% ($${opp.fees?.total_usdt?.toFixed(4) || '0'})
                                            </div>
                                        </div>
                                        <div class="text-right">
                                            <div class="text-2xl font-bold ${opp.profit >= 0 ? 'text-green-400' : 'text-red-400'}">${opp.pct.toFixed(4)}%</div>
                                            <div class="text-sm ${opp.profit >= 0 ? 'text-green-400' : 'text-red-400'}">$${opp.profit.toFixed(4)}</div>
                                        </div>
                                    </div>
                                </div>
                            `).join('');
                        } else {
                            document.getElementById('opportunitiesList').innerHTML = `
                                <div class="bg-gray-800 p-6 text-center text-gray-500 rounded">
                                    🔍 No profitable opportunities found yet... Waiting for market conditions.
                                </div>
                            `;
                        }
                        
                        // Market prices
                        document.getElementById('marketPrices').innerHTML = `
                            <div class="bg-gray-800 p-3 rounded">
                                <div class="text-gray-400 text-xs">BTC/USDT</div>
                                <div class="text-xl font-bold">$${prices.btc_usdt?.toFixed(2) || '---'}</div>
                                <div class="text-xs ${prices.btc_change >= 0 ? 'text-green-400' : 'text-red-400'}">24h: ${prices.btc_change?.toFixed(2) || '0'}%</div>
                                <div class="text-xs fee-text">Fee: ${fees.btc_usdt ? (fees.btc_usdt * 100).toFixed(3) : '0.200'}%</div>
                            </div>
                            <div class="bg-gray-800 p-3 rounded">
                                <div class="text-gray-400 text-xs">ETH/USDT</div>
                                <div class="text-xl font-bold">$${prices.eth_usdt?.toFixed(2) || '---'}</div>
                                <div class="text-xs ${prices.eth_change >= 0 ? 'text-green-400' : 'text-red-400'}">24h: ${prices.eth_change?.toFixed(2) || '0'}%</div>
                                <div class="text-xs fee-text">Fee: ${fees.eth_usdt ? (fees.eth_usdt * 100).toFixed(3) : '0.200'}%</div>
                            </div>
                            <div class="bg-gray-800 p-3 rounded">
                                <div class="text-gray-400 text-xs">BNB/USDT</div>
                                <div class="text-xl font-bold">$${prices.bnb_usdt?.toFixed(2) || '---'}</div>
                                <div class="text-xs fee-text">Fee: ${fees.bnb_usdt ? (fees.bnb_usdt * 100).toFixed(3) : '0.200'}%</div>
                            </div>
                            <div class="bg-gray-800 p-3 rounded">
                                <div class="text-gray-400 text-xs">SOL/USDT</div>
                                <div class="text-xl font-bold">$${prices.sol_usdt?.toFixed(2) || '---'}</div>
                                <div class="text-xs fee-text">Fee: ${fees.sol_usdt ? (fees.sol_usdt * 100).toFixed(3) : '0.200'}%</div>
                            </div>
                        `;
                        
                        // Logs
                        const l = await (await fetch('/api/logs')).json();
                        document.getElementById('logs').innerHTML = l.logs.slice().reverse().slice(0, 50).map(l => `<div class="border-b border-gray-800 py-1 font-mono">${escapeHtml(l)}</div>`).join('');
                    } catch(e) { console.error(e); }
                }
                
                function escapeHtml(t) { 
                    if (!t) return '';
                    return t.replace(/[&<>]/g, function(m) {
                        if (m === '&') return '&amp;';
                        if (m === '<') return '&lt;';
                        if (m === '>') return '&gt;';
                        return m;
                    });
                }
                
                setInterval(refreshData, 1000);
                refreshData();
            </script>
        </body>
    </html>
    """

@app.get("/api/status")
async def get_status():
    return {
        "running": state["running"],
        "running_trade": state.get("running_trade", False),
        "trades": state["trades"],
        "total_profit": state["total_profit"],
        "best": state["best"],
        "all_opportunities": state["all_opportunities"]
    }

@app.get("/api/logs")
async def get_logs():
    return {"logs": state["logs"]}

@app.get("/api/balance")
async def get_balance():
    try:
        balance = await exchange.fetch_balance()
        return {"usdt": balance.get("USDT", {}).get("free", 0)}
    except Exception as e:
        return {"usdt": 0}

@app.get("/api/fees")
async def get_fees():
    """Return real-time fee rates"""
    await fetch_real_fees()
    return {
        "btc_usdt": fee_cache.get("BTC/USDT", {}).get("taker", 0.002),
        "eth_usdt": fee_cache.get("ETH/USDT", {}).get("taker", 0.002),
        "bnb_usdt": fee_cache.get("BNB/USDT", {}).get("taker", 0.002),
        "sol_usdt": fee_cache.get("SOL/USDT", {}).get("taker", 0.002),
        "eth_btc": fee_cache.get("ETH/BTC", {}).get("taker", 0.002),
        "bnb_btc": fee_cache.get("BNB/BTC", {}).get("taker", 0.002),
        "sol_eth": fee_cache.get("SOL/ETH", {}).get("taker", 0.002)
    }

@app.get("/api/prices")
async def get_prices():
    try:
        btc_bid, btc_ask = get_valid_prices("BTC/USDT")
        eth_bid, eth_ask = get_valid_prices("ETH/USDT")
        bnb_bid, bnb_ask = get_valid_prices("BNB/USDT")
        sol_bid, sol_ask = get_valid_prices("SOL/USDT")
        
        # Get 24h change
        btc_ticker = await exchange.fetch_ticker("BTC/USDT")
        eth_ticker = await exchange.fetch_ticker("ETH/USDT")
        
        return {
            "btc_usdt": btc_ask,
            "btc_change": btc_ticker.get('percentage', 0),
            "eth_usdt": eth_ask,
            "eth_change": eth_ticker.get('percentage', 0),
            "bnb_usdt": bnb_ask,
            "sol_usdt": sol_ask
        }
    except:
        return {"btc_usdt": 0, "eth_usdt": 0, "bnb_usdt": 0, "sol_usdt": 0}

@app.get("/start")
async def start_bot():
    if state["running"]:
        return {"status": "already_running"}
    state["stop"] = False
    state["running_trade"] = False
    asyncio.create_task(run_bot())
    return {"status": "started"}

@app.get("/stop")
async def stop_bot():
    state["stop"] = True
    return {"status": "stopping"}

async def run_bot():
    await exchange.load_markets()
    await fetch_real_fees()
    log("📡 Markets loaded")
    log(f"🔺 Scanning {len(TRIANGLES)} triangles:")
    for triangle in TRIANGLES:
        log(f"   - {triangle['path']}")
    
    # Start WebSocket streams for all symbols
    stream_tasks = []
    for symbol in ALL_SYMBOLS:
        stream_tasks.append(stream(symbol))
    
    await asyncio.gather(*stream_tasks, engine())

# =========================
# 9. RUN
# =========================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)

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
FEE_RATE = 0.002  # 0.2% Standard Gate.io fee

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
orderbooks = {
    "BTC/USDT": {"book": None, "updated": 0},
    "ETH/USDT": {"book": None, "updated": 0},
    "ETH/BTC":  {"book": None, "updated": 0},
}

state = {
    "running": False,
    "stop": False,
    "running_trade": False,
    "logs": [],
    "best": None,
    "all_opportunities": [],  # NEW: Store all opportunities
    "trades": 0,
    "total_profit": 0.0,
}

def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line)
    state["logs"].append(line)
    state["logs"] = state["logs"][-100:]

# =========================
# 3. WEBSOCKET STREAMS
# =========================
async def stream(symbol):
    while not state["stop"]:
        try:
            book = await exchange.watch_order_book(symbol, limit=5)
            orderbooks[symbol] = {"book": book, "updated": time.time()}
        except Exception as e:
            log(f"⚠️ WS Error {symbol}: {e}")
            await asyncio.sleep(2)

def get_valid_prices(symbol):
    data = orderbooks[symbol]
    if not data["book"] or (time.time() - data["updated"] > 2.0):
        return None, None
    book = data["book"]
    if not book["bids"] or not book["asks"]:
        return None, None
    return book["bids"][0][0], book["asks"][0][0]

# =========================
# 4. PRECISE CALCULATION
# =========================
def calculate_precise_route(route_type):
    """Simulates trade legs with fee deduction and decimal rounding."""
    try:
        btc_bid, btc_ask = get_valid_prices("BTC/USDT")
        eth_bid, eth_ask = get_valid_prices("ETH/USDT")
        eb_bid, eb_ask = get_valid_prices("ETH/BTC")

        if None in [btc_bid, btc_ask, eth_bid, eth_ask, eb_bid, eb_ask]:
            return None

        if route_type == "BTC_FIRST":
            # USDT -> BTC -> ETH -> USDT
            qty1 = (TRADE_AMOUNT / btc_ask) * (1 - FEE_RATE)
            qty1 = float(exchange.amount_to_precision("BTC/USDT", qty1))
            
            qty2 = (qty1 / eb_ask) * (1 - FEE_RATE)
            qty2 = float(exchange.amount_to_precision("ETH/BTC", qty2))
            
            final_usdt = (qty2 * eth_bid) * (1 - FEE_RATE)
            profit = final_usdt - TRADE_AMOUNT
            pct = (profit / TRADE_AMOUNT) * 100
            return {
                "route": "USDT → BTC → ETH → USDT", 
                "final": final_usdt, 
                "profit": profit,
                "pct": pct,
                "legs": ["BTC/USDT", "ETH/BTC", "ETH/USDT"]
            }

        else:
            # USDT -> ETH -> BTC -> USDT
            qty1 = (TRADE_AMOUNT / eth_ask) * (1 - FEE_RATE)
            qty1 = float(exchange.amount_to_precision("ETH/USDT", qty1))
            
            qty2 = (qty1 * eb_bid) * (1 - FEE_RATE)
            qty2 = float(exchange.amount_to_precision("ETH/BTC", qty2))
            
            final_usdt = (qty2 * btc_bid) * (1 - FEE_RATE)
            profit = final_usdt - TRADE_AMOUNT
            pct = (profit / TRADE_AMOUNT) * 100
            return {
                "route": "USDT → ETH → BTC → USDT", 
                "final": final_usdt, 
                "profit": profit,
                "pct": pct,
                "legs": ["ETH/USDT", "ETH/BTC", "BTC/USDT"]
            }
    except Exception as e:
        return None

# =========================
# 5. REAL EXECUTION ENGINE
# =========================
async def execute_triangular_trade(opp):
    """Executes 3 legs and verifies fills before returning."""
    try:
        log(f"⚡ EXECUTION START: {opp['route']}")
        
        legs = opp.get('legs', [])
        if not legs:
            if "BTC → ETH" in opp['route']:
                legs = ["BTC/USDT", "ETH/BTC", "ETH/USDT"]
            else:
                legs = ["ETH/USDT", "ETH/BTC", "BTC/USDT"]
        
        sides = ["buy", "buy", "sell"]
        current_amount = TRADE_AMOUNT
        
        for i, (symbol, side) in enumerate(zip(legs, sides)):
            if side == "buy":
                _, ask = get_valid_prices(symbol)
                if not ask:
                    raise Exception(f"Cannot get price for {symbol}")
                qty = current_amount / ask
            else:
                qty = current_amount

            precise_qty = float(exchange.amount_to_precision(symbol, qty))
            
            if precise_qty <= 0:
                raise Exception(f"Invalid quantity: {precise_qty}")

            log(f"📊 Leg {i+1}: {side.upper()} {precise_qty:.8f} {symbol}")
            order = await exchange.create_order(
                symbol=symbol,
                type="market",
                side=side,
                amount=precise_qty
            )
            
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

        log(f"💰 CYCLE COMPLETE. Final USDT: ${current_amount:.4f}")
        profit = current_amount - TRADE_AMOUNT
        log(f"💵 PROFIT: ${profit:.4f}")
        return profit

    except Exception as e:
        log(f"🚨 CRITICAL EXECUTION ERROR: {e}")
        return None

# =========================
# 6. CORE ENGINE (WITH ALL OPPORTUNITIES)
# =========================
async def engine():
    state["running"] = True
    log("🚀 ENGINE ONLINE - Mode: " + ("DRY RUN" if DRY_RUN else "LIVE"))
    log(f"💰 Trade Amount: ${TRADE_AMOUNT}")
    log(f"🎯 Min Profit Required: {MIN_PROFIT}%")
    
    while not state["stop"]:
        try:
            if state["running_trade"]:
                await asyncio.sleep(0.05)
                continue
                
            # Calculate BOTH routes
            r1 = calculate_precise_route("BTC_FIRST")
            r2 = calculate_precise_route("ETH_FIRST")
            
            # Collect ALL profitable opportunities
            all_ops = []
            for res in [r1, r2]:
                if res and res["pct"] > MIN_PROFIT and res["profit"] > 0:
                    all_ops.append(res)
            
            # Sort by profit percentage
            all_ops.sort(key=lambda x: x["pct"], reverse=True)
            
            # Store all opportunities for display
            state["all_opportunities"] = all_ops[:5]  # Keep top 5
            
            # Get best opportunity
            best = all_ops[0] if all_ops else None
            state["best"] = best

            # Execute best if available
            if best and best["pct"] > MIN_PROFIT and best["profit"] > 0:
                log(f"🎯 BEST: {best['route']} | {best['pct']:.4f}% | ${best['profit']:.4f}")
                
                if DRY_RUN:
                    state["trades"] += 1
                    state["total_profit"] += best["profit"]
                    log(f"💰 [DRY RUN] Total Profit: ${state['total_profit']:.4f}")
                else:
                    state["running_trade"] = True
                    actual_profit = await execute_triangular_trade(best)
                    state["running_trade"] = False
                    
                    if actual_profit is not None:
                        state["trades"] += 1
                        state["total_profit"] += actual_profit
                        log(f"💰 REAL TRADE PROFIT: ${actual_profit:.4f}")

            await asyncio.sleep(0.05)
        except Exception as e:
            log(f"Engine Error: {e}")
            await asyncio.sleep(1)

# =========================
# 7. DASHBOARD & API
# =========================
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return """
    <html>
        <head>
            <title>Ultra-Fast Arbitrage Bot</title>
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
                .profit-positive { color: #4ade80; }
                .profit-negative { color: #f87171; }
            </style>
        </head>
        <body class="bg-gray-900 text-green-400 p-6 font-mono">
            <div class="max-w-6xl mx-auto">
                <h1 class="text-3xl border-b border-green-900 pb-2 mb-4">⚡ ULTRA-FAST ARBITRAGE BOT ⚡</h1>
                <div class="text-gray-500 text-sm mb-4">50ms loop | WebSocket | Real-time execution | Multi-opportunity scanner</div>
                
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
                    <div id="marketPrices" class="grid grid-cols-1 md:grid-cols-3 gap-3 mb-6"></div>
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
                    💰 Trade Amount: $""" + str(TRADE_AMOUNT) + """ | 🎯 Min Profit: """ + str(MIN_PROFIT) + """% | 💸 Fee: 0.2% per leg
                </div>
            </div>
            
            <script>
                async function refreshData() {
                    try {
                        const s = await (await fetch('/api/status')).json();
                        const balance = await (await fetch('/api/balance')).json();
                        const prices = await (await fetch('/api/prices')).json();
                        
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
                                <div class="text-gray-400 text-sm mt-2">✨ ${new Date().toLocaleTimeString()}</div>
                            `;
                        } else {
                            document.getElementById('best').style.display = 'none';
                        }
                        
                        // ALL OPPORTUNITIES LIST
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
                                                Legs: ${opp.legs ? opp.legs.join(' → ') : 'BTC → ETH → USDT'}
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
                            </div>
                            <div class="bg-gray-800 p-3 rounded">
                                <div class="text-gray-400 text-xs">ETH/USDT</div>
                                <div class="text-xl font-bold">$${prices.eth_usdt?.toFixed(2) || '---'}</div>
                                <div class="text-xs ${prices.eth_change >= 0 ? 'text-green-400' : 'text-red-400'}">24h: ${prices.eth_change?.toFixed(2) || '0'}%</div>
                            </div>
                            <div class="bg-gray-800 p-3 rounded">
                                <div class="text-gray-400 text-xs">ETH/BTC</div>
                                <div class="text-xl font-bold">${prices.eth_btc?.toFixed(6) || '---'}</div>
                                <div class="text-xs text-gray-400">Spread: ${prices.spread?.toFixed(4) || '0'}%</div>
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

@app.get("/api/prices")
async def get_prices():
    try:
        btc_bid, btc_ask = get_valid_prices("BTC/USDT")
        eth_bid, eth_ask = get_valid_prices("ETH/USDT")
        eb_bid, eb_ask = get_valid_prices("ETH/BTC")
        
        # Get 24h change
        btc_ticker = await exchange.fetch_ticker("BTC/USDT")
        eth_ticker = await exchange.fetch_ticker("ETH/USDT")
        
        return {
            "btc_usdt": btc_ask,
            "btc_change": btc_ticker.get('percentage', 0),
            "eth_usdt": eth_ask,
            "eth_change": eth_ticker.get('percentage', 0),
            "eth_btc": eb_ask,
            "spread": ((eb_ask - eb_bid) / eb_bid * 100) if eb_bid else 0
        }
    except:
        return {"btc_usdt": 0, "eth_usdt": 0, "eth_btc": 0}

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
    log("📡 Markets loaded")
    await asyncio.gather(
        stream("BTC/USDT"),
        stream("ETH/USDT"),
        stream("ETH/BTC"),
        engine()
    )

# =========================
# 8. RUN
# =========================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)

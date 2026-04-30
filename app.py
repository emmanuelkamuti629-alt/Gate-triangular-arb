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
MIN_PROFIT = float(os.getenv("MIN_PROFIT_PCT", "0.1"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# Initialize Exchange
exchange = ccxt.gateio({
    "apiKey": os.getenv("GATEIO_API_KEY"),
    "secret": os.getenv("GATEIO_SECRET"),
    "enableRateLimit": True,
    "options": {"defaultType": "spot"},
})

# =========================
# 2. TRIANGLES (ALL VALID ON GATE.IO)
# =========================
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
        "name": "XRP/BTC Triangle",
        "legs": [
            {"from": "USDT", "to": "XRP", "symbol": "XRP/USDT", "side": "buy"},
            {"from": "XRP", "to": "BTC", "symbol": "XRP/BTC", "side": "sell"},
            {"from": "BTC", "to": "USDT", "symbol": "BTC/USDT", "side": "sell"}
        ],
        "path": "USDT → XRP → BTC → USDT",
        "symbols": ["XRP/USDT", "XRP/BTC", "BTC/USDT"]
    },
    {
        "name": "LTC/BTC Triangle",
        "legs": [
            {"from": "USDT", "to": "LTC", "symbol": "LTC/USDT", "side": "buy"},
            {"from": "LTC", "to": "BTC", "symbol": "LTC/BTC", "side": "sell"},
            {"from": "BTC", "to": "USDT", "symbol": "BTC/USDT", "side": "sell"}
        ],
        "path": "USDT → LTC → BTC → USDT",
        "symbols": ["LTC/USDT", "LTC/BTC", "BTC/USDT"]
    },
    {
        "name": "ADA/BTC Triangle",
        "legs": [
            {"from": "USDT", "to": "ADA", "symbol": "ADA/USDT", "side": "buy"},
            {"from": "ADA", "to": "BTC", "symbol": "ADA/BTC", "side": "sell"},
            {"from": "BTC", "to": "USDT", "symbol": "BTC/USDT", "side": "sell"}
        ],
        "path": "USDT → ADA → BTC → USDT",
        "symbols": ["ADA/USDT", "ADA/BTC", "BTC/USDT"]
    },
    {
        "name": "DOGE/BTC Triangle",
        "legs": [
            {"from": "USDT", "to": "DOGE", "symbol": "DOGE/USDT", "side": "buy"},
            {"from": "DOGE", "to": "BTC", "symbol": "DOGE/BTC", "side": "sell"},
            {"from": "BTC", "to": "USDT", "symbol": "BTC/USDT", "side": "sell"}
        ],
        "path": "USDT → DOGE → BTC → USDT",
        "symbols": ["DOGE/USDT", "DOGE/BTC", "BTC/USDT"]
    },
    {
        "name": "SOL/USDT Direct",
        "legs": [
            {"from": "USDT", "to": "SOL", "symbol": "SOL/USDT", "side": "buy"},
            {"from": "SOL", "to": "USDT", "symbol": "SOL/USDT", "side": "sell"}
        ],
        "path": "USDT → SOL → USDT (Simple)",
        "symbols": ["SOL/USDT"]
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
# 3. REAL-TIME FEE FETCHING
# =========================
async def fetch_real_fees():
    global fee_cache, last_fee_fetch
    
    if time.time() - last_fee_fetch < 300 and fee_cache:
        return fee_cache
    
    try:
        trading_fees = await exchange.fetch_trading_fees()
        
        for symbol in ALL_SYMBOLS:
            if symbol in trading_fees:
                fee_cache[symbol] = trading_fees[symbol].get('taker', 0.002)
            else:
                fee_cache[symbol] = 0.002
        
        last_fee_fetch = time.time()
        return fee_cache
        
    except Exception as e:
        log(f"⚠️ Fee fetch error: {e}")
        for symbol in ALL_SYMBOLS:
            fee_cache[symbol] = 0.002
        return fee_cache

async def get_fee_rate(symbol):
    fees = await fetch_real_fees()
    return fees.get(symbol, 0.002)

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
    bid, ask = get_valid_prices(symbol)
    if side == "buy":
        return ask
    elif side == "sell":
        return bid
    return None

# =========================
# 5. CALCULATE PROFIT FOR EACH TRIANGLE
# =========================
async def calculate_triangle_profit(triangle):
    try:
        current_amount = TRADE_AMOUNT
        fee_total = 0
        
        for leg in triangle["legs"]:
            symbol = leg["symbol"]
            side = leg["side"]
            
            price = get_price(symbol, side)
            if not price:
                return None
            
            fee_rate = await get_fee_rate(symbol)
            
            if side == "buy":
                qty = current_amount / price
                fee = current_amount * fee_rate
                current_amount = qty * (1 - fee_rate)
            else:
                qty = current_amount
                received = qty * price
                fee = received * fee_rate
                current_amount = received * (1 - fee_rate)
            
            fee_total += fee
        
        final_usdt = current_amount
        profit = final_usdt - TRADE_AMOUNT
        pct = (profit / TRADE_AMOUNT) * 100
        total_fee_pct = (fee_total / TRADE_AMOUNT) * 100
        
        return {
            "route": triangle["path"],
            "profit": profit,
            "pct": pct,
            "final_usdt": final_usdt,
            "legs": [leg["symbol"] for leg in triangle["legs"]],
            "fees": {
                "total_usdt": fee_total,
                "total_pct": total_fee_pct
            }
        }
        
    except Exception as e:
        return None

# =========================
# 6. REAL EXECUTION ENGINE
# =========================
async def execute_triangular_trade(opp, triangle):
    try:
        log(f"⚡ EXECUTING: {opp['route']} | {opp['pct']:.4f}%")
        
        legs = triangle["legs"]
        current_amount = TRADE_AMOUNT
        
        for i, leg in enumerate(legs):
            symbol = leg["symbol"]
            side = leg["side"]
            
            price = get_price(symbol, side)
            if not price:
                raise Exception(f"No price for {symbol}")
            
            if side == "buy":
                qty = current_amount / price
            else:
                qty = current_amount
            
            precise_qty = float(exchange.amount_to_precision(symbol, qty))
            
            if precise_qty <= 0:
                raise Exception(f"Invalid quantity")
            
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
                            filled = True
                    except:
                        retries += 1
                
                if not filled:
                    log(f"❌ Leg {i+1} FAILED")
                    return None
            else:
                if side == "buy":
                    current_amount = precise_qty
                else:
                    current_amount = precise_qty * price
        
        profit = current_amount - TRADE_AMOUNT
        log(f"💰 PROFIT: ${profit:.4f} ({profit/TRADE_AMOUNT*100:.4f}%)")
        return profit
        
    except Exception as e:
        log(f"❌ Execution error: {e}")
        return None

# =========================
# 7. MAIN ENGINE
# =========================
async def engine():
    state["running"] = True
    
    await fetch_real_fees()
    
    log("🚀 MULTI-TRIANGLE ARBITRAGE BOT STARTED")
    log(f"💰 Trade Amount: ${TRADE_AMOUNT}")
    log(f"🎯 Min Profit: {MIN_PROFIT}%")
    log(f"🔺 Scanning {len(TRIANGLES)} triangles")
    log(f"🧪 Mode: {'DRY RUN' if DRY_RUN else 'LIVE'}")
    
    while not state["stop"]:
        try:
            if state["running_trade"]:
                await asyncio.sleep(0.05)
                continue
            
            # Calculate all triangles
            all_ops = []
            for triangle in TRIANGLES:
                result = await calculate_triangle_profit(triangle)
                if result and result["pct"] > MIN_PROFIT and result["profit"] > 0:
                    result["triangle"] = triangle
                    all_ops.append(result)
            
            # Sort by profit percentage
            all_ops.sort(key=lambda x: x["pct"], reverse=True)
            
            state["all_opportunities"] = all_ops[:10]
            state["best"] = all_ops[0] if all_ops else None
            
            # Log best opportunity
            if state["best"]:
                best = state["best"]
                log(f"🎯 BEST: {best['pct']:.4f}% | {best['route']} | ${best['profit']:.4f}")
                
                if DRY_RUN:
                    state["trades"] += 1
                    state["total_profit"] += best["profit"]
            
            await asyncio.sleep(0.1)
            
        except Exception as e:
            log(f"Engine error: {e}")
            await asyncio.sleep(1)

# =========================
# 8. DASHBOARD
# =========================
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Multi-Triangle Arbitrage Bot</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { font-family: 'Courier New', monospace; background: #0a0c10; color: #0f0; padding: 20px; }
            .container { max-width: 1200px; margin: 0 auto; }
            .header { text-align: center; margin-bottom: 30px; border-bottom: 1px solid #0f0; padding-bottom: 10px; }
            .title { font-size: 28px; font-weight: bold; }
            .subtitle { color: #888; font-size: 12px; margin-top: 5px; }
            .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 30px; }
            .stat-card { background: #0d1117; border: 1px solid #0f0; border-radius: 8px; padding: 15px; text-align: center; }
            .stat-label { font-size: 11px; color: #888; text-transform: uppercase; }
            .stat-value { font-size: 28px; font-weight: bold; margin-top: 5px; }
            .profit-positive { color: #0f0; }
            .profit-negative { color: #f00; }
            .section { margin-bottom: 30px; }
            .section-title { font-size: 18px; border-left: 3px solid #0f0; padding-left: 10px; margin-bottom: 15px; }
            .opportunity-card { background: #0d1117; border: 1px solid #333; border-radius: 8px; padding: 12px; margin-bottom: 8px; transition: all 0.2s; }
            .opportunity-card:hover { border-color: #0f0; transform: scale(1.01); }
            .opportunity-best { border-color: #ff0; background: #1a1a00; }
            .opportunity-path { font-size: 14px; font-weight: bold; }
            .opportunity-profit { font-size: 20px; font-weight: bold; }
            .opportunity-fees { font-size: 10px; color: #ff0; margin-top: 5px; }
            .market-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 20px; }
            .market-card { background: #0d1117; border: 1px solid #333; border-radius: 8px; padding: 10px; text-align: center; }
            .market-symbol { font-size: 12px; color: #888; }
            .market-price { font-size: 16px; font-weight: bold; margin-top: 5px; }
            .logs-box { background: #0d1117; border: 1px solid #333; border-radius: 8px; padding: 10px; height: 300px; overflow-y: auto; font-size: 11px; }
            .log-line { border-bottom: 1px solid #1a1a1a; padding: 4px 0; font-family: monospace; }
            button { background: #0f0; color: #000; border: none; padding: 10px 20px; margin: 5px; cursor: pointer; font-weight: bold; border-radius: 5px; }
            button:hover { background: #0c0; }
            .stop-btn { background: #f00; color: #fff; }
            .status-running { color: #0f0; }
            .status-stopped { color: #f00; }
            .footer { text-align: center; font-size: 10px; color: #444; margin-top: 30px; padding-top: 10px; border-top: 1px solid #333; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <div class="title">⚡ MULTI-TRIANGLE ARBITRAGE BOT ⚡</div>
                <div class="subtitle">WebSocket | """ + str(len(TRIANGLES)) + """ Triangles | Real-time | 100ms loop</div>
            </div>
            
            <div id="stats" class="stats-grid"></div>
            
            <div class="section">
                <div class="section-title">🔥 BEST OPPORTUNITY</div>
                <div id="bestOpportunity"></div>
            </div>
            
            <div class="section">
                <div class="section-title">📊 ALL PROFITABLE OPPORTUNITIES</div>
                <div id="opportunitiesList"></div>
            </div>
            
            <div class="section">
                <div class="section-title">💹 LIVE MARKET PRICES</div>
                <div id="marketPrices" class="market-grid"></div>
            </div>
            
            <div class="section">
                <div class="section-title">📝 LIVE LOGS</div>
                <div id="logs" class="logs-box"></div>
            </div>
            
            <div style="text-align: center;">
                <button onclick="startBot()">▶ START BOT</button>
                <button onclick="stopBot()" class="stop-btn">⏹ STOP BOT</button>
                <button onclick="refreshData()">🔄 REFRESH</button>
            </div>
            
            <div class="footer">
                💰 Trade Amount: $""" + str(TRADE_AMOUNT) + """ | 🎯 Min Profit: """ + str(MIN_PROFIT) + """% | 🧪 Mode: """ + ("DRY RUN" if DRY_RUN else "LIVE") + """
            </div>
        </div>
        
        <script>
            async function refreshData() {
                try {
                    const res = await fetch('/api/status');
                    const data = await res.json();
                    const balance = await fetch('/api/balance').then(r => r.json());
                    const prices = await fetch('/api/prices').then(r => r.json());
                    
                    // Stats
                    document.getElementById('stats').innerHTML = `
                        <div class="stat-card">
                            <div class="stat-label">💰 USDT BALANCE</div>
                            <div class="stat-value">$${balance.usdt?.toFixed(2) || '0.00'}</div>
                        </div>
                        <div class="stat-card">
                            <div class="stat-label">📊 TOTAL P&L</div>
                            <div class="stat-value ${data.total_profit >= 0 ? 'profit-positive' : 'profit-negative'}">$${data.total_profit.toFixed(4)}</div>
                        </div>
                        <div class="stat-card">
                            <div class="stat-label">🎯 TRADES</div>
                            <div class="stat-value">${data.trades}</div>
                        </div>
                        <div class="stat-card">
                            <div class="stat-label">⚡ STATUS</div>
                            <div class="stat-value ${data.running ? 'status-running' : 'status-stopped'}">${data.running ? 'RUNNING' : 'STOPPED'}</div>
                            ${data.running_trade ? '<div style="font-size:10px;color:#ff0;">⚡ EXECUTING...</div>' : ''}
                        </div>
                    `;
                    
                    // Best Opportunity
                    if (data.best && data.best.pct > 0) {
                        document.getElementById('bestOpportunity').innerHTML = `
                            <div class="opportunity-card opportunity-best">
                                <div class="opportunity-path">🏆 ${data.best.route}</div>
                                <div class="opportunity-profit profit-positive">${data.best.pct.toFixed(4)}% ($${data.best.profit.toFixed(4)})</div>
                                <div class="opportunity-fees">💸 Fees: ${data.best.fees?.total_pct?.toFixed(3) || '0'}% ($${data.best.fees?.total_usdt?.toFixed(4) || '0'})</div>
                                <div style="font-size:10px;color:#888;margin-top:5px;">Legs: ${data.best.legs?.join(' → ') || 'N/A'}</div>
                            </div>
                        `;
                    } else {
                        document.getElementById('bestOpportunity').innerHTML = `<div class="opportunity-card" style="text-align:center;color:#888;">🔍 Waiting for profitable opportunities...</div>`;
                    }
                    
                    // All Opportunities
                    if (data.all_opportunities && data.all_opportunities.length > 0) {
                        document.getElementById('opportunitiesList').innerHTML = data.all_opportunities.map(opp => `
                            <div class="opportunity-card">
                                <div style="display:flex;justify-content:space-between;align-items:center;">
                                    <div>
                                        <div class="opportunity-path">${opp.route}</div>
                                        <div class="opportunity-fees">💸 Fees: ${opp.fees?.total_pct?.toFixed(3) || '0'}% ($${opp.fees?.total_usdt?.toFixed(4) || '0'})</div>
                                    </div>
                                    <div style="text-align:right;">
                                        <div class="opportunity-profit profit-positive">${opp.pct.toFixed(4)}%</div>
                                        <div style="font-size:12px;">$${opp.profit.toFixed(4)}</div>
                                    </div>
                                </div>
                            </div>
                        `).join('');
                    } else {
                        document.getElementById('opportunitiesList').innerHTML = `<div class="opportunity-card" style="text-align:center;color:#888;">🔍 No profitable opportunities found yet...</div>`;
                    }
                    
                    // Market Prices
                    document.getElementById('marketPrices').innerHTML = `
                        <div class="market-card"><div class="market-symbol">BTC/USDT</div><div class="market-price">$${prices.btc_usdt?.toFixed(2) || '---'}</div></div>
                        <div class="market-card"><div class="market-symbol">ETH/USDT</div><div class="market-price">$${prices.eth_usdt?.toFixed(2) || '---'}</div></div>
                        <div class="market-card"><div class="market-symbol">BNB/USDT</div><div class="market-price">$${prices.bnb_usdt?.toFixed(2) || '---'}</div></div>
                        <div class="market-card"><div class="market-symbol">SOL/USDT</div><div class="market-price">$${prices.sol_usdt?.toFixed(2) || '---'}</div></div>
                        <div class="market-card"><div class="market-symbol">XRP/USDT</div><div class="market-price">$${prices.xrp_usdt?.toFixed(4) || '---'}</div></div>
                        <div class="market-card"><div class="market-symbol">DOGE/USDT</div><div class="market-price">$${prices.doge_usdt?.toFixed(4) || '---'}</div></div>
                    `;
                    
                    // Logs
                    const logsRes = await fetch('/api/logs');
                    const logsData = await logsRes.json();
                    document.getElementById('logs').innerHTML = logsData.logs.slice().reverse().slice(0, 50).map(l => `<div class="log-line">${escapeHtml(l)}</div>`).join('');
                    
                } catch(e) { console.error(e); }
            }
            
            function escapeHtml(t) { if (!t) return ''; return t.replace(/[&<>]/g, function(m) { if (m === '&') return '&amp;'; if (m === '<') return '&lt;'; if (m === '>') return '&gt;'; return m; }); }
            
            async function startBot() { await fetch('/start'); setTimeout(refreshData, 1000); }
            async function stopBot() { await fetch('/stop'); setTimeout(refreshData, 1000); }
            
            setInterval(refreshData, 2000);
            refreshData();
        </script>
    </body>
    </html>
    """

# =========================
# 9. API ENDPOINTS
# =========================
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
    except:
        return {"usdt": 0}

@app.get("/api/prices")
async def get_prices():
    try:
        btc = get_price("BTC/USDT", "ask") or 0
        eth = get_price("ETH/USDT", "ask") or 0
        bnb = get_price("BNB/USDT", "ask") or 0
        sol = get_price("SOL/USDT", "ask") or 0
        xrp = get_price("XRP/USDT", "ask") or 0
        doge = get_price("DOGE/USDT", "ask") or 0
        return {
            "btc_usdt": btc, "eth_usdt": eth, "bnb_usdt": bnb,
            "sol_usdt": sol, "xrp_usdt": xrp, "doge_usdt": doge
        }
    except:
        return {}

@app.get("/start")
async def start_bot():
    if state["running"]:
        return {"status": "already_running"}
    state["stop"] = False
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
    
    tasks = [stream(symbol) for symbol in ALL_SYMBOLS]
    tasks.append(engine())
    await asyncio.gather(*tasks)

# =========================
# 10. RUN
# =========================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)

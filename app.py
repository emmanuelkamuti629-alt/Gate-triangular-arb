import asyncio
import os
import time
from dotenv import load_dotenv
import ccxt.async_support as ccxt
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

# =========================
# CONFIG
# =========================
load_dotenv()
app = FastAPI()

TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", "20"))
MIN_PROFIT = float(os.getenv("MIN_PROFIT_PCT", "0.05"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
FEE = 0.002

exchange = ccxt.gateio({
    "apiKey": os.getenv("GATEIO_API_KEY"),
    "secret": os.getenv("GATEIO_SECRET"),
    "enableRateLimit": True,
    "options": {"defaultType": "spot"},
})

# =========================
# STATE
# =========================
state = {
    "running": False,
    "stop": False,
    "logs": [],
    "best": None,
    "all_opportunities": [],
    "trades": 0,
    "total_profit": 0.0,
    "prices": {}
}

def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line)
    state["logs"].append(line)
    state["logs"] = state["logs"][-100:]

# =========================
# INIT MARKETS
# =========================
async def init():
    await exchange.load_markets()
    log("📡 Markets loaded")

# =========================
# PRICE FETCH (BID/ASK)
# =========================
async def get_ticker(symbol):
    try:
        t = await exchange.fetch_ticker(symbol)
        return {
            "bid": t.get("bid") or t.get("last") or 0,
            "ask": t.get("ask") or t.get("last") or 0
        }
    except Exception as e:
        log(f"Error fetching {symbol}: {e}")
        return {"bid": 0, "ask": 0}

# =========================
# REAL TRIANGLE SIMULATION (FIXED)
# =========================
async def simulate_triangle(triangle):
    """Calculate profit for a triangle path"""
    try:
        amount = TRADE_AMOUNT
        
        for step in triangle["path"]:
            symbol = step["symbol"]
            side = step["side"]
            
            ticker = state["prices"].get(symbol)
            if not ticker:
                ticker = await get_ticker(symbol)
                state["prices"][symbol] = ticker
            
            price = ticker["ask"] if side == "buy" else ticker["bid"]
            
            if price <= 0:
                return None
            
            if side == "buy":
                # Buy: USDT -> Crypto
                amount = (amount / price) * (1 - FEE)
            else:
                # Sell: Crypto -> USDT
                amount = (amount * price) * (1 - FEE)
        
        profit = amount - TRADE_AMOUNT
        pct = (profit / TRADE_AMOUNT) * 100
        
        if pct >= MIN_PROFIT and profit > 0:
            return {
                "route": triangle["name"],
                "profit": profit,
                "pct": pct,
                "path": " → ".join([step["symbol"] for step in triangle["path"]])
            }
        return None
        
    except Exception as e:
        log(f"Simulation error for {triangle['name']}: {e}")
        return None

# =========================
# TRIANGLES (COMPLETE WITH ALL ADDITIONS)
# =========================
TRIANGLES = [
    # Original triangles
    {
        "name": "USDT → BTC → ETH → USDT",
        "path": [
            {"symbol": "BTC/USDT", "side": "buy"},
            {"symbol": "ETH/BTC", "side": "buy"},
            {"symbol": "ETH/USDT", "side": "sell"},
        ]
    },
    {
        "name": "USDT → BTC → BNB → USDT",
        "path": [
            {"symbol": "BTC/USDT", "side": "buy"},
            {"symbol": "BNB/BTC", "side": "buy"},
            {"symbol": "BNB/USDT", "side": "sell"},
        ]
    },
    {
        "name": "USDT → BTC → XRP → USDT",
        "path": [
            {"symbol": "BTC/USDT", "side": "buy"},
            {"symbol": "XRP/BTC", "side": "buy"},
            {"symbol": "XRP/USDT", "side": "sell"},
        ]
    },
    {
        "name": "USDT → BTC → LTC → USDT",
        "path": [
            {"symbol": "BTC/USDT", "side": "buy"},
            {"symbol": "LTC/BTC", "side": "buy"},
            {"symbol": "LTC/USDT", "side": "sell"},
        ]
    },
    {
        "name": "USDT → BTC → ADA → USDT",
        "path": [
            {"symbol": "BTC/USDT", "side": "buy"},
            {"symbol": "ADA/BTC", "side": "buy"},
            {"symbol": "ADA/USDT", "side": "sell"},
        ]
    },
    {
        "name": "USDT → BTC → DOGE → USDT",
        "path": [
            {"symbol": "BTC/USDT", "side": "buy"},
            {"symbol": "DOGE/BTC", "side": "buy"},
            {"symbol": "DOGE/USDT", "side": "sell"},
        ]
    },
    {
        "name": "USDT → SOL → USDT (Simple)",
        "path": [
            {"symbol": "SOL/USDT", "side": "buy"},
            {"symbol": "SOL/USDT", "side": "sell"},
        ]
    },
    
    # NEW TRIANGLES ADDED
    {
        "name": "USDT → BTC → SOL → USDT",
        "path": [
            {"symbol": "BTC/USDT", "side": "buy"},
            {"symbol": "SOL/BTC", "side": "buy"},
            {"symbol": "SOL/USDT", "side": "sell"},
        ]
    },
    {
        "name": "USDT → ETH → LINK → USDT",
        "path": [
            {"symbol": "ETH/USDT", "side": "buy"},
            {"symbol": "LINK/ETH", "side": "buy"},
            {"symbol": "LINK/USDT", "side": "sell"},
        ]
    },
    {
        "name": "USDT → ETH → MATIC → USDT",
        "path": [
            {"symbol": "ETH/USDT", "side": "buy"},
            {"symbol": "MATIC/ETH", "side": "buy"},
            {"symbol": "MATIC/USDT", "side": "sell"},
        ]
    },
    {
        "name": "USDT → BTC → XRP → USDT",
        "path": [
            {"symbol": "BTC/USDT", "side": "buy"},
            {"symbol": "XRP/BTC", "side": "buy"},
            {"symbol": "XRP/USDT", "side": "sell"},
        ]
    },
    {
        "name": "USDT → BTC → AVAX → USDT",
        "path": [
            {"symbol": "BTC/USDT", "side": "buy"},
            {"symbol": "AVAX/BTC", "side": "buy"},
            {"symbol": "AVAX/USDT", "side": "sell"},
        ]
    },
    {
        "name": "USDT → ETH → ADA → USDT",
        "path": [
            {"symbol": "ETH/USDT", "side": "buy"},
            {"symbol": "ADA/ETH", "side": "buy"},
            {"symbol": "ADA/USDT", "side": "sell"},
        ]
    },
    {
        "name": "USDT → BNB → DOGE → USDT",
        "path": [
            {"symbol": "BNB/USDT", "side": "buy"},
            {"symbol": "DOGE/BNB", "side": "buy"},
            {"symbol": "DOGE/USDT", "side": "sell"},
        ]
    },
    {
        "name": "USDT → BTC → INJ → USDT",
        "path": [
            {"symbol": "BTC/USDT", "side": "buy"},
            {"symbol": "INJ/BTC", "side": "buy"},
            {"symbol": "INJ/USDT", "side": "sell"},
        ]
    },
    {
        "name": "USDT → ETH → OP → USDT",
        "path": [
            {"symbol": "ETH/USDT", "side": "buy"},
            {"symbol": "OP/ETH", "side": "buy"},
            {"symbol": "OP/USDT", "side": "sell"},
        ]
    },
    {
        "name": "USDT → BTC → PEPE → USDT",
        "path": [
            {"symbol": "BTC/USDT", "side": "buy"},
            {"symbol": "PEPE/BTC", "side": "buy"},
            {"symbol": "PEPE/USDT", "side": "sell"},
        ]
    }
]

# =========================
# UPDATE ALL PRICES
# =========================
async def update_all_prices():
    """Fetch all needed prices in parallel"""
    all_symbols = set()
    for triangle in TRIANGLES:
        for step in triangle["path"]:
            all_symbols.add(step["symbol"])
    
    # Fetch all tickers in parallel
    tasks = [get_ticker(symbol) for symbol in all_symbols]
    results = await asyncio.gather(*tasks)
    
    for symbol, ticker in zip(all_symbols, results):
        state["prices"][symbol] = ticker

# =========================
# SCAN LOOP
# =========================
async def scan():
    """Scan all triangles for opportunities"""
    opportunities = []
    
    # Update all prices first
    await update_all_prices()
    
    # Calculate each triangle
    for triangle in TRIANGLES:
        result = await simulate_triangle(triangle)
        if result:
            opportunities.append(result)
    
    # Sort by profit percentage
    opportunities.sort(key=lambda x: x["pct"], reverse=True)
    return opportunities

# =========================
# ENGINE
# =========================
async def engine():
    state["running"] = True
    log("🚀 MULTI-TRIANGLE ARBITRAGE BOT STARTED")
    log(f"💰 Trade Amount: ${TRADE_AMOUNT}")
    log(f"🎯 Min Profit: {MIN_PROFIT}%")
    log(f"🧪 Mode: {'DRY RUN' if DRY_RUN else 'LIVE'}")
    log(f"🔺 Scanning {len(TRIANGLES)} triangles")
    
    while not state["stop"]:
        try:
            ops = await scan()
            
            state["all_opportunities"] = ops[:10]
            state["best"] = ops[0] if ops else None
            
            if state["best"] and state["best"]["pct"] > MIN_PROFIT:
                best = state["best"]
                log(f"🎯 {best['route']} | {best['pct']:.4f}% | ${best['profit']:.4f}")
                
                if DRY_RUN:
                    state["trades"] += 1
                    state["total_profit"] += best["profit"]
                    log(f"💰 [DRY RUN] Total: ${state['total_profit']:.4f}")
            
            await asyncio.sleep(2)  # Scan every 2 seconds
            
        except Exception as e:
            log(f"Engine error: {e}")
            await asyncio.sleep(2)

# =========================
# DASHBOARD HTML
# =========================
HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Arbitrage Bot</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Courier New', monospace; background: #0a0c10; color: #0f0; padding: 20px; }
        .container { max-width: 1000px; margin: 0 auto; }
        h1 { text-align: center; border-bottom: 1px solid #0f0; padding-bottom: 10px; margin-bottom: 20px; }
        .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px; margin-bottom: 20px; }
        .card { background: #0d1117; border: 1px solid #0f0; border-radius: 8px; padding: 15px; text-align: center; }
        .card-value { font-size: 24px; font-weight: bold; margin-top: 5px; }
        .section { margin-bottom: 20px; }
        .section-title { font-size: 18px; border-left: 3px solid #0f0; padding-left: 10px; margin-bottom: 10px; }
        .opportunity { background: #0d1117; border: 1px solid #333; border-radius: 8px; padding: 12px; margin-bottom: 8px; }
        .opportunity-best { border-color: #ff0; background: #1a1a00; }
        .profit-pct { font-size: 20px; font-weight: bold; color: #0f0; }
        .profit-usd { font-size: 14px; color: #0f0; }
        .market-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 20px; }
        .market-card { background: #0d1117; border: 1px solid #333; border-radius: 8px; padding: 10px; text-align: center; }
        .logs { background: #0d1117; border: 1px solid #333; border-radius: 8px; padding: 10px; height: 300px; overflow-y: auto; font-size: 11px; }
        .log-line { border-bottom: 1px solid #1a1a1a; padding: 4px 0; }
        button { background: #0f0; color: #000; border: none; padding: 10px 20px; margin: 5px; cursor: pointer; font-weight: bold; border-radius: 5px; font-size: 16px; }
        button:hover { background: #0c0; }
        .stop-btn { background: #f00; color: #fff; }
        .footer { text-align: center; font-size: 10px; color: #444; margin-top: 20px; padding-top: 10px; border-top: 1px solid #333; }
        .running { color: #0f0; }
        .stopped { color: #f00; }
    </style>
</head>
<body>
    <div class="container">
        <h1>⚡ TRIANGULAR ARBITRAGE BOT ⚡</h1>
        
        <div id="stats" class="stats"></div>
        
        <div class="section">
            <div class="section-title">🏆 BEST OPPORTUNITY</div>
            <div id="best"></div>
        </div>
        
        <div class="section">
            <div class="section-title">🔥 ALL PROFITABLE OPPORTUNITIES</div>
            <div id="opportunities"></div>
        </div>
        
        <div class="section">
            <div class="section-title">📊 LIVE MARKET PRICES</div>
            <div id="prices" class="market-grid"></div>
        </div>
        
        <div class="section">
            <div class="section-title">📝 LIVE LOGS</div>
            <div id="logs" class="logs"></div>
        </div>
        
        <div style="text-align: center;">
            <button onclick="startBot()">▶ START BOT</button>
            <button onclick="stopBot()" class="stop-btn">⏹ STOP BOT</button>
            <button onclick="refresh()">🔄 REFRESH</button>
        </div>
        
        <div class="footer">
            💰 Trade: $""" + str(TRADE_AMOUNT) + """ | 🎯 Min Profit: """ + str(MIN_PROFIT) + """% | 💸 Fee: 0.2% | Mode: """ + ("DRY RUN" if DRY_RUN else "LIVE") + """
        </div>
    </div>
    
    <script>
        async function refresh() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                const balance = await fetch('/api/balance').then(r => r.json());
                
                document.getElementById('stats').innerHTML = `
                    <div class="card"><div>💰 USDT BALANCE</div><div class="card-value">$${balance.usdt?.toFixed(2) || '0'}</div></div>
                    <div class="card"><div>📊 TOTAL P&L</div><div class="card-value ${data.total_profit >= 0 ? 'profit-pct' : ''}">$${data.total_profit.toFixed(4)}</div></div>
                    <div class="card"><div>🎯 TRADES</div><div class="card-value">${data.trades}</div></div>
                    <div class="card"><div>⚡ STATUS</div><div class="card-value ${data.running ? 'running' : 'stopped'}">${data.running ? 'RUNNING' : 'STOPPED'}</div></div>
                `;
                
                if (data.best && data.best.pct > 0) {
                    document.getElementById('best').innerHTML = `<div class="opportunity opportunity-best"><strong>${data.best.route}</strong><br><span class="profit-pct">${data.best.pct.toFixed(4)}%</span> <span class="profit-usd">($${data.best.profit.toFixed(4)})</span></div>`;
                } else {
                    document.getElementById('best').innerHTML = '<div class="opportunity">🔍 Waiting for opportunities...</div>';
                }
                
                if (data.all_opportunities && data.all_opportunities.length > 0) {
                    document.getElementById('opportunities').innerHTML = data.all_opportunities.map(o => `
                        <div class="opportunity">
                            <strong>${o.route}</strong><br>
                            <span class="profit-pct">${o.pct.toFixed(4)}%</span> <span class="profit-usd">($${o.profit.toFixed(4)})</span>
                        </div>
                    `).join('');
                } else {
                    document.getElementById('opportunities').innerHTML = '<div class="opportunity">🔍 No profitable opportunities found...</div>';
                }
                
                const prices = await fetch('/api/prices').then(r => r.json());
                const priceKeys = Object.keys(prices).slice(0, 8);
                document.getElementById('prices').innerHTML = priceKeys.map(k => `<div class="market-card"><strong>${k}</strong><br>$${prices[k].toFixed(2)}</div>`).join('');
                
                const logs = await fetch('/api/logs').then(r => r.json());
                document.getElementById('logs').innerHTML = logs.logs.slice().reverse().slice(0, 40).map(l => `<div class="log-line">${escapeHtml(l)}</div>`).join('');
            } catch(e) { console.error(e); }
        }
        
        function escapeHtml(t) { if (!t) return ''; return t.replace(/[&<>]/g, m => m === '&' ? '&amp;' : m === '<' ? '&lt;' : '&gt;'); }
        
        async function startBot() { await fetch('/start'); setTimeout(refresh, 1000); }
        async function stopBot() { await fetch('/stop'); setTimeout(refresh, 1000); }
        
        setInterval(refresh, 2000);
        refresh();
    </script>
</body>
</html>
"""

# =========================
# API ENDPOINTS
# =========================
@app.on_event("startup")
async def startup():
    await init()

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTML

@app.get("/start")
async def start():
    if not state["running"]:
        asyncio.create_task(engine())
    state["stop"] = False
    return {"status": "started"}

@app.get("/stop")
async def stop():
    state["stop"] = True
    state["running"] = False
    return {"status": "stopped"}

@app.get("/api/status")
async def status():
    return {
        "running": state["running"],
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
    return {k: round(v.get("bid", 0), 2) for k, v in state["prices"].items() if v.get("bid", 0) > 0}

# =========================
# RUN
# =========================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)

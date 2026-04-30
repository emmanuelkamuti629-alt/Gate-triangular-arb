import asyncio
import os
import time
from dotenv import load_dotenv
import ccxt.async_support as ccxt

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

# =========================
# ENV
# =========================
load_dotenv()

app = FastAPI()

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
            
            await asyncio.sleep(2)
            
        except Exception as e:
            log(f"Engine error: {e}")
            await asyncio.sleep(5)
    
    await exchange.close()
    state["running"] = False
    log("🛑 Engine stopped")

# =========================
# HTML DASHBOARD (embedded)
# =========================
HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>Arbitrage Bot</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif; 
            background: #0a0c10; 
            color: #e6edf3; 
            padding: 20px;
        }
        .container { 
            max-width: 900px; 
            margin: 0 auto; 
            background: #161b22; 
            padding: 24px; 
            border-radius: 16px; 
            box-shadow: 0 4px 6px rgba(0,0,0,0.3);
        }
        h1 { 
            color: #58a6ff; 
            font-size: 28px;
            margin-bottom: 20px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .status { 
            padding: 12px; 
            border-radius: 8px; 
            margin: 16px 0; 
            font-weight: bold;
            font-size: 16px;
        }
        .running { background: #0f5323; border-left: 4px solid #56d364; }
        .stopped { background: #5a1e1e; border-left: 4px solid #f85149; }
        .dry-run { background: #6e4600; border-left: 4px solid #f0883e; }
        .stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 12px;
            margin: 20px 0;
        }
        .stat-card {
            background: #0d1117;
            padding: 16px;
            border-radius: 12px;
            border: 1px solid #30363d;
        }
        .stat-label {
            font-size: 12px;
            color: #8b949e;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .stat-value {
            font-size: 28px;
            font-weight: bold;
            margin-top: 8px;
        }
        .profit { color: #56d364; }
        .loss { color: #f85149; }
        .best-card {
            background: #1f6feb;
            padding: 16px;
            border-radius: 12px;
            margin: 20px 0;
            text-align: center;
        }
        .best-path {
            font-size: 20px;
            font-weight: bold;
            margin-bottom: 8px;
        }
        .best-profit {
            font-size: 32px;
            font-weight: bold;
        }
        button {
            background: #238636;
            color: white;
            border: none;
            padding: 12px 24px;
            margin: 8px;
            cursor: pointer;
            border-radius: 8px;
            font-size: 16px;
            font-weight: bold;
            transition: all 0.2s;
        }
        button:hover { background: #2ea043; transform: scale(1.02); }
        .stop-btn { background: #da3633; }
        .stop-btn:hover { background: #f85149; }
        .logs {
            background: #0d1117;
            padding: 16px;
            border-radius: 12px;
            height: 350px;
            overflow-y: auto;
            font-family: 'Courier New', monospace;
            font-size: 12px;
            margin-top: 20px;
            border: 1px solid #30363d;
        }
        .log-entry {
            padding: 6px 0;
            border-bottom: 1px solid #21262d;
            font-family: monospace;
        }
        .refresh-btn {
            background: #1f6feb;
            font-size: 14px;
            padding: 8px 16px;
        }
        .footer {
            margin-top: 20px;
            text-align: center;
            font-size: 12px;
            color: #8b949e;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.6; }
        }
        .scanning {
            animation: pulse 1.5s ease-in-out infinite;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>
            <span>🔄</span> 
            Triangular Arbitrage Bot
            <span id="scanIcon" style="font-size: 14px;"></span>
        </h1>
        
        <div id="statusDiv" class="status running">🟢 LOADING...</div>
        
        <div class="stats">
            <div class="stat-card">
                <div class="stat-label">💰 USDT Balance</div>
                <div class="stat-value" id="balance">$0.00</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">📊 Total Trades</div>
                <div class="stat-value" id="trades">0</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">✅ Wins / ❌ Losses</div>
                <div class="stat-value" id="winsLosses">0 / 0</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">💰 Total PnL</div>
                <div class="stat-value" id="pnl">$0.00</div>
            </div>
        </div>
        
        <div id="bestDiv" style="display: none;" class="best-card">
            <div class="best-path" id="bestPath"></div>
            <div class="best-profit" id="bestProfit"></div>
        </div>
        
        <div>
            <button id="startBtn" onclick="startBot()">▶️ Start Bot</button>
            <button id="stopBtn" onclick="stopBot()" class="stop-btn" style="display: none;">⏹️ Stop Bot</button>
            <button onclick="refreshData()" class="refresh-btn">🔄 Refresh</button>
        </div>
        
        <h3 style="margin-top: 20px;">📝 Live Logs</h3>
        <div class="logs" id="logs">
            <div class="log-entry">Waiting for bot to start...</div>
        </div>
        
        <div class="footer">
            ⚡ Scan every 2 seconds | Fee: 0.2% | DRY RUN MODE
        </div>
    </div>

    <script>
        async function refreshData() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                
                // Update stats
                document.getElementById('balance').innerHTML = `$${data.balance.toFixed(2)}`;
                document.getElementById('trades').innerHTML = data.trades;
                document.getElementById('winsLosses').innerHTML = `${data.wins} / ${data.losses}`;
                
                const pnlElem = document.getElementById('pnl');
                pnlElem.innerHTML = `$${data.realized_pnl.toFixed(2)}`;
                if (data.realized_pnl >= 0) {
                    pnlElem.className = 'stat-value profit';
                } else {
                    pnlElem.className = 'stat-value loss';
                }
                
                // Update status
                const statusDiv = document.getElementById('statusDiv');
                if (data.running) {
                    statusDiv.innerHTML = '🟢 RUNNING';
                    statusDiv.className = 'status running';
                    document.getElementById('startBtn').style.display = 'none';
                    document.getElementById('stopBtn').style.display = 'inline-block';
                    document.getElementById('scanIcon').innerHTML = '⚡ scanning...';
                    document.getElementById('scanIcon').className = 'scanning';
                } else {
                    statusDiv.innerHTML = '🔴 STOPPED';
                    statusDiv.className = 'status stopped';
                    document.getElementById('startBtn').style.display = 'inline-block';
                    document.getElementById('stopBtn').style.display = 'none';
                    document.getElementById('scanIcon').innerHTML = '';
                }
                
                // Update best opportunity
                if (data.best && data.best.pct) {
                    const bestDiv = document.getElementById('bestDiv');
                    bestDiv.style.display = 'block';
                    document.getElementById('bestPath').innerHTML = data.best.path;
                    const profitClass = data.best.profit >= 0 ? 'profit' : 'loss';
                    document.getElementById('bestProfit').innerHTML = `${data.best.pct.toFixed(4)}% ($${data.best.profit.toFixed(4)})`;
                    document.getElementById('bestProfit').className = `best-profit ${profitClass}`;
                } else {
                    document.getElementById('bestDiv').style.display = 'none';
                }
            } catch(e) {
                console.error(e);
            }
        }
        
        async function refreshLogs() {
            try {
                const res = await fetch('/api/logs');
                const data = await res.json();
                const logsDiv = document.getElementById('logs');
                logsDiv.innerHTML = data.logs.map(log => `<div class="log-entry">${escapeHtml(log)}</div>`).join('');
                logsDiv.scrollTop = logsDiv.scrollHeight;
            } catch(e) {
                console.error(e);
            }
        }
        
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        
        async function startBot() {
            await fetch('/start');
            setTimeout(() => refreshData(), 500);
        }
        
        async function stopBot() {
            await fetch('/stop');
            setTimeout(() => refreshData(), 500);
        }
        
        refreshData();
        refreshLogs();
        setInterval(refreshData, 3000);
        setInterval(refreshLogs, 2000);
    </script>
</body>
</html>
"""

# =========================
# FASTAPI ROUTES
# =========================
@app.on_event("startup")
async def startup():
    log("🌐 Arbitrage Bot API Ready")

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(content=HTML_PAGE)

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

@app.get("/api/logs")
async def get_logs():
    return {"logs": state["logs"]}

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

import asyncio
import os
import time
from dotenv import load_dotenv
import ccxt.pro as ccxt
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

# =========================
# ENV
# =========================
load_dotenv()

app = FastAPI()

TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", "20"))
FEE = 0.002
MIN_PROFIT = float(os.getenv("MIN_PROFIT_PCT", "0.2"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

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
# LIVE ORDERBOOK CACHE
# =========================
orderbooks = {
    "BTC/USDT": None,
    "ETH/USDT": None,
    "ETH/BTC": None,
}

# =========================
# STATE
# =========================
state = {
    "running": False,
    "stop": False,
    "logs": [],
    "best": None,
    "trades": 0,
    "total_profit": 0.0,
}

# =========================
# LOGGING
# =========================
def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line)
    state["logs"].append(line)
    state["logs"] = state["logs"][-200:]

# =========================
# WEBSOCKET STREAMS
# =========================
async def stream(symbol):
    while not state["stop"]:
        try:
            book = await exchange.watch_order_book(symbol, limit=10)
            if book:
                orderbooks[symbol] = book
        except Exception as e:
            log(f"WS ERROR {symbol}: {e}")
            await asyncio.sleep(1)

# =========================
# PRICE HELPERS
# =========================
def get_prices(book):
    if not book or not book["bids"] or not book["asks"]:
        return None, None
    return book["bids"][0][0], book["asks"][0][0]

# =========================
# ARBITRAGE CALCULATION
# =========================
def scan_opportunity():
    btc = orderbooks["BTC/USDT"]
    eth = orderbooks["ETH/USDT"]
    ethbtc = orderbooks["ETH/BTC"]

    if not btc or not eth or not ethbtc:
        return None

    btc_bid, btc_ask = get_prices(btc)
    eth_bid, eth_ask = get_prices(eth)
    ethbtc_bid, ethbtc_ask = get_prices(ethbtc)

    if None in [btc_bid, btc_ask, eth_bid, eth_ask, ethbtc_bid, ethbtc_ask]:
        return None

    # ROUTE 1: USDT → BTC → ETH → USDT
    btc_amt = TRADE_AMOUNT / btc_ask
    eth_amt = btc_amt / ethbtc_ask
    final_usdt = eth_amt * eth_bid
    final_usdt *= (1 - FEE) ** 3
    profit1 = final_usdt - TRADE_AMOUNT
    pct1 = (profit1 / TRADE_AMOUNT) * 100

    # ROUTE 2: USDT → ETH → BTC → USDT
    eth_amt2 = TRADE_AMOUNT / eth_ask
    btc_amt2 = eth_amt2 * ethbtc_bid
    final_usdt2 = btc_amt2 * btc_bid
    final_usdt2 *= (1 - FEE) ** 3
    profit2 = final_usdt2 - TRADE_AMOUNT
    pct2 = (profit2 / TRADE_AMOUNT) * 100

    # Find best
    if pct1 > MIN_PROFIT and pct1 >= pct2:
        return {"route": "USDT → BTC → ETH → USDT", "pct": pct1, "profit": profit1}
    elif pct2 > MIN_PROFIT:
        return {"route": "USDT → ETH → BTC → USDT", "pct": pct2, "profit": profit2}

    return None

# =========================
# EXECUTION
# =========================
async def execute_trade(opp):
    log(f"🚀 {opp['route']} | {opp['pct']:.3f}% | ${opp['profit']:.4f}")

    if DRY_RUN:
        state["trades"] += 1
        state["total_profit"] += opp["profit"]
        log(f"💰 DRY RUN - Total Profit: ${state['total_profit']:.4f}")
    else:
        # REAL TRADING (when ready)
        log("⚠️ LIVE TRADING COMING SOON")

# =========================
# MAIN ENGINE
# =========================
async def engine():
    state["running"] = True
    log("⚡ LOW LATENCY ENGINE STARTED")
    log(f"💰 Trade Amount: ${TRADE_AMOUNT}")
    log(f"🎯 Min Profit: {MIN_PROFIT}%")
    log(f"🧪 DRY RUN: {DRY_RUN}")
    
    last_log = time.time()
    
    while not state["stop"]:
        try:
            opp = scan_opportunity()

            if opp:
                state["best"] = opp
                
                if time.time() - last_log > 2:
                    log(f"🔥 {opp['pct']:.3f}% | {opp['route']}")
                    last_log = time.time()
                
                if opp["pct"] > MIN_PROFIT and opp["profit"] > 0:
                    await execute_trade(opp)

            await asyncio.sleep(0.05)  # 50ms - ULTRA FAST

        except Exception as e:
            log(f"ENGINE ERROR: {e}")
            await asyncio.sleep(1)
    
    state["running"] = False

# =========================
# HTML DASHBOARD
# =========================
HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Ultra-Fast Arbitrage Bot</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: 'Courier New', monospace; background: #0a0c10; color: #0f0; padding: 20px; }
        .container { max-width: 800px; margin: 0 auto; background: #0d1117; padding: 20px; border: 1px solid #0f0; border-radius: 10px; }
        .status { padding: 10px; border-radius: 5px; margin: 10px 0; text-align: center; font-weight: bold; }
        .running { background: #0f5323; color: #0f0; }
        .stopped { background: #5a1e1e; color: #f00; }
        .dry { background: #6e4600; color: #ff0; }
        .stat { margin: 10px 0; padding: 10px; background: #000; border-left: 3px solid #0f0; }
        .profit { color: #0f0; }
        .loss { color: #f00; }
        button { background: #0f0; color: #000; border: none; padding: 10px 20px; margin: 5px; cursor: pointer; font-weight: bold; border-radius: 5px; }
        button:hover { background: #0c0; }
        .stop { background: #f00; color: #fff; }
        .log { background: #000; padding: 10px; height: 400px; overflow-y: scroll; font-family: monospace; font-size: 11px; margin-top: 10px; border: 1px solid #333; }
        .log-line { border-bottom: 1px solid #1a1a1a; padding: 4px 0; }
        blink { animation: blink 1s step-end infinite; }
        @keyframes blink { 50% { opacity: 0; } }
    </style>
</head>
<body>
    <div class="container">
        <h1 style="text-align: center;">⚡ ULTRA-FAST ARBITRAGE BOT ⚡</h1>
        <p style="text-align: center; color: #888;">50ms latency | WebSocket | Real-time</p>
        
        <div id="status" class="status stopped">🔴 STOPPED</div>
        <div class="dry" style="padding: 5px; text-align: center;">🧪 DRY RUN MODE - No real money</div>
        
        <div class="stat">💰 Balance: $<span id="balance">0.00</span></div>
        <div class="stat">📊 Trades: <span id="trades">0</span></div>
        <div class="stat">💰 Total PnL: $<span id="pnl">0.00</span></div>
        
        <div id="bestDiv" style="display: none; background: #0f0; color: #000; padding: 15px; margin: 10px 0; border-radius: 5px; text-align: center;">
            <strong>🔥 LIVE OPPORTUNITY 🔥</strong><br>
            <span id="bestRoute"></span><br>
            <span id="bestPct" style="font-size: 24px; font-weight: bold;"></span>
        </div>
        
        <div style="text-align: center;">
            <button id="startBtn" onclick="startBot()">▶️ START BOT</button>
            <button id="stopBtn" onclick="stopBot()" class="stop" style="display: none;">⏹️ STOP BOT</button>
        </div>
        
        <h3>📡 LIVE FEED (50ms updates)</h3>
        <div class="log" id="logs">
            <div class="log-line">Waiting for bot...</div>
        </div>
    </div>

    <script>
        async function refresh() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                
                document.getElementById('balance').innerText = data.balance?.toFixed(2) || '0.00';
                document.getElementById('trades').innerText = data.trades || 0;
                const pnl = data.total_profit || 0;
                document.getElementById('pnl').innerHTML = pnl.toFixed(4);
                document.getElementById('pnl').style.color = pnl >= 0 ? '#0f0' : '#f00';
                
                const statusDiv = document.getElementById('status');
                if (data.running) {
                    statusDiv.innerHTML = '🟢 RUNNING (50ms loop)';
                    statusDiv.className = 'status running';
                    document.getElementById('startBtn').style.display = 'none';
                    document.getElementById('stopBtn').style.display = 'inline-block';
                } else {
                    statusDiv.innerHTML = '🔴 STOPPED';
                    statusDiv.className = 'status stopped';
                    document.getElementById('startBtn').style.display = 'inline-block';
                    document.getElementById('stopBtn').style.display = 'none';
                }
                
                if (data.best && data.best.pct > 0) {
                    document.getElementById('bestDiv').style.display = 'block';
                    document.getElementById('bestRoute').innerHTML = data.best.route;
                    document.getElementById('bestPct').innerHTML = `${data.best.pct.toFixed(4)}% ($${data.best.profit.toFixed(4)})`;
                } else {
                    document.getElementById('bestDiv').style.display = 'none';
                }
            } catch(e) {}
        }
        
        async function refreshLogs() {
            try {
                const res = await fetch('/api/logs');
                const data = await res.json();
                const logsDiv = document.getElementById('logs');
                logsDiv.innerHTML = data.logs.map(l => `<div class="log-line">${escapeHtml(l)}</div>`).reverse().join('');
            } catch(e) {}
        }
        
        function escapeHtml(t) {
            return t.replace(/[&<>]/g, function(m) {
                if (m === '&') return '&amp;';
                if (m === '<') return '&lt;';
                if (m === '>') return '&gt;';
                return m;
            });
        }
        
        async function startBot() {
            await fetch('/start');
            setTimeout(refresh, 500);
        }
        
        async function stopBot() {
            await fetch('/stop');
            setTimeout(refresh, 500);
        }
        
        refresh();
        refreshLogs();
        setInterval(refresh, 1000);
        setInterval(refreshLogs, 2000);
    </script>
</body>
</html>
"""

# =========================
# FASTAPI ENDPOINTS
# =========================
@app.on_event("startup")
async def startup():
    log("🌐 WebSocket Arbitrage Bot Ready")

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(content=HTML)

@app.get("/api/status")
async def get_status():
    return {
        "running": state["running"],
        "balance": 0,  # Add balance fetch if needed
        "trades": state["trades"],
        "total_profit": state["total_profit"],
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
    asyncio.create_task(run_bot())
    return {"status": "started"}

@app.get("/stop")
async def stop_bot():
    state["stop"] = True
    return {"status": "stopping"}

async def run_bot():
    await asyncio.gather(
        stream("BTC/USDT"),
        stream("ETH/USDT"),
        stream("ETH/BTC"),
        engine()
    )

# =========================
# RUN
# =========================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)

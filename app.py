import os
import asyncio
import logging
import threading
import time
from datetime import datetime
from collections import defaultdict
from flask import Flask, render_template_string, jsonify
import ccxt.async_support as ccxt
from aiolimiter import AsyncLimiter
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# === CONFIG ===
EXCHANGE_IDS = ['mexc', 'kucoin', 'gate', 'bitget', 'coinex']
MIN_SPREAD = 0.4
MIN_TRADE_USD = 50
MAX_TRADE_USD = 2000
TRADE_SIZES = [50, 100, 500, 1000, 2000]
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
MAX_WS_TOTAL = 500
ORDERBOOK_DEPTH = 20
STALE_BOOK_MS = 3000
OPP_EXPIRE_MS = 60000

# === YOUR 280 COINS ===
COINS = [
'BTC','ETH','BNB','SOL','TON','SUI','XLM','XMR','DOGE','AVAX','TRX','ADA','MATIC','POL','LINK','DOT','XRP','ICP','HBAR','SHIB',
'LTC','ETC','BCH','NEAR','APT','ARB','OP','INJ','RUNE','FET','GRT','UNI','AAVE','MKR','SNX','CRV','SUSHI','1INCH','COMP','YFI','BAL','GMX','WOO',
'IMX','AR','EGLD','FLUX','ALGO','KCS','ZEC','STX','VET','FIL','RNDR','ROSE','STG','RLB','RDNT','JUP','PYR','JTO','WLD','ZRO','TIA','SEI',
'AGIX','AKT','MINA','FLOW','ZIL','QTUM','ENJ','CHZ','BAT','MANA','SAND','GALA','AXS','ILV','YGG','DYDX','LDO','PENDLE','JOE','CELR','IOTX','CKB',
'XVS','XEM','DGB','RVN','SC','NKN','SYS','RLC','OMG','ZRX','BAND','OXT','CTSI','PERP','RIF','LRC','SKL','CVC','WAXP','BNT','REN','UMA','NMR','TRU',
'IDEX','HFT','BIGTIME','FLM','ALPHA','TLM','OGN','CHR','REEF','POLYX','DAR','LPT','MX','HOT','ANKR','UTK','API3','CELO','KDA','GLMR','MOVR','SXP','KAVA',
'OKB','GT','HT','LEO','KLAY','XTZ','ATOM','KSM','WAVES','IOTA','NEO','ONT','NANO','DASH','XEC','THETA','TFUEL','FTM','CFX','STRK','METIS',
'PEPE','BONK','WIF','POPCAT','MEW','BOME','MOG','NEIRO','BRETT','FLOKI','TURBO','WEN','SLERF','PNUT','TRUMP','MELANIA','BABYDOGE','DOGWIFHAT','TOSHI','PURR','NYAN','CAT','MASK',
'STMX','BICO','ACH','LINA','HARD','IRIS','LTO','MDX','NULS','PHA','QNT','KNC','DODO','BEL','PERL','DATA','SFP','LIT','TORN','ARPA','GTC',
'CTK','COS','DUSK','FIS','FORTH','FXS','MAGIC','RAD','ALICE','SLP','GMT','GST','VOXEL','XETA','HOOK','ID','ASTR','RPL','SD','LQTY','POND',
'PEOPLE','WOJAK','MILADY','DOGS','CATE','ELON','SHIB2','PEPE2','MOON','STAR','AIDOGE','BULL','BEAR','FLOKICEO','CHEEMS','KISHU','HOGE','AKITA','SAFEMOON'
]

PAIRS = list(set([f"{coin}/USDT" for coin in COINS if coin]))
logger.info(f"Loaded {len(PAIRS)} unique USDT pairs")

FEES = {
    'mexc': {'taker': 0.002, 'maker': 0.0},
    'kucoin': {'taker': 0.001, 'maker': 0.001},
    'coinex': {'taker': 0.002, 'maker': 0.002},
    'gate': {'taker': 0.0015, 'maker': 0.0015},
    'bitget': {'taker': 0.001, 'maker': 0.001}
}

class ArbitrageState:
    def __init__(self):
        self.exchanges = {}
        self.orderbooks = defaultdict(lambda: defaultdict(dict))
        self.opportunities = []
        self.last_update = None
        self.active_pairs = []
        self.rate_limiters = {
            'mexc': AsyncLimiter(18, 1), 'kucoin': AsyncLimiter(9, 1),
            'coinex': AsyncLimiter(9, 1), 'gate': AsyncLimiter(2, 1),
            'bitget': AsyncLimiter(9, 1),
        }
        self.alerted_opps = {}
        self.ws_tasks = {}
        self.pair_availability = defaultdict(set)
        self.currencies = {}

state = ArbitrageState()

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Crypto Arbitrage Scanner</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0a0a0a; color: #e5e5e5; padding: 16px; }
       .container { max-width: 800px; margin: 0 auto; }
       .header { text-align: center; padding: 20px 0 16px; }
       .header h1 { font-size: 1.5em; font-weight: 700; margin-bottom: 4px; background: linear-gradient(135deg, #3b82f6, #8b5cf6); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
       .header p { font-size: 0.85em; color: #737373; }
       .stats { display: flex; justify-content: space-between; gap: 8px; margin-bottom: 16px; }
       .stat { background: #171717; padding: 12px; border-radius: 8px; flex: 1; text-align: center; border: 1px solid #262626; }
       .stat-val { font-size: 1.3em; font-weight: 700; color: #3b82f6; }
       .stat-label { font-size: 0.7em; color: #737373; margin-top: 2px; }
       .opps { display: flex; flex-direction: column; gap: 10px; }
       .card { background: #171717; border: 1px solid #262626; border-radius: 12px; padding: 14px; display: flex; align-items: center; gap: 12px; transition: all 0.2s; }
       .card:hover { border-color: #3b82f6; transform: translateY(-1px); }
       .ex-box { background: #262626; padding: 8px 10px; border-radius: 8px; min-width: 90px; }
       .ex-line { font-size: 0.75em; font-weight: 700; line-height: 1.4; }
       .buy-text { color: #fbbf24; }
       .sell-text { color: #fbbf24; }
       .ex-name { color: #e5e5e5; margin-left: 4px; }
       .pair-info { flex: 1; }
       .pair-name { font-size: 1.1em; font-weight: 700; color: #fafafa; margin-bottom: 2px; }
       .liquidity { font-size: 0.8em; color: #a3a3a3; }
       .verified { font-size: 0.7em; color: #fbbf24; margin-top: 2px; }
       .spread-box { text-align: right; }
       .spread-val { font-size: 1.4em; font-weight: 700; color: #4ade80; }
       .empty { text-align: center; padding: 60px 20px; color: #525252; }
       .pulse { animation: pulse 2s infinite; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
        @media (max-width: 480px) {
           .card { padding: 12px; gap: 8px; }
           .ex-box { min-width: 80px; padding: 6px 8px; }
           .pair-name { font-size: 1em; }
           .spread-val { font-size: 1.2em; }
        }
    </style>
    <script>
        function timeAgo(ts) {
            const sec = Math.floor((Date.now() - ts) / 1000);
            if (sec < 60) return sec + ' seconds ago';
            return Math.floor(sec / 60) + ' minutes ago';
        }
        async function refresh() {
            try {
                const res = await fetch('/api/opportunities');
                const data = await res.json();
                document.getElementById('oppCount').textContent = data.opportunities.length;
                document.getElementById('bestSpread').textContent = data.best_spread || '0%';
                document.getElementById('pairsCount').textContent = data.pairs_monitored;
                const container = document.getElementById('opps');
                if (!data.opportunities.length) {
                    container.innerHTML = '<div class="empty pulse">Scanning ' + data.pairs_monitored + ' pairs across ' + data.exchanges + ' exchanges...</div>';
                    return;
                }
                container.innerHTML = data.opportunities.map(o => `
                    <div class="card">
                        <div class="ex-box">
                            <div class="ex-line"><span class="buy-text">BUY</span><span class="ex-name">${o.buy_ex.toUpperCase()}</span></div>
                            <div class="ex-line"><span class="sell-text">SELL</span><span class="ex-name">${o.sell_ex.toUpperCase()}</span></div>
                        </div>
                        <div class="pair-info">
                            <div class="pair-name">${o.pair}</div>
                            <div class="liquidity">Liquidity: $${o.liquidity.toLocaleString()}</div>
                            <div class="verified">Last Verified ${timeAgo(o.ts)}</div>
                        </div>
                        <div class="spread-box">
                            <div class="spread-val">${o.spread}%</div>
                        </div>
                    </div>
                `).join('');
            } catch(e) { console.error(e); }
        }
        setInterval(refresh, 1000);
        refresh();
    </script>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Arbitrage Scanner</h1>
            <p>Real-time spot opportunities across ${len(EXCHANGE_IDS)} exchanges</p>
        </div>
        <div class="stats">
            <div class="stat"><div class="stat-val" id="oppCount">0</div><div class="stat-label">Opportunities</div></div>
            <div class="stat"><div class="stat-val" id="bestSpread">0%</div><div class="stat-label">Best Spread</div></div>
            <div class="stat"><div class="stat-val" id="pairsCount">0</div><div class="stat-label">Pairs Live</div></div>
        </div>
        <div class="opps" id="opps">
            <div class="empty pulse">Initializing...</div>
        </div>
    </div>
</body>
</html>
"""

async def init_exchanges():
    for ex_id in EXCHANGE_IDS:
        try:
            ex_class = getattr(ccxtpro, ex_id)
            state.exchanges[ex_id] = ex_class({'enableRateLimit': True, 'options': {'defaultType': 'spot'}, 'timeout': 30000})
            await state.exchanges[ex_id].load_markets()
            logger.info(f"Loaded {ex_id}: {len(state.exchanges[ex_id].markets)} markets")
        except Exception as e:
            logger.error(f"Failed {ex_id}: {e}")

async def load_currencies():
    for ex_id, ex in state.exchanges.items():
        try:
            async with state.rate_limiters[ex_id]:
                state.currencies[ex_id] = await ex.fetch_currencies()
            logger.info(f"Loaded currency status for {ex_id}")
        except Exception as e:
            logger.warning(f"Could not load currencies for {ex_id}: {e}")

async def filter_active_pairs():
    for pair in PAIRS:
        for ex_id, ex in state.exchanges.items():
            if pair in ex.markets and ex.markets.get('active', True):
                state.pair_availability.add(ex_id)
    pair_scores = [(p, len(exs)) for p, exs in state.pair_availability.items()]
    pair_scores.sort(key=lambda x: x[1], reverse=True)
    state.active_pairs = [p[0] for p in pair_scores if p[1] >= 2]
    logger.info(f"Monitoring {len(state.active_pairs)} pairs on 2+ exchanges")
    return state.active_pairs

def calc_fill_price(book_side, amount_usd):
    remaining = amount_usd
    total_base = 0
    total_cost = 0
    for price, qty in book_side:
        level_usd = price * qty
        if remaining >= level_usd:
            total_base += qty
            total_cost += level_usd
            remaining -= level_usd
        else:
            partial_qty = remaining / price
            total_base += partial_qty
            total_cost += remaining
            remaining = 0
            break
    if total_base == 0:
        return 0, 0, True
    return total_cost / total_base, total_base, remaining > 1

def check_deposits_ok(exchange, coin):
    if exchange not in state.currencies:
        return True
    coin_data = state.currencies[exchange].get(coin)
    if not coin_data:
        return True
    return coin_data.get('active', True) and coin_data.get('withdraw', True) and coin_data.get('deposit', True)

async def check_arbitrage(pair):
    if len(state.orderbooks) < 2:
        return
    now = int(time.time() * 1000)
    base = pair.split('/')[0]
    for trade_usd in TRADE_SIZES:
        for buy_ex, buy_ob in state.orderbooks.items():
            if not check_deposits_ok(buy_ex, base):
                continue
            for sell_ex, sell_ob in state.orderbooks.items():
                if buy_ex == sell_ex or not check_deposits_ok(sell_ex, base):
                    continue
                if now - buy_ob['ts'] > STALE_BOOK_MS or now - sell_ob['ts'] > STALE_BOOK_MS:
                    continue
                buy_price, buy_qty, buy_partial = calc_fill_price(buy_ob['asks'], trade_usd)
                if buy_partial or buy_qty == 0:
                    continue
                sell_price, sell_qty, sell_partial = calc_fill_price(sell_ob['bids'], buy_qty * sell_price)
                if sell_partial or sell_qty < buy_qty * 0.97:
                    continue
                buy_fee = FEES[buy_ex]['taker']
                sell_fee = FEES[sell_ex]['taker']
                buy_cost = buy_qty * buy_price * (1 + buy_fee)
                sell_rev = buy_qty * sell_price * (1 - sell_fee)
                profit = sell_rev - buy_cost
                spread = profit / trade_usd * 100
                if spread >= MIN_SPREAD:
                    liquidity = min(buy_ob['asks'][0][0] * buy_ob['asks'][0][1],
                                   sell_ob['bids'][0][0] * sell_ob['bids'][0][1])
                    opp = {
                        'pair': pair, 'buy_ex': buy_ex, 'sell_ex': sell_ex,
                        'buy_price': f"{buy_price:.8f}".rstrip('0').rstrip('.'),
                        'sell_price': f"{sell_price:.8f}".rstrip('0').rstrip('.'),
                        'spread': round(spread, 1), 'profit': round(profit, 2),
                        'size_usd': trade_usd, 'liquidity': int(liquidity), 'ts': now
                    }
                    await handle_opportunity(opp)
                    return

async def handle_opportunity(opp):
    state.opportunities = [o for o in state.opportunities if time.time() * 1000 - o['ts'] < OPP_EXPIRE_MS]
    existing = next((o for o in state.opportunities if o['pair'] == opp['pair']), None)
    if existing and existing['spread'] >= opp['spread']:
        return
    if existing:
        state.opportunities.remove(existing)
    state.opportunities.append(opp)
    state.opportunities.sort(key=lambda x: x['spread'], reverse=True)
    state.opportunities = state.opportunities[:100]
    state.last_update = datetime.now()
    opp_key = f"{opp['pair']}_{opp['buy_ex']}_{opp['sell_ex']}"
    if TELEGRAM_TOKEN and time.time() - state.alerted_opps.get(opp_key, 0) > 600:
        state.alerted_opps[opp_key] = time.time()
        asyncio.create_task(send_telegram(opp))

async def send_telegram(opp):
    try:
        import aiohttp
        msg = (f"🚨 {opp['pair']} {opp['spread']}%\n"
               f"BUY {opp['buy_ex'].upper()} ${opp['buy_price']}\n"
               f"SELL {opp['sell_ex'].upper()} ${opp['sell_price']}\n"
               f"${opp['size_usd']} | Liq: ${opp['liquidity']} | +${opp['profit']}")
        async with aiohttp.ClientSession() as s:
            await s.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                        json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
    except: pass

async def watch_orderbook(exchange, pair):
    task_key = f"{exchange.id}_{pair}"
    while True:
        try:
            ob = await exchange.watch_order_book(pair, limit=ORDERBOOK_DEPTH)
            if ob['bids'] and ob['asks']:
                state.orderbooks[exchange.id] = {
                    'bids': ob['bids'][:ORDERBOOK_DEPTH],
                    'asks': ob['asks'][:ORDERBOOK_DEPTH],
                    'ts': exchange.milliseconds()
                }
                await check_arbitrage(pair)
        except Exception as e:
            logger.debug(f"WS {task_key}: {e}")
            await exchange.sleep(5000)

async def scanner_loop():
    await init_exchanges()
    await load_currencies()
    pairs = await filter_active_pairs()
    ws_count = 0
    for pair in pairs:
        for ex_id in state.pair_availability:
            if ws_count >= MAX_WS_TOTAL:
                logger.warning(f"Hit MAX_WS_TOTAL={MAX_WS_TOTAL}. Monitoring {ws_count} streams")
                break
            if ex_id not in state.exchanges:
                continue
            ex = state.exchanges[ex_id]
            task_key = f"{ex_id}_{pair}"
            state.ws_tasks[task_key] = asyncio.create_task(watch_orderbook(ex, pair))
            ws_count += 1
        if ws_count >= MAX_WS_TOTAL:
            break
    logger.info(f"Started {ws_count} WebSocket streams")
    await asyncio.gather(*state.ws_tasks.values(), return_exceptions=True)

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, PAIRS=PAIRS, MIN_TRADE_USD=MIN_TRADE_USD,
                                 MAX_TRADE_USD=MAX_TRADE_USD, MIN_SPREAD=MIN_SPREAD)

@app.route('/api/opportunities')
def api_opps():
    now = time.time() * 1000
    for o in state.opportunities:
        o['age'] = int((now - o['ts']) / 1000)
    return jsonify({
        'opportunities': state.opportunities,
        'best_spread': f"{state.opportunities[0]['spread']}%" if state.opportunities else "0%",
        'pairs_monitored': len(state.active_pairs),
        'ws_connections': len(state.ws_tasks),
        'exchanges': len(EXCHANGE_IDS),
        'last_update': state.last_update.strftime('%H:%M:%S') if state.last_update else None
    })

@app.route('/health')
def health():
    return {
        'status': 'ok', 'pairs_total': len(PAIRS), 'pairs_active': len(state.active_pairs),
        'opps': len(state.opportunities), 'ws': len(state.ws_tasks)
    }

def run_scanner():
    asyncio.run(scanner_loop())

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    threading.Thread(target=run_scanner, daemon=True).start()
    logger.info(f"🚀 Scanner: {len(PAIRS)} coins, {len(EXCHANGE_IDS)} exchanges")
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)

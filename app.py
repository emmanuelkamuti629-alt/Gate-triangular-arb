import asyncio
import os
from dotenv import load_dotenv
import ccxt.pro as ccxt

load_dotenv()

exchange = ccxt.gateio({
    "apiKey": os.getenv("GATEIO_API_KEY"),
    "secret": os.getenv("GATEIO_SECRET"),
    "enableRateLimit": True,
})

TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", 50))
MIN_PROFIT_PCT = 0.003 # Require 0.3% profit after fees to execute

async def wait_fill(order_id, symbol, timeout=15):
    start = asyncio.get_event_loop().time()
    last_filled = 0

    while asyncio.get_event_loop().time() - start < timeout:
        try:
            o = await exchange.fetch_order(order_id, symbol)
            filled = float(o.get("filled", 0))
            status = o.get("status")

            if status == "closed" and filled > 0:
                if abs(filled - last_filled) < 1e-10: # Stopped filling
                    return filled
                last_filled = filled

            if status in ["canceled", "rejected", "expired"]:
                return 0
        except Exception as e:
            print(f"wait_fill error: {e}")

        await asyncio.sleep(0.5)
    return last_filled # Return partial fill if timeout

async def check_profit():
    """Check if triangle is profitable using orderbook asks/bids"""
    btc_usdt = await exchange.fetch_order_book("BTC/USDT")
    eth_btc = await exchange.fetch_order_book("ETH/BTC")
    eth_usdt = await exchange.fetch_order_book("ETH/USDT")

    # Simulate: USDT -> BTC -> ETH -> USDT
    btc_ask = btc_usdt["asks"][0][0] # Buy BTC with USDT
    eth_btc_ask = eth_btc["asks"][0][0] # Buy ETH with BTC
    eth_usdt_bid = eth_usdt["bids"][0][0] # Sell ETH for USDT

    # Calculate without fees first
    btc_amt = TRADE_AMOUNT / btc_ask
    eth_amt = btc_amt / eth_btc_ask
    usdt_final = eth_amt * eth_usdt_bid

    # Gate.io taker fee ~0.2% per trade = 0.6% total
    fees = 0.006
    profit_pct = (usdt_final / TRADE_AMOUNT) - 1 - fees

    return profit_pct, btc_ask, eth_btc_ask, eth_usdt_bid

async def execute():
    await exchange.load_markets()

    try:
        print("🚀 CHECKING TRIANGLE")
        profit_pct, btc_ask, eth_btc_ask, eth_usdt_bid = await check_profit()
        print(f"Est. profit: {profit_pct*100:.3f}%")

        if profit_pct < MIN_PROFIT_PCT:
            return print(f"❌ Not profitable. Need >{MIN_PROFIT_PCT*100:.1f}%")

        print("🚀 START CYCLE")

        # STEP 1: USDT → BTC
        amount1 = exchange.amount_to_precision("BTC/USDT", TRADE_AMOUNT)
        o1 = await exchange.create_market_buy_order("BTC/USDT", amount1)
        btc = await wait_fill(o1["id"], "BTC/USDT")
        if btc <= 0: return print("❌ STEP 1 FAILED")
        print(f"Step 1: {btc:.8f} BTC")

        # STEP 2: BTC → ETH
        btc_trade = exchange.amount_to_precision("ETH/BTC", btc)
        o2 = await exchange.create_market_buy_order("ETH/BTC", btc_trade)
        eth = await wait_fill(o2["id"], "ETH/BTC")
        if eth <= 0: return print("❌ STEP 2 FAILED - STUCK WITH BTC")
        print(f"Step 2: {eth:.8f} ETH")

        # STEP 3: ETH → USDT
        eth_trade = exchange.amount_to_precision("ETH/USDT", eth)
        o3 = await exchange.create_market_sell_order("ETH/USDT", eth_trade)
        usdt = await wait_fill(o3["id"], "ETH/USDT")
        if usdt <= 0: return print("❌ STEP 3 FAILED - STUCK WITH ETH")

        pnl = usdt - TRADE_AMOUNT
        print("✅ CYCLE COMPLETE")
        print(f"Start: {TRADE_AMOUNT} USDT | End: {usdt:.4f} USDT")
        print(f"PnL: {pnl:.4f} USDT ({pnl/TRADE_AMOUNT*100:.3f}%)")

    except ccxt.InsufficientFunds as e:
        print("Insufficient funds:", e)
    except ccxt.InvalidOrder as e:
        print("Invalid order - check precision:", e)
    except Exception as e:
        print("EXEC ERROR:", e)
    finally:
        await exchange.close()

asyncio.run(execute())


# Triangular Arbitrage Bot for Gate.io

⚠️ **WARNING**: This bot trades real cryptocurrency. Use at your own risk!

## Setup
1. Add your Gate.io API keys in Render environment variables
2. Set DRY_RUN=true for testing first
3. Monitor the dashboard before enabling real trades

## Environment Variables (Set in Render)
- GATEIO_API_KEY: Your Gate.io API key
- GATEIO_SECRET: Your Gate.io secret  
- TRADE_AMOUNT: USDT amount per trade (default: 10)
- MIN_PROFIT_PCT: Minimum profit % to execute (default: 0.5)
- DRY_RUN: true = simulation, false = real trades

## Safety Features
- Dry run mode by default
- Minimum profit threshold
- Liquidity checks
- Market condition validation

## Dashboard
Access the web dashboard at your Render URL to monitor bot status.

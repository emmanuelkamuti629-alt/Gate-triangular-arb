import ccxt
import time
import itertools
from datetime import datetime

def fetch_gateio_markets():
    """
    Initialize Gate.io exchange and fetch all available markets
    """
    exchange = ccxt.gateio({
        'enableRateLimit': True,  # Respect rate limits
        'options': {
            'defaultType': 'spot',  # Only spot trading
        }
    })
    
    # Load all market data
    exchange.load_markets()
    
    return exchange

def get_usdt_pairs(exchange):
    """
    Get all USDT trading pairs from Gate.io
    """
    usdt_pairs = {}
    for symbol, market in exchange.markets.items():
        if market['active'] and symbol.endswith('/USDT'):
            try:
                ticker = exchange.fetch_ticker(symbol)
                usdt_pairs[symbol] = ticker['last']
            except Exception as e:
                print(f"Error fetching {symbol}: {e}")
    return usdt_pairs

def get_cross_pairs(exchange, symbols):
    """
    Fetch cross pairs between coins (e.g., ETH/BTC, BTC/ETH)
    Gate.io uses underscore format for API calls
    """
    cross_pairs = {}
    
    # Generate possible cross pair combinations
    for base, quote in itertools.permutations(symbols, 2):
        # Gate.io symbol format for cross pairs: e.g., "eth_btc"
        pair_id = f"{base.lower()}_{quote.lower()}"
        unified_symbol = f"{base}/{quote}"
        
        try:
            # Check if this market exists on Gate.io
            if unified_symbol in exchange.markets:
                ticker = exchange.fetch_ticker(unified_symbol)
                cross_pairs[unified_symbol] = ticker['last']
                print(f"✓ Found cross pair: {unified_symbol} = {ticker['last']}")
        except Exception as e:
            pass  # Pair doesn't exist
    
    return cross_pairs

def calculate_triangular_arbitrage(usdt_pairs, cross_pairs, fee_rate=0.002):
    """
    Calculate triangular arbitrage opportunities
    
    Path: USDT -> Coin A -> Coin B -> USDT
    Uses real cross pairs when available
    """
    symbols = [pair.split('/')[0] for pair in usdt_pairs.keys()]
    opportunities = []
    
    # Fee multiplier for 3 trades (0.1% each is optimistic, using 0.2% each)
    fee_multiplier = (1 - fee_rate) ** 3
    
    print(f"\n{'='*80}")
    print(f"Scanning triangular arbitrage on Gate.io")
    print(f"Available USDT pairs: {symbols}")
    print(f"Available cross pairs: {list(cross_pairs.keys())}")
    print(f"Fee multiplier (3 trades @ {fee_rate*100}% each): {fee_multiplier:.6f}")
    print(f"{'='*80}\n")
    
    for a, b in itertools.combinations(symbols, 2):
        # Check both path directions
        # Path 1: USDT -> A -> B -> USDT
        path1_result = evaluate_path(
            usdt_pairs, cross_pairs, a, b, fee_multiplier, "USDT"
        )
        if path1_result:
            opportunities.append(path1_result)
        
        # Path 2: USDT -> B -> A -> USDT  
        path2_result = evaluate_path(
            usdt_pairs, cross_pairs, b, a, fee_multiplier, "USDT"
        )
        if path2_result:
            opportunities.append(path2_result)
    
    return sorted(opportunities, key=lambda x: x['profit_pct'], reverse=True)

def evaluate_path(usdt_pairs, cross_pairs, first, second, fee_multiplier, start_currency):
    """
    Evaluate a specific path: Start -> First -> Second -> Start
    """
    pair1 = f"{first}/USDT"
    pair2 = f"{second}/USDT"
    cross_pair = f"{first}/{second}"
    cross_pair_reverse = f"{second}/{first}"
    
    # Check if all required pairs exist
    if pair1 not in usdt_pairs or pair2 not in usdt_pairs:
        return None
    
    # Check which cross pair is available
    cross_rate = None
    cross_available = False
    
    if cross_pair in cross_pairs:
        cross_rate = cross_pairs[cross_pair]
        cross_available = True
        cross_direction = f"{first}->{second}"
    elif cross_pair_reverse in cross_pairs:
        # Use reverse rate: if second/first is available, invert it
        cross_rate = 1 / cross_pairs[cross_pair_reverse]
        cross_available = True
        cross_direction = f"{first}->{second} (via reverse)"
    
    if not cross_available:
        return None
    
    # Start with 1 USDT
    start_amount = 1.0
    
    # Step 1: USDT -> First coin
    first_amount = start_amount / usdt_pairs[pair1]
    
    # Step 2: First coin -> Second coin (using cross rate)
    second_amount = first_amount * cross_rate
    
    # Step 3: Second coin -> USDT
    final_usdt = second_amount * usdt_pairs[pair2]
    
    # Apply fees
    final_usdt *= fee_multiplier
    
    profit_pct = (final_usdt - start_amount) * 100
    
    return {
        'path': f"{start_currency} -> {first} -> {second} -> {start_currency}",
        'cross_used': cross_direction,
        'cross_rate': cross_rate,
        'start_amount': start_amount,
        'final_amount': final_usdt,
        'profit_pct': profit_pct,
        'first_pair': pair1,
        'second_pair': pair2,
        'first_price': usdt_pairs[pair1],
        'second_price': usdt_pairs[pair2]
    }

def get_gateio_all_pairs():
    """
    Direct API call to Gate.io to list ALL trading pairs
    This shows what's actually available vs what's in your screenshot
    """
    import requests
    
    print("\n" + "="*80)
    print("Gate.io Official Trading Pairs (API 2.0)")
    print("="*80)
    
    try:
        # Gate.io API 2.0 - returns all trading pairs
        response = requests.get('https://data.gateapi.io/api2/1/pairs', timeout=10)
        if response.status_code == 200:
            pairs = response.json()
            print(f"\nTotal trading pairs on Gate.io: {len(pairs)}")
            print("\nSample pairs (first 30):")
            for pair in pairs[:30]:
                print(f"  - {pair}")
            
            # Check for cross pairs between your coins
            coins = ['btc', 'eth', 'bnb', 'link', 'avax', 'op']
            print("\n" + "-"*80)
            print("Cross pairs available for your coins:")
            print("-"*80)
            
            found = []
            for pair in pairs:
                pair_lower = pair.lower()
                for base in coins:
                    for quote in coins:
                        if base != quote:
                            if pair_lower == f"{base}_{quote}":
                                found.append(pair)
            
            if found:
                for f in found:
                    print(f"  ✓ {f}")
            else:
                print("  ✗ No direct cross pairs between BTC, ETH, BNB, LINK, AVAX, OP")
                print("\n  Gate.io primarily lists pairs with BTC or ETH as base/quote")
                print("  For triangular arbitrage, consider using:")
                print("    - BTC as intermediate (BTC/ETH, BTC/LINK, etc.)")
        else:
            print(f"API error: {response.status_code}")
    except Exception as e:
        print(f"Error fetching pairs: {e}")

def main():
    print("Gate.io Triangular Arbitrage Scanner")
    print("="*80)
    
    # First, check what pairs Gate.io actually offers
    get_gateio_all_pairs()
    
    print("\n" + "="*80)
    print("Starting real-time market scan...")
    print("="*80)
    
    exchange = fetch_gateio_markets()
    
    # Get USDT pairs
    usdt_pairs = get_usdt_pairs(exchange)
    print(f"\nFound {len(usdt_pairs)} active USDT pairs on Gate.io")
    
    # Extract symbols from USDT pairs
    symbols = list(set([pair.split('/')[0] for pair in usdt_pairs.keys()]))
    print(f"Coins with USDT pairs: {symbols[:20]}...")
    
    # Get cross pairs between these symbols
    cross_pairs = get_cross_pairs(exchange, symbols[:10])  # Limit to first 10 coins
    
    # Calculate opportunities
    opportunities = calculate_triangular_arbitrage(usdt_pairs, cross_pairs)
    
    # Display results
    print("\n" + "="*80)
    print("TOP TRIANGULAR ARBITRAGE OPPORTUNITIES")
    print("="*80)
    
    if opportunities:
        print(f"\n{'Path':<40} {'Cross Rate':<15} {'Profit %':<12}")
        print("-"*80)
        
        for opp in opportunities[:10]:
            if opp['profit_pct'] > 0:
                profit_symbol = "▲"
            else:
                profit_symbol = "▼"
            print(f"{opp['path']:<40} {opp['cross_rate']:<15.8f} {profit_symbol} {opp['profit_pct']:.6f}%")
    else:
        print("\n⚠️  NO EXECUTABLE TRIANGULAR ARBITRAGE PATHS FOUND")
        print("\nThis is expected because Gate.io lacks the necessary cross pairs.")
        print("\nTo make triangular arbitrage work on Gate.io:")
        print("  1. Use paths through BTC (BTC/USDT + BTC/ETH + ETH/USDT)")
        print("  2. Check if Gate.io has pairs like:")
        print("     - BTC/ETH, BTC/LINK, BTC/AVAX")
        print("     - ETH/BNB, ETH/LINK, ETH/OP")
    
    # Show what's needed
    print("\n" + "="*80)
    print("WHAT GATE.IO ACTUALLY OFFERS")
    print("="*80)
    print("""
Based on Gate.io API 2.0, trading pairs are formatted as: "eth_btc", "etc_btc", etc.
Gate.io's main cross pairs are with BTC as the quote currency.

To succeed with triangular arbitrage on Gate.io:
- Use: USDT -> ALT -> BTC -> USDT
- Or: USDT -> BTC -> ALT -> USDT  
- Ensure you have pairs like ALT/BTC or BTC/ALT

Your current coins would need these pairs:
  ✓ BTC/USDT, ETH/USDT, BNB/USDT, LINK/USDT, AVAX/USDT, OP/USDT
  ✗ BTC/ETH, BTC/LINK, BTC/AVAX (missing)
  ✗ ETH/BNB, ETH/LINK, ETH/OP (missing)
    """)

if __name__ == "__main__":
    main()

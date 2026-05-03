import ccxt
import time
import json
from datetime import datetime
from colorama import init, Fore, Style
import threading

init(autoreset=True)

class GateIOArbitrageBot:
    def __init__(self):
        self.exchange = ccxt.gateio({
            'enableRateLimit': True,
            'rateLimit': 1200,
            'options': {
                'defaultType': 'spot',
            }
        })
        
        self.arbitrage_threshold = 0.5  # Minimum profit % to show
        self.trade_amount_usdt = 100    # Amount to trade per arbitrage
        self.running = False
        
        # Cache for market data
        self.markets = None
        self.usdt_pairs = {}
        self.btc_pairs = {}
        self.eth_pairs = {}
        
    def load_markets(self):
        """Load all available markets from Gate.io"""
        print(f"{Fore.CYAN}📡 Loading markets from Gate.io...")
        self.exchange.load_markets()
        
        # Categorize pairs
        for symbol, market in self.exchange.markets.items():
            if not market['active']:
                continue
                
            if symbol.endswith('/USDT'):
                self.usdt_pairs[symbol] = market
            elif symbol.endswith('/BTC'):
                self.btc_pairs[symbol] = market
            elif symbol.endswith('/ETH'):
                self.eth_pairs[symbol] = market
        
        print(f"{Fore.GREEN}✓ Loaded: {len(self.usdt_pairs)} USDT pairs, {len(self.btc_pairs)} BTC pairs, {len(self.eth_pairs)} ETH pairs")
        
    def get_real_time_prices(self):
        """Fetch live prices for all relevant pairs"""
        try:
            all_tickers = self.exchange.fetch_tickers()
            
            prices = {
                'usdt': {},
                'btc': {},
                'eth': {}
            }
            
            for symbol, ticker in all_tickers.items():
                if symbol.endswith('/USDT') and ticker['last']:
                    prices['usdt'][symbol] = ticker['last']
                elif symbol.endswith('/BTC') and ticker['last']:
                    prices['btc'][symbol] = ticker['last']
                elif symbol.endswith('/ETH') and ticker['last']:
                    prices['eth'][symbol] = ticker['last']
            
            return prices
        except Exception as e:
            print(f"{Fore.RED}Error fetching prices: {e}")
            return None
    
    def find_triangular_arbitrage_usdt_btc_usdt(self, prices):
        """
        Triangle 1: USDT -> ALT -> BTC -> USDT
        Requires: ALT/USDT, ALT/BTC, BTC/USDT
        """
        opportunities = []
        
        if not prices['btc'] or 'BTC/USDT' not in prices['usdt']:
            return opportunities
        
        btc_usdt = prices['usdt']['BTC/USDT']
        
        for alt_pair in prices['btc']:
            # alt_pair format: "ALT/BTC"
            alt = alt_pair.split('/')[0]
            usdt_pair = f"{alt}/USDT"
            
            if usdt_pair not in prices['usdt']:
                continue
            
            alt_btc = prices['btc'][alt_pair]
            alt_usdt = prices['usdt'][usdt_pair]
            
            # Path: 1 USDT -> BTC (using BTC/USDT) -> ALT (using ALT/BTC) -> USDT (using ALT/USDT)
            # This is the reverse path, let's do the correct one:
            
            # Path A: USDT -> ALT -> BTC -> USDT
            # Step 1: 100 USDT -> ALT: amount_alt = 100 / alt_usdt
            # Step 2: ALT -> BTC: amount_btc = amount_alt * alt_btc
            # Step 3: BTC -> USDT: amount_usdt = amount_btc * btc_usdt
            
            amount_alt = self.trade_amount_usdt / alt_usdt
            amount_btc = amount_alt * alt_btc
            final_usdt = amount_btc * btc_usdt
            
            profit = final_usdt - self.trade_amount_usdt
            profit_pct = (profit / self.trade_amount_usdt) * 100
            
            # Apply fees (0.2% per trade)
            fee_factor = 0.998 ** 3
            final_usdt_with_fees = final_usdt * fee_factor
            profit_with_fees = final_usdt_with_fees - self.trade_amount_usdt
            profit_pct_with_fees = (profit_with_fees / self.trade_amount_usdt) * 100
            
            if profit_pct_with_fees > self.arbitrage_threshold:
                opportunities.append({
                    'type': 'USDT → ALT → BTC → USDT',
                    'pair': alt,
                    'profit_usdt': profit_with_fees,
                    'profit_pct': profit_pct_with_fees,
                    'details': {
                        'alt_usdt': alt_usdt,
                        'alt_btc': alt_btc,
                        'btc_usdt': btc_usdt
                    }
                })
            
            # Path B: USDT -> BTC -> ALT -> USDT
            # Step 1: 100 USDT -> BTC: amount_btc = 100 / btc_usdt
            # Step 2: BTC -> ALT: amount_alt = amount_btc / alt_btc
            # Step 3: ALT -> USDT: amount_usdt = amount_alt * alt_usdt
            
            amount_btc = self.trade_amount_usdt / btc_usdt
            amount_alt = amount_btc / alt_btc
            final_usdt = amount_alt * alt_usdt
            
            profit = final_usdt - self.trade_amount_usdt
            profit_pct = (profit / self.trade_amount_usdt) * 100
            
            final_usdt_with_fees = final_usdt * fee_factor
            profit_with_fees = final_usdt_with_fees - self.trade_amount_usdt
            profit_pct_with_fees = (profit_with_fees / self.trade_amount_usdt) * 100
            
            if profit_pct_with_fees > self.arbitrage_threshold:
                opportunities.append({
                    'type': 'USDT → BTC → ALT → USDT',
                    'pair': alt,
                    'profit_usdt': profit_with_fees,
                    'profit_pct': profit_pct_with_fees,
                    'details': {
                        'btc_usdt': btc_usdt,
                        'alt_btc': alt_btc,
                        'alt_usdt': alt_usdt
                    }
                })
        
        return opportunities
    
    def find_triangular_arbitrage_usdt_eth_usdt(self, prices):
        """
        Triangle 2: USDT -> ALT -> ETH -> USDT
        Requires: ALT/USDT, ALT/ETH, ETH/USDT
        """
        opportunities = []
        
        if not prices['eth'] or 'ETH/USDT' not in prices['usdt']:
            return opportunities
        
        eth_usdt = prices['usdt']['ETH/USDT']
        
        for alt_pair in prices['eth']:
            alt = alt_pair.split('/')[0]
            usdt_pair = f"{alt}/USDT"
            
            if usdt_pair not in prices['usdt']:
                continue
            
            alt_eth = prices['eth'][alt_pair]
            alt_usdt = prices['usdt'][usdt_pair]
            
            # Path: USDT -> ALT -> ETH -> USDT
            amount_alt = self.trade_amount_usdt / alt_usdt
            amount_eth = amount_alt * alt_eth
            final_usdt = amount_eth * eth_usdt
            
            fee_factor = 0.998 ** 3
            final_usdt_with_fees = final_usdt * fee_factor
            profit_with_fees = final_usdt_with_fees - self.trade_amount_usdt
            profit_pct_with_fees = (profit_with_fees / self.trade_amount_usdt) * 100
            
            if profit_pct_with_fees > self.arbitrage_threshold:
                opportunities.append({
                    'type': 'USDT → ALT → ETH → USDT',
                    'pair': alt,
                    'profit_usdt': profit_with_fees,
                    'profit_pct': profit_pct_with_fees,
                    'details': {
                        'alt_usdt': alt_usdt,
                        'alt_eth': alt_eth,
                        'eth_usdt': eth_usdt
                    }
                })
        
        return opportunities
    
    def find_dual_exchange_arbitrage(self, prices):
        """
        Compare Gate.io prices with theoretical fair prices
        """
        opportunities = []
        
        btc_usdt = prices['usdt'].get('BTC/USDT', 0)
        eth_usdt = prices['usdt'].get('ETH/USDT', 0)
        
        for symbol in prices['usdt']:
            if symbol in ['BTC/USDT', 'ETH/USDT', 'BNB/USDT']:
                continue
                
            current_price = prices['usdt'][symbol]
            
            # Calculate fair price through BTC
            alt = symbol.split('/')[0]
            alt_btc_pair = f"{alt}/BTC"
            
            if alt_btc_pair in prices['btc']:
                fair_price_btc = prices['btc'][alt_btc_pair] * btc_usdt
                price_diff = ((current_price - fair_price_btc) / fair_price_btc) * 100
                
                if abs(price_diff) > self.arbitrage_threshold:
                    opportunities.append({
                        'type': 'BTC/Hedge Arbitrage',
                        'pair': symbol,
                        'current': current_price,
                        'fair_via_btc': fair_price_btc,
                        'diff_pct': price_diff,
                        'action': 'SELL' if price_diff > 0 else 'BUY'
                    })
            
            # Calculate fair price through ETH
            alt_eth_pair = f"{alt}/ETH"
            if alt_eth_pair in prices['eth']:
                fair_price_eth = prices['eth'][alt_eth_pair] * eth_usdt
                price_diff = ((current_price - fair_price_eth) / fair_price_eth) * 100
                
                if abs(price_diff) > self.arbitrage_threshold:
                    opportunities.append({
                        'type': 'ETH/Hedge Arbitrage',
                        'pair': symbol,
                        'current': current_price,
                        'fair_via_eth': fair_price_eth,
                        'diff_pct': price_diff,
                        'action': 'SELL' if price_diff > 0 else 'BUY'
                    })
        
        return opportunities
    
    def scan_cycle(self):
        """Perform one complete scan cycle"""
        print(f"\n{Fore.CYAN}{'='*60}")
        print(f"🔍 Scanning at {datetime.now().strftime('%H:%M:%S')}")
        print(f"{'='*60}")
        
        prices = self.get_real_time_prices()
        if not prices:
            return
        
        # Display current prices
        print(f"\n{Fore.YELLOW}📊 Current Market Prices:")
        for pair in ['BTC/USDT', 'ETH/USDT', 'BNB/USDT']:
            if pair in prices['usdt']:
                print(f"  {pair}: ${prices['usdt'][pair]:,.2f}")
        
        # Find opportunities
        all_opportunities = []
        
        opp1 = self.find_triangular_arbitrage_usdt_btc_usdt(prices)
        opp2 = self.find_triangular_arbitrage_usdt_eth_usdt(prices)
        opp3 = self.find_dual_exchange_arbitrage(prices)
        
        all_opportunities.extend(opp1)
        all_opportunities.extend(opp2)
        all_opportunities.extend(opp3)
        
        # Sort by profit
        all_opportunities.sort(key=lambda x: x.get('profit_pct', x.get('diff_pct', 0)), reverse=True)
        
        # Display results
        if all_opportunities:
            print(f"\n{Fore.GREEN}🎯 PROFITABLE OPPORTUNITIES FOUND:")
            print(f"{'-'*60}")
            
            for opp in all_opportunities[:5]:
                if 'profit_pct' in opp:
                    print(f"\n{Fore.GREEN}✓ {opp['type']} - {opp['pair']}")
                    print(f"   Profit: ${opp['profit_usdt']:.2f} ({opp['profit_pct']:.3f}%)")
                else:
                    print(f"\n{Fore.CYAN}⟳ {opp['type']} - {opp['pair']}")
                    print(f"   Current: ${opp['current']:.4f} | Fair: ${opp.get('fair_via_btc', opp.get('fair_via_eth', 0)):.4f}")
                    print(f"   Difference: {opp['diff_pct']:.2f}% - {Fore.GREEN}{opp['action']}{Style.RESET_ALL}")
        else:
            print(f"\n{Fore.YELLOW}⏳ No opportunities above {self.arbitrage_threshold}% threshold")
        
        return all_opportunities
    
    def start_bot(self, interval_seconds=5):
        """Start the bot in continuous scanning mode"""
        self.running = True
        print(f"\n{Fore.GREEN}🚀 Bot started! Scanning every {interval_seconds} seconds")
        print(f"{Fore.YELLOW}💡 Profit threshold: {self.arbitrage_threshold}%")
        print(f"{Fore.YELLOW}💰 Trade amount: ${self.trade_amount_usdt} USDT")
        print(f"{Fore.RED}⚠️  Press Ctrl+C to stop\n")
        
        try:
            while self.running:
                self.scan_cycle()
                time.sleep(interval_seconds)
        except KeyboardInterrupt:
            self.stop_bot()
    
    def stop_bot(self):
        """Stop the bot"""
        self.running = False
        print(f"\n{Fore.RED}🛑 Bot stopped")
    
    def show_available_pairs(self):
        """Display what trading pairs are actually available"""
        print(f"\n{Fore.CYAN}{'='*60}")
        print("📋 AVAILABLE TRADING PAIRS ON GATE.IO")
        print(f"{'='*60}")
        
        self.load_markets()
        
        # Show BTC cross pairs
        print(f"\n{Fore.YELLOW}BTC Cross Pairs (ALT/BTC):")
        btc_crosses = [p for p in self.btc_pairs.keys() if p != 'BTC/BTC']
        for pair in sorted(btc_crosses)[:15]:
            print(f"  ✓ {pair}")
        
        # Show ETH cross pairs
        print(f"\n{Fore.YELLOW}ETH Cross Pairs (ALT/ETH):")
        eth_crosses = [p for p in self.eth_pairs.keys() if p != 'ETH/ETH']
        for pair in sorted(eth_crosses)[:15]:
            print(f"  ✓ {pair}")
        
        # Show your specific coins
        target_coins = ['BTC', 'ETH', 'BNB', 'LINK', 'AVAX', 'OP']
        print(f"\n{Fore.GREEN}🎯 Your Coins - Available Pairs:")
        for coin in target_coins:
            print(f"\n  {coin}:")
            for pair in self.usdt_pairs:
                if pair.startswith(f"{coin}/"):
                    print(f"    - {pair}")

def main():
    print(f"{Fore.CYAN}{'='*60}")
    print("🚀 GATE.IO ARBITRAGE BOT v2.0")
    print("Real triangular arbitrage with available pairs")
    print(f"{'='*60}")
    
    bot = GateIOArbitrageBot()
    
    # Load markets first
    bot.load_markets()
    
    # Show what's available
    bot.show_available_pairs()
    
    # Menu
    while True:
        print(f"\n{Fore.CYAN}{'='*40}")
        print("1. 📊 Show available pairs")
        print("2. 🔍 Single scan")
        print("3. 🚀 Start continuous bot")
        print("4. ⚙️  Settings")
        print("5. 🚪 Exit")
        
        choice = input(f"\n{Fore.YELLOW}Select option: ")
        
        if choice == '1':
            bot.show_available_pairs()
        elif choice == '2':
            bot.scan_cycle()
        elif choice == '3':
            interval = input("Scan interval (seconds) [5]: ")
            interval = int(interval) if interval.isdigit() else 5
            bot.start_bot(interval)
        elif choice == '4':
            new_threshold = input(f"Profit threshold % [{bot.arbitrage_threshold}]: ")
            if new_threshold:
                bot.arbitrage_threshold = float(new_threshold)
            new_amount = input(f"Trade amount USDT [{bot.trade_amount_usdt}]: ")
            if new_amount:
                bot.trade_amount_usdt = float(new_amount)
            print(f"{Fore.GREEN}✓ Settings updated")
        elif choice == '5':
            print(f"{Fore.RED}Goodbye!")
            break

if __name__ == "__main__":
    main()

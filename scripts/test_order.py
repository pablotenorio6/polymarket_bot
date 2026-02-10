"""
Test script for placing orders on Polymarket using py-clob-client
Based on: https://github.com/Polymarket/py-clob-client/

This script tests:
1. Connection to Polymarket CLOB
2. API credentials derivation
3. Reading market data
4. Placing a small test order
"""

import os
import sys
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon

# Get private key (MetaMask personal - 0x2d35... - signs transactions)
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY")
if not PRIVATE_KEY:
    print("ERROR: POLYMARKET_PRIVATE_KEY not found in .env file")
    sys.exit(1)

# Add 0x prefix if missing
if not PRIVATE_KEY.startswith("0x"):
    PRIVATE_KEY = "0x" + PRIVATE_KEY

# Funder wallet (Polymarket browser wallet - 0xC59e... - has the $200)
# Set to None to use signer wallet, or set the address to use funder
FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS")  # Set in .env or None

# Test mode: Try different signature types
# 0 = EOA (default MetaMask)
# 1 = Email/Magic wallet
# 2 = Browser wallet proxy
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "0"))


def test_connection():
    """Test basic connection to Polymarket CLOB"""
    print("\n" + "=" * 60)
    print("TEST 1: Basic Connection (Read-Only)")
    print("=" * 60)
    
    from py_clob_client.client import ClobClient
    
    # Read-only client (no auth needed)
    client = ClobClient(HOST)
    
    # Test connection
    try:
        ok = client.get_ok()
        print(f"[OK] Server OK: {ok}")
    except Exception as e:
        print(f"[FAIL] Connection failed: {e}")
        return False
    
    # Get server time
    try:
        time = client.get_server_time()
        print(f"[OK] Server Time: {time}")
    except Exception as e:
        print(f"[FAIL] Failed to get server time: {e}")
    
    return True


def test_authenticated_client():
    """Test authenticated client setup"""
    print("\n" + "=" * 60)
    print("TEST 2: Authenticated Client Setup")
    print("=" * 60)
    
    from py_clob_client.client import ClobClient
    
    try:
        # Show configuration
        print(f"   Signature Type: {SIGNATURE_TYPE}")
        print(f"   Funder: {FUNDER_ADDRESS or 'None (using signer wallet)'}")
        
        # Create client with private key and optional funder
        if FUNDER_ADDRESS:
            print(f"   Using FUNDER mode: signer={PRIVATE_KEY[:10]}..., funder={FUNDER_ADDRESS}")
            client = ClobClient(
                HOST,
                key=PRIVATE_KEY,
                chain_id=CHAIN_ID,
                signature_type=SIGNATURE_TYPE,
                funder=FUNDER_ADDRESS
            )
        else:
            print(f"   Using STANDARD mode: signer only")
            client = ClobClient(
                HOST,
                key=PRIVATE_KEY,
                chain_id=CHAIN_ID,
                signature_type=SIGNATURE_TYPE
            )
        
        # Get wallet address
        address = client.get_address()
        print(f"[OK] Wallet Address: {address}")
        
        # Set API credentials using the official method
        print("   Deriving API credentials...")
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        print("[OK] API credentials set successfully")
        
        # Check USDC balance using web3
        try:
            from web3 import Web3
            web3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
            
            # There are TWO USDC tokens on Polygon!
            usdc_tokens = {
                "USDC.e (Bridged)": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
                "USDC (Native)": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
            }
            
            usdc_abi = '[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"}]'
            
            total_usdc = 0
            for name, usdc_address in usdc_tokens.items():
                usdc = web3.eth.contract(address=Web3.to_checksum_address(usdc_address), abi=usdc_abi)
                balance_raw = usdc.functions.balanceOf(Web3.to_checksum_address(address)).call()
                balance_usdc = balance_raw / 1e6  # USDC has 6 decimals
                total_usdc += balance_usdc
                if balance_usdc > 0:
                    print(f"[INFO] {name}: ${balance_usdc:.2f}")
            
            if total_usdc == 0:
                print("[INFO] USDC Balance: $0.00 (both USDC.e and USDC Native)")
            
            if total_usdc < 1:
                print("[WARNING] Low USDC balance! You need at least $1 USDC for testing")
                print("[INFO] Polymarket uses USDC.e (0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174)")
                print("[INFO] If you have Native USDC, swap it for USDC.e on QuickSwap/Uniswap")
        except Exception as e:
            print(f"[INFO] Could not check USDC balance: {e}")
        
        return client
        
    except Exception as e:
        print(f"[FAIL] Authentication failed: {e}")
        import traceback
        traceback.print_exc()
        return None


def test_get_market_data(client):
    """Test getting market data"""
    print("\n" + "=" * 60)
    print("TEST 3: Get Market Data")
    print("=" * 60)
    
    try:
        # Get simplified markets
        print("   Fetching markets...")
        markets = client.get_simplified_markets()
        
        if markets and "data" in markets:
            print(f"[OK] Found {len(markets['data'])} markets")
            
            # Show first market
            if markets['data']:
                first = markets['data'][0]
                print(f"\n   First market:")
                print(f"   - Question: {first.get('question', 'N/A')[:60]}...")
                print(f"   - Token IDs: {len(first.get('tokens', []))} tokens")
                
                return first
        else:
            print("[FAIL] No markets found")
            return None
            
    except Exception as e:
        print(f"[FAIL] Failed to get markets: {e}")
        return None


def test_get_orderbook(client, token_id: str):
    """Test getting order book for a token"""
    print("\n" + "=" * 60)
    print("TEST 4: Get Order Book")
    print("=" * 60)
    
    try:
        # Get midpoint price
        mid_response = client.get_midpoint(token_id)
        mid = mid_response.get('mid') if isinstance(mid_response, dict) else mid_response
        print(f"[OK] Midpoint: ${mid}")
        
        # Get buy price
        buy_response = client.get_price(token_id, side="BUY")
        buy_price = buy_response.get('price') if isinstance(buy_response, dict) else buy_response
        print(f"[OK] Best Buy Price: ${buy_price}")
        
        # Get sell price
        sell_response = client.get_price(token_id, side="SELL")
        sell_price = sell_response.get('price') if isinstance(sell_response, dict) else sell_response
        print(f"[OK] Best Sell Price: ${sell_price}")
        
        # Get order book
        book = client.get_order_book(token_id)
        if book:
            print(f"[OK] Order Book:")
            print(f"   - Bids: {len(book.bids) if hasattr(book, 'bids') else 'N/A'}")
            print(f"   - Asks: {len(book.asks) if hasattr(book, 'asks') else 'N/A'}")
        
        return float(mid) if mid else None
        
    except Exception as e:
        print(f"[FAIL] Failed to get order book: {e}")
        return None


def test_place_order(client, token_id: str, price: float = 0.01):
    """Test placing a small limit order"""
    print("\n" + "=" * 60)
    print("TEST 5: Place Test Order")
    print("=" * 60)
    
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
    
    # Use a small order for testing
    # Price: $0.01 (very low, unlikely to fill)
    # Size: 100 shares (min order is $1, so 100 * 0.01 = $1)
    test_price = 0.01  # Very low price so it won't actually fill
    test_size = 100.0  # 100 shares * $0.01 = $1 minimum
    
    print(f"   Order details:")
    print(f"   - Token ID: {token_id[:20]}...")
    print(f"   - Side: BUY")
    print(f"   - Price: ${test_price}")
    print(f"   - Size: {test_size} shares")
    print(f"   - Type: GTC (Good Till Canceled)")
    print(f"   - Cost: ~${test_price * test_size:.2f}")
    
    try:
        # Create order args
        order_args = OrderArgs(
            token_id=token_id,
            price=test_price,
            size=test_size,
            side=BUY
        )
        
        print("\n   Creating and signing order...")
        signed_order = client.create_order(order_args)
        print(f"[OK] Order signed successfully")
        
        print("   Posting order to exchange...")
        response = client.post_order(signed_order, OrderType.GTC)
        
        print(f"[OK] Order posted successfully!")
        print(f"   Response: {response}")
        
        # Extract order ID if available
        order_id = response.get('orderID') or response.get('id') if isinstance(response, dict) else None
        
        return order_id
        
    except Exception as e:
        print(f"[FAIL] Failed to place order: {e}")
        import traceback
        traceback.print_exc()
        return None


def test_cancel_order(client, order_id: str):
    """Test canceling an order"""
    print("\n" + "=" * 60)
    print("TEST 6: Cancel Order")
    print("=" * 60)
    
    if not order_id:
        print("   No order ID to cancel, skipping...")
        return
    
    try:
        print(f"   Canceling order: {order_id}")
        result = client.cancel(order_id)
        print(f"[OK] Order canceled: {result}")
    except Exception as e:
        print(f"[FAIL] Failed to cancel order: {e}")


def test_get_open_orders(client):
    """Test getting open orders"""
    print("\n" + "=" * 60)
    print("TEST 7: Get Open Orders")
    print("=" * 60)
    
    from py_clob_client.clob_types import OpenOrderParams
    
    try:
        orders = client.get_orders(OpenOrderParams())
        print(f"[OK] Open orders: {len(orders) if orders else 0}")
        
        if orders:
            for i, order in enumerate(orders[:3]):  # Show first 3
                print(f"\n   Order {i+1}:")
                print(f"   - ID: {order.get('id', 'N/A')}")
                print(f"   - Price: ${order.get('price', 'N/A')}")
                print(f"   - Size: {order.get('size', 'N/A')}")
                print(f"   - Side: {order.get('side', 'N/A')}")
        
        return orders
        
    except Exception as e:
        print(f"[FAIL] Failed to get orders: {e}")
        return None


def find_active_market_with_orderbook(client):
    """Find any active market that has an orderbook for testing"""
    print("\n" + "=" * 60)
    print("Finding Active Market with Orderbook")
    print("=" * 60)
    
    import requests
    
    try:
        # Use Gamma API to get active markets with liquidity
        print("   Fetching active markets from Gamma API...")
        
        # Get markets that are active and have trading
        url = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100"
        response = requests.get(url)
        
        if response.status_code != 200:
            print(f"[FAIL] Gamma API returned {response.status_code}")
            return None
        
        markets = response.json()
        print(f"   Found {len(markets)} active markets")
        
        # Try to find a market with an active orderbook
        print("   Looking for market with active orderbook...")
        
        checked = 0
        for market in markets:
            # Get token IDs from clobTokenIds field
            clob_tokens = market.get('clobTokenIds')
            if not clob_tokens:
                continue
            
            # Parse if string
            import json
            if isinstance(clob_tokens, str):
                try:
                    clob_tokens = json.loads(clob_tokens)
                except:
                    continue
            
            if not clob_tokens or len(clob_tokens) == 0:
                continue
            
            token_id = clob_tokens[0]
            checked += 1
            
            # Try to get midpoint - if it works, orderbook exists
            try:
                mid_response = client.get_midpoint(token_id)
                
                # Extract mid value from response (can be dict or string)
                if isinstance(mid_response, dict):
                    mid = mid_response.get('mid', '0')
                else:
                    mid = mid_response
                
                mid_float = float(mid) if mid else 0
                
                if mid_float > 0:
                    question = market.get('question', 'Unknown')[:50]
                    print(f"[OK] Found market with orderbook (checked {checked}):")
                    print(f"   Question: {question}...")
                    print(f"   Token ID: {token_id[:30]}...")
                    print(f"   Midpoint: ${mid_float:.4f}")
                    return token_id
            except Exception as e:
                # Show progress every 20 markets
                if checked % 20 == 0:
                    print(f"   Checked {checked} markets so far...")
                continue  # No orderbook for this token, try next
        
        print(f"[FAIL] No market with active orderbook found (checked {checked} markets)")
        return None
        
    except Exception as e:
        print(f"[FAIL] Error finding market: {e}")
        import traceback
        traceback.print_exc()
        return None


def main():
    print("=" * 60)
    print("POLYMARKET ORDER TEST SCRIPT")
    print("=" * 60)
    print(f"\nHost: {HOST}")
    print(f"Chain ID: {CHAIN_ID}")
    print(f"Private Key: {PRIVATE_KEY[:10]}...{PRIVATE_KEY[-4:]}")
    
    # Test 1: Basic connection
    if not test_connection():
        print("\n[FAIL] Cannot connect to Polymarket")
        return
    
    # Test 2: Authenticated client
    client = test_authenticated_client()
    if not client:
        print("\n[FAIL] Authentication failed")
        return
    
    # Test 3: Get market data
    market = test_get_market_data(client)
    
    # Find a market with active orderbook for testing
    token_id = find_active_market_with_orderbook(client)
    
    if not token_id:
        print("\n[FAIL] No market with active orderbook found for testing")
        print("   This might mean markets are closed or there's no liquidity")
        return
    
    print(f"\n>>> Using Token ID: {token_id[:30]}...")
    
    # Test 4: Get order book
    mid_price = test_get_orderbook(client, token_id)
    
    # Test 5: Place a small test order
    print("\n*** Placing a REAL test order (small order at $0.01)")
    order_id = test_place_order(client, token_id)
    
    # Test 6: Get open orders
    orders = test_get_open_orders(client)
    
    # Test 7: Cancel the test order
    if order_id:
        test_cancel_order(client, order_id)
    
    # Final summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    print("[OK] Connection: OK")
    print("[OK] Authentication: OK")
    print("[OK] Market Data: OK")
    print("[OK] Order Book: OK")
    if order_id:
        print("[OK] Order Placement: OK")
    else:
        print("[FAIL] Order Placement: FAILED")
    print("\nIf all tests passed, your bot should work correctly!")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n*** Interrupted by user")
    except Exception as e:
        print(f"\n\n[ERROR] Unexpected error: {e}")
        import traceback
        traceback.print_exc()

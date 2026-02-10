"""
Script to set token allowances for Polymarket trading
Based on: https://gist.github.com/poly-rodr/44313920481de58d5a3f6d1f8226bd5e

This script approves USDC and CTF tokens for Polymarket's exchange contracts.
You need to run this ONCE before you can place orders on Polymarket.

Requirements:
- pip install web3
- POL/MATIC tokens in your wallet (for gas fees, ~0.5 POL should be enough)
- USDC tokens in your wallet (for trading)

Note: POL and MATIC are interchangeable on Polygon network.
"""

import os
from web3 import Web3
from web3.constants import MAX_INT
from web3.middleware import ExtraDataToPOAMiddleware
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
RPC_URL = "https://polygon-rpc.com"  # Free Polygon RPC
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY")
CHAIN_ID = 137  # Polygon Mainnet

# Contract addresses on Polygon
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"   # Conditional Tokens

# Polymarket Exchange contracts that need approval
EXCHANGES = {
    "CTF Exchange": "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
    "Neg Risk CTF Exchange": "0xC5d563A36AE78145C45a50134d48A1215220f80a",
    "Neg Risk Adapter": "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
}

# ABIs
ERC20_APPROVE_ABI = '[{"constant": false,"inputs": [{"name": "_spender","type": "address"},{"name": "_value","type": "uint256"}],"name": "approve","outputs": [{"name": "", "type": "bool"}],"payable": false,"stateMutability": "nonpayable","type": "function"}]'

ERC1155_SET_APPROVAL_ABI = '[{"inputs": [{"internalType": "address","name": "operator","type": "address"},{"internalType": "bool","name": "approved","type": "bool"}],"name": "setApprovalForAll","outputs": [],"stateMutability": "nonpayable","type": "function"}]'


def setup_web3():
    """Initialize Web3 connection"""
    print(f"🔗 Connecting to Polygon via {RPC_URL}...")
    web3 = Web3(Web3.HTTPProvider(RPC_URL))
    web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    
    if not web3.is_connected():
        raise Exception("❌ Failed to connect to Polygon RPC")
    
    print("✅ Connected to Polygon")
    return web3


def get_account(web3):
    """Get account from private key"""
    if not PRIVATE_KEY:
        raise ValueError("❌ POLYMARKET_PRIVATE_KEY not found in .env file")
    
    account = web3.eth.account.from_key(PRIVATE_KEY)
    print(f"👛 Wallet Address: {account.address}")
    
    # Check POL/MATIC balance
    balance_wei = web3.eth.get_balance(account.address)
    balance_pol = web3.from_wei(balance_wei, 'ether')
    print(f"💰 POL/MATIC Balance: {balance_pol:.4f}")
    
    if balance_pol < 0.1:
        print("⚠️  WARNING: Low balance. You need POL/MATIC for gas fees!")
        print("   Get POL from: https://wallet.polygon.technology/")
    
    return account


def send_transaction(web3, account, raw_tx, description):
    """Sign and send a transaction"""
    print(f"\n📝 {description}")
    
    try:
        # Sign transaction
        signed_tx = web3.eth.account.sign_transaction(raw_tx, private_key=account.key)
        
        # Send transaction
        tx_hash = web3.eth.send_raw_transaction(signed_tx.raw_transaction)
        print(f"   Tx Hash: {tx_hash.hex()}")
        
        # Wait for receipt
        print("   Waiting for confirmation...")
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
        
        if receipt.status == 1:
            print(f"   ✅ Success! (Gas used: {receipt.gasUsed})")
        else:
            print(f"   ❌ Transaction failed!")
            
        return receipt
        
    except Exception as e:
        print(f"   ❌ Error: {e}")
        raise


def approve_usdc(web3, account, usdc_contract, exchange_address, exchange_name, nonce):
    """Approve USDC for an exchange"""
    raw_tx = usdc_contract.functions.approve(
        exchange_address,
        int(MAX_INT, 0)  # Maximum approval
    ).build_transaction({
        "chainId": CHAIN_ID,
        "from": account.address,
        "nonce": nonce,
        "gas": 100000,  # Estimated gas
        "gasPrice": web3.eth.gas_price
    })
    
    return send_transaction(
        web3, account, raw_tx,
        f"Approving USDC for {exchange_name}"
    )


def approve_ctf(web3, account, ctf_contract, exchange_address, exchange_name, nonce):
    """Approve CTF tokens for an exchange"""
    raw_tx = ctf_contract.functions.setApprovalForAll(
        exchange_address,
        True
    ).build_transaction({
        "chainId": CHAIN_ID,
        "from": account.address,
        "nonce": nonce,
        "gas": 100000,  # Estimated gas
        "gasPrice": web3.eth.gas_price
    })
    
    return send_transaction(
        web3, account, raw_tx,
        f"Approving CTF for {exchange_name}"
    )


def main():
    print("=" * 70)
    print("🚀 Polymarket Allowance Setup")
    print("=" * 70)
    print()
    print("This script will approve USDC and CTF tokens for Polymarket trading.")
    print("You will need to confirm 6 transactions (2 per exchange).")
    print()
    
    # Setup
    web3 = setup_web3()
    account = get_account(web3)
    
    # Get initial nonce
    nonce = web3.eth.get_transaction_count(account.address)
    print(f"📊 Starting nonce: {nonce}")
    
    # Create contract instances
    usdc = web3.eth.contract(
        address=Web3.to_checksum_address(USDC_ADDRESS),
        abi=ERC20_APPROVE_ABI
    )
    ctf = web3.eth.contract(
        address=Web3.to_checksum_address(CTF_ADDRESS),
        abi=ERC1155_SET_APPROVAL_ABI
    )
    
    print("\n" + "=" * 70)
    print("🔓 Setting Allowances...")
    print("=" * 70)
    
    # Approve for each exchange
    for exchange_name, exchange_address in EXCHANGES.items():
        print(f"\n📍 {exchange_name}")
        print(f"   Address: {exchange_address}")
        
        # Approve USDC
        approve_usdc(web3, account, usdc, exchange_address, exchange_name, nonce)
        nonce += 1
        
        # Approve CTF
        approve_ctf(web3, account, ctf, exchange_address, exchange_name, nonce)
        nonce += 1
    
    print("\n" + "=" * 70)
    print("✅ ALL APPROVALS COMPLETED!")
    print("=" * 70)
    print()
    print("Your wallet is now set up for Polymarket trading! 🎉")
    print("You can now run your trading bot with: python main.py")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user")
    except Exception as e:
        print(f"\n\n❌ Error: {e}")
        print("\nIf you're having issues, make sure:")
        print("1. You have MATIC for gas fees")
        print("2. Your POLYMARKET_PRIVATE_KEY is correct in .env")
        print("3. You're connected to Polygon network")


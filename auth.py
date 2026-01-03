"""
Authentication for Polymarket API

Polymarket uses Ethereum wallet signatures for authentication.
You need to use the py-clob-client library for proper authentication.

Install: pip install py-clob-client
"""

import os
import logging
from typing import Optional
from pathlib import Path

# Create logger first
logger = logging.getLogger(__name__)

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    
    # Look for .env in current directory and parent directories
    env_path = Path('.env')
    if env_path.exists():
        load_dotenv(env_path)
        logger.debug(f"Loaded environment variables from {env_path.absolute()}")
    else:
        # Try to find .env in parent directory
        parent_env = Path(__file__).parent / '.env'
        if parent_env.exists():
            load_dotenv(parent_env)
            logger.debug(f"Loaded environment variables from {parent_env.absolute()}")
except ImportError:
    logger.debug("python-dotenv not installed. Using system environment variables only.")


class PolymarketAuth:
    """
    Handles Polymarket authentication using py-clob-client
    
    Supports two modes:
    1. Standard mode: Uses signer wallet for both signing and funds
    2. Funder mode: Uses signer wallet for signing, funder wallet for funds
    """
    
    def __init__(self):
        self.private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
        self.funder_address = os.getenv("POLYMARKET_FUNDER_ADDRESS")
        self.signature_type = int(os.getenv("SIGNATURE_TYPE", "0"))
        self.client = None
        
        if not self.private_key:
            logger.warning("POLYMARKET_PRIVATE_KEY not set. Trading will be disabled.")
    
    def initialize_client(self):
        """
        Initialize the Polymarket CLOB client with authentication
        
        Based on: https://github.com/Polymarket/py-clob-client/
        
        Configuration via environment variables:
        - POLYMARKET_PRIVATE_KEY: Wallet that signs transactions
        - POLYMARKET_FUNDER_ADDRESS: (Optional) Wallet that has the funds
        - SIGNATURE_TYPE: 0=EOA, 1=Magic, 2=Browser proxy
        
        Requires: pip install py-clob-client
        """
        if not self.private_key:
            raise ValueError("Private key not set. Set POLYMARKET_PRIVATE_KEY environment variable.")
        
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.constants import POLYGON
            
            logger.debug("Initializing Polymarket client...")
            
            # Ensure private key has 0x prefix
            private_key = self.private_key
            if not private_key.startswith("0x"):
                private_key = "0x" + private_key
            
            # Create client based on configuration
            if self.funder_address:
                # Funder mode: signer signs, funder has funds
                logger.debug(f"Using FUNDER mode (sig_type={self.signature_type}, funder={self.funder_address[:10]}...)")
                
                self.client = ClobClient(
                    host="https://clob.polymarket.com",
                    key=private_key,
                    chain_id=POLYGON,
                    signature_type=self.signature_type,
                    funder=self.funder_address
                )
            else:
                # Standard mode: signer does everything
                logger.debug(f"Using STANDARD mode (signature_type={self.signature_type})")
                
                self.client = ClobClient(
                    host="https://clob.polymarket.com",
                    key=private_key,
                    chain_id=POLYGON,
                    signature_type=self.signature_type
                )
            
            # Get wallet addresses
            signer_address = self.client.get_address()
            logger.debug(f"Signer wallet: {signer_address}")
            
            # Set API credentials using the official method
            logger.debug("Setting up API credentials...")
            creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(creds)
            
            logger.debug("Polymarket client ready")
            return self.client
            
        except ImportError:
            logger.error("py-clob-client not installed. Run: pip install py-clob-client")
            raise
        except Exception as e:
            logger.error(f"Failed to initialize client: {e}")
            raise
    
    def get_client(self):
        """Get the initialized client"""
        if not self.client:
            self.initialize_client()
        return self.client
    
    def is_authenticated(self) -> bool:
        """Check if we have valid authentication"""
        return self.client is not None


# Singleton instance
_auth_instance: Optional[PolymarketAuth] = None


def get_auth() -> PolymarketAuth:
    """Get the singleton auth instance"""
    global _auth_instance
    if _auth_instance is None:
        _auth_instance = PolymarketAuth()
    return _auth_instance


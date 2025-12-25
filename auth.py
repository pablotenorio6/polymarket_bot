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
    """
    
    def __init__(self):
        self.private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
        self.client = None
        
        if not self.private_key:
            logger.warning("POLYMARKET_PRIVATE_KEY not set. Trading will be disabled.")
    
    def initialize_client(self):
        """
        Initialize the Polymarket CLOB client
        
        Requires: pip install py-clob-client
        """
        if not self.private_key:
            raise ValueError("Private key not set. Set POLYMARKET_PRIVATE_KEY environment variable.")
        
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.constants import POLYGON
            
            # Initialize client with private key
            self.client = ClobClient(
                host="https://clob.polymarket.com",
                key=self.private_key,
                chain_id=POLYGON  # Polymarket runs on Polygon
            )
            
            logger.info("Successfully initialized Polymarket client")
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


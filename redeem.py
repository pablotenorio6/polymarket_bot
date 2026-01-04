"""
Automatic redemption of winnings from resolved Polymarket markets
Uses polymarket_apis library

Runs every REDEEM_INTERVAL_HOURS to collect all available rewards.
"""

import os
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Configuration
REDEEM_INTERVAL_HOURS = 1  # How often to check for redeemable positions


class RedeemManager:
    """
    Manages automatic redemption of resolved market positions
    """
    
    def __init__(self):
        self.last_redeem_time: Optional[datetime] = None
        self.web3_client = None
        self.data_client = None
        self.wallet_address = None
        self.initialized = False
    
    def initialize(self) -> bool:
        """Initialize the redeem clients"""
        try:
            private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
            if not private_key:
                logger.warning("POLYMARKET_PRIVATE_KEY not set - redeem disabled")
                return False
            
            signature_type = int(os.getenv("SIGNATURE_TYPE", "0"))
            funder_address = os.getenv("POLYMARKET_FUNDER_ADDRESS")
            
            from polymarket_apis import PolymarketWeb3Client, PolymarketDataClient
            
            self.web3_client = PolymarketWeb3Client(
                private_key=private_key,
                signature_type=signature_type
            )
            self.data_client = PolymarketDataClient()
            
            # Determine wallet address
            if funder_address:
                self.wallet_address = funder_address
            else:
                self.wallet_address = self.web3_client.address
            
            logger.debug(f"Redeem manager initialized for {self.wallet_address[:10]}...")
            self.initialized = True
            return True
            
        except ImportError:
            logger.warning("polymarket_apis not installed - redeem disabled")
            logger.warning("Install with: pip install polymarket_apis")
            return False
        except Exception as e:
            logger.error(f"Failed to initialize redeem manager: {e}")
            return False
    
    def should_run(self) -> bool:
        """Check if it's time to run redeem"""
        # Try to initialize if not done yet
        if not self.initialized:
            if not self.initialize():
                return False  # Can't initialize, skip
        
        if self.last_redeem_time is None:
            return True
        
        elapsed = datetime.now() - self.last_redeem_time
        return elapsed >= timedelta(hours=REDEEM_INTERVAL_HOURS)
    
    def check_and_redeem(self) -> Optional[Dict]:
        """
        Check if redeem should run and execute if needed.
        Convenience method for use in trading loops.
        
        Returns:
            Redeem results if redeem was run, None if skipped
        """
        if self.should_run():
            return self.run_redeem()
        return None
    
    def run_redeem(self) -> Dict:
        """
        Check for and redeem all available positions
        
        Returns:
            Dict with results: {
                'success': bool,
                'positions_found': int,
                'positions_redeemed': int,
                'total_value': float,
                'errors': list
            }
        """
        results = {
            'success': True,
            'positions_found': 0,
            'positions_redeemed': 0,
            'total_value': 0.0,
            'errors': []
        }
        
        if not self.initialized:
            if not self.initialize():
                results['success'] = False
                results['errors'].append("Failed to initialize")
                return results
        
        try:
            # Get redeemable positions
            positions = self.data_client.get_positions(
                self.wallet_address, 
                redeemable=True
            )
            
            if not positions:
                logger.debug("No redeemable positions found")
                self.last_redeem_time = datetime.now()
                return results
            
            results['positions_found'] = len(positions)
            logger.info(f"Found {len(positions)} redeemable positions")
            
            # Calculate total value
            for pos in positions:
                if hasattr(pos, 'current_value'):
                    results['total_value'] += pos.current_value
            
            # Redeem each position
            for pos in positions:
                try:
                    # Prepare amounts array [Yes shares, No shares]
                    amounts = [0, 0]
                    if hasattr(pos, 'outcome_index') and hasattr(pos, 'size'):
                        size_wei = int(pos.size * 1e6)
                        amounts[pos.outcome_index] = size_wei
                    
                    neg_risk = pos.negative_risk if hasattr(pos, 'negative_risk') else False
                    
                    # Execute redeem
                    result = self.web3_client.redeem_position(
                        condition_id=pos.condition_id,
                        amounts=amounts,
                        neg_risk=neg_risk
                    )
                    
                    if result:
                        results['positions_redeemed'] += 1
                        value = pos.current_value if hasattr(pos, 'current_value') else 0
                        logger.info(f"Redeemed position: ${value:.2f}")
                    else:
                        results['errors'].append(f"No TX for {pos.condition_id[:10]}...")
                        
                except Exception as e:
                    error_msg = f"Error redeeming {pos.condition_id[:10]}...: {e}"
                    logger.error(error_msg)
                    results['errors'].append(error_msg)
            
            self.last_redeem_time = datetime.now()
            
            if results['positions_redeemed'] > 0:
                logger.info(f"REDEEMED: {results['positions_redeemed']}/{results['positions_found']} positions (${results['total_value']:.2f})")
            
        except Exception as e:
            logger.error(f"Error in redeem process: {e}")
            results['success'] = False
            results['errors'].append(str(e))
        
        return results


# Singleton instance
_redeem_manager: Optional[RedeemManager] = None


def get_redeem_manager() -> RedeemManager:
    """Get the singleton redeem manager"""
    global _redeem_manager
    if _redeem_manager is None:
        _redeem_manager = RedeemManager()
    return _redeem_manager


def run_redeem_if_needed() -> Optional[Dict]:
    """
    Convenience function to run redeem if interval has passed.
    Call this periodically from main loop.
    
    Returns:
        Redeem results if redeem was run, None if skipped
    """
    manager = get_redeem_manager()
    
    if manager.should_run():
        return manager.run_redeem()
    
    return None


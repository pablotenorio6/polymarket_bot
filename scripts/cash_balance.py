from __future__ import annotations

import logging
from typing import Optional, Any

logger = logging.getLogger(__name__)


def get_available_cash_usdc_from_clob_client(clob_client: Any) -> Optional[float]:
    """
    Fetch *available* collateral balance (USDC cash) from Polymarket CLOB (L2).

    Returns:
        float balance (USDC) on success, None on failure.
    """
    if clob_client is None:
        return None

    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

        resp = clob_client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        # Note: CLOB returns collateral balance in micro-USDC (6 decimals).
        balance_raw = resp.get("balance", "0")
        balance_micro = float(balance_raw)
        balance_usdc = balance_micro / 1_000_000.0
        # Helpful for verifying against UI cash balance
        logger.info(
            f"parsed_balance_micro={balance_micro} parsed_balance_usdc={balance_usdc}"
        )
        return balance_usdc
    except Exception as e:
        logger.warning(f"Could not fetch available cash (collateral) balance: {e}")
        return None


"""
Post-Close Sniper Strategy

Exploits the ~2-minute post-close window where Polymarket CLOB still accepts
trades after a market theoretically ends. Uses the Chainlink price feed
(the actual resolution oracle) to determine the winner before resolution,
then buys the winning side at cheap prices during the lag window.

How it works:
1. PREPARATION: On market detection, subscribe to Chainlink + Binance feeds
2. PRICE-TO-BEAT CAPTURE: Record Chainlink price at market start time
3. MONITORING: Track Chainlink price vs price-to-beat throughout the market
4. SNIPE WINDOW: In the last seconds before close + ~120s after close,
   if Chainlink clearly shows a winner, buy that side aggressively via FOK
5. POST-CLOSE CASCADE: Place FOK buys at increasing prices until filled

Key insight: The CLOB continues accepting orders for ~120s after end_time.
Chainlink price (used for actual resolution) is available via RTDS ~1s updates.
Binance price lags Chainlink by $60-70 but is irrelevant for resolution.

Requires:
- RTDS with Chainlink subscription (crypto_prices_chainlink topic)
- Fast order execution (FOK orders via FastTrader)
- Extended market monitoring window (don't stop at end_time)
"""

import logging
import time
from typing import Dict, Optional, List, Tuple
from datetime import datetime, timedelta
from enum import Enum

import pytz

from strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class SnipePhase(Enum):
    """Current phase of the sniper strategy for a market"""
    WAITING = "waiting"           # Market not yet near end
    PRE_SNIPE = "pre_snipe"       # Last 30s before close, high-alert monitoring
    SNIPE_ACTIVE = "snipe_active" # Post-close window, actively sniping
    DONE = "done"                 # Sniped or window expired


# FOK order price cascade - try cheap first, then more aggressive
SNIPE_PRICE_CASCADE = [0.01, 0.02, 0.05, 0.10, 0.20, 0.35, 0.50]

# Configuration
CHAINLINK_CONFIDENCE_THRESHOLD_USD = 15.0  # Min $ delta from price_to_beat to act
PRE_SNIPE_SECONDS = 30                     # Start pre-snipe N seconds before close
POST_CLOSE_WINDOW_SECONDS = 120            # How long post-close to keep sniping
MAX_SNIPE_ATTEMPTS = 10                    # Max FOK orders per market
SNIPE_SIZE_USD = 200                       # Size per snipe order
CASCADE_INTERVAL_MS = 500                  # Min ms between cascade attempts


class PostCloseSniperStrategy(BaseStrategy):
    """
    Snipe the post-close window using Chainlink oracle prices.

    Unlike early_queue (passive GTC orders placed hours early),
    this strategy is reactive: it watches the Chainlink price in real-time,
    waits until the resolution outcome is clear, then aggressively buys
    the winning side during the post-close lag window.

    Can run alongside early_queue for complementary coverage.
    """

    name = "postclose_sniper"
    description = "Snipe post-close window using Chainlink oracle feed"

    # Resource requirements
    requires_price_websocket = True   # Need token prices to see orderbook state
    requires_data_collector = True    # Record outcomes
    requires_rtds = True              # Need Chainlink + Binance feeds

    # Keep monitoring for 120s after market close (the CLOB lag window)
    post_close_grace_seconds = POST_CLOSE_WINDOW_SECONDS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Per-market state: condition_id -> MarketSnipeState
        self.market_states: Dict[str, "MarketSnipeState"] = {}

        # Chainlink price cache (separate from Binance in rtds_client)
        self.chainlink_prices: Dict[str, float] = {}  # symbol -> price
        self.chainlink_last_ts: Dict[str, float] = {}  # symbol -> timestamp_ms

    async def initialize(self) -> None:
        """
        Initialize: subscribe to Chainlink feed via RTDS.

        The existing rtds_client subscribes to crypto_prices (Binance).
        We need to ALSO subscribe to crypto_prices_chainlink.
        """
        logger.info("[Sniper] Initializing post-close sniper strategy")

        if self.rtds_client and self.rtds_client.ws and self.rtds_client.connected:
            await self._subscribe_chainlink()
        else:
            logger.warning("[Sniper] RTDS not connected - Chainlink feed unavailable")
            logger.warning("[Sniper] Strategy will not be able to determine winners")

    async def _subscribe_chainlink(self):
        """
        Subscribe to Chainlink price feed on the existing RTDS WebSocket.

        Topic: crypto_prices_chainlink, type: update
        Payload format: {symbol: "btc/usd", value: 68862.10, timestamp: 1770711759000}
        """
        import json

        subscribe_msg = {
            "action": "subscribe",
            "subscriptions": [{
                "topic": "crypto_prices_chainlink",
                "type": "update",
            }]
        }

        try:
            await self.rtds_client.ws.send(json.dumps(subscribe_msg))
            logger.info("[Sniper] Subscribed to crypto_prices_chainlink via RTDS")

            # Patch the RTDS message handler to also route Chainlink messages to us
            original_handler = self.rtds_client._handle_message
            strategy_ref = self  # Capture reference for closure

            def patched_handler(raw_message: str):
                # Call original handler first (for Binance prices)
                original_handler(raw_message)
                # Also process Chainlink messages
                try:
                    import json as _json
                    data = _json.loads(raw_message)
                    if data.get('topic') == 'crypto_prices_chainlink' and data.get('type') == 'update':
                        strategy_ref._process_chainlink_update(data.get('payload', {}))
                except Exception:
                    pass

            self.rtds_client._handle_message = patched_handler
            logger.info("[Sniper] Patched RTDS handler for Chainlink messages")

        except Exception as e:
            logger.error(f"[Sniper] Failed to subscribe to Chainlink: {e}")

    def _process_chainlink_update(self, payload: Dict):
        """Process a Chainlink price update from RTDS."""
        if not isinstance(payload, dict):
            return

        symbol = payload.get('symbol', '')  # e.g. "btc/usd"
        value = payload.get('value')
        ts = payload.get('timestamp', 0)

        if value is None or not symbol:
            return

        # Normalize symbol: "btc/usd" -> "BTC"
        normalized = symbol.split('/')[0].upper()

        self.chainlink_prices[normalized] = float(value)
        self.chainlink_last_ts[normalized] = ts

        logger.debug(f"[Sniper] Chainlink {normalized}: ${float(value):,.2f}")

    def get_chainlink_price(self, symbol: str = None) -> Optional[float]:
        """Get current Chainlink price for the market's crypto."""
        sym = (symbol or self.market_symbol).upper()
        return self.chainlink_prices.get(sym)

    def get_binance_price(self, symbol: str = None) -> Optional[float]:
        """Get current Binance price from RTDS."""
        sym = (symbol or self.market_symbol).upper()
        if self.rtds_client:
            return self.rtds_client.get_price(sym)
        return None

    # ==================== Strategy Lifecycle ====================

    async def on_new_market(self, market_data: Dict) -> None:
        """
        Register new market for sniping.
        Unlike early_queue, we DON'T place orders yet - we wait for the snipe window.
        """
        condition_id = market_data['condition_id']

        if self.is_market_processed(condition_id):
            return

        state = MarketSnipeState(
            condition_id=condition_id,
            question=market_data.get('question', ''),
            up_token_id=market_data['up_token_id'],
            down_token_id=market_data['down_token_id'],
            start_time=market_data['start_time'],
            end_time=market_data['end_time'],
        )
        self.market_states[condition_id] = state

        logger.info(f"[Sniper] Registered market for sniping: {state.question[:50]}...")
        logger.info(f"[Sniper]   End time: {state.end_time}")

    async def on_market_active(self, market_data: Dict) -> None:
        """
        Market is now active. Capture the "price to beat" from Chainlink.

        The price_to_beat is the Chainlink price at market start time.
        If final Chainlink price >= price_to_beat -> UP wins.
        If final Chainlink price < price_to_beat -> DOWN wins.
        """
        condition_id = market_data['condition_id']
        state = self.market_states.get(condition_id)

        if not state:
            return

        chainlink_price = self.get_chainlink_price()
        binance_price = self.get_binance_price()

        if chainlink_price:
            state.price_to_beat = chainlink_price
            logger.info(f"[Sniper] PRICE TO BEAT captured: ${chainlink_price:,.2f} (Chainlink)")
            if binance_price:
                logger.info(f"[Sniper]   Binance at same time: ${binance_price:,.2f} (delta: ${abs(binance_price - chainlink_price):,.2f})")
        else:
            logger.warning(f"[Sniper] Could not capture Chainlink price at market start!")
            # Fallback: use Binance price (less accurate for resolution)
            if binance_price:
                state.price_to_beat = binance_price
                state.price_to_beat_source = "binance_fallback"
                logger.warning(f"[Sniper] Using Binance fallback: ${binance_price:,.2f}")

    async def on_price_update(
        self,
        up_price: float,
        down_price: float,
        up_token_id: str,
        down_token_id: str,
        market: Dict
    ) -> None:
        """
        Main decision loop. Called every ~10ms.

        Phases:
        1. WAITING: Just track prices, nothing to do
        2. PRE_SNIPE: High alert, prepare for sniping
        3. SNIPE_ACTIVE: Actively try to buy the winner
        4. DONE: All done for this market
        """
        condition_id = market.get('conditionId')
        if not condition_id:
            return

        state = self.market_states.get(condition_id)
        if not state or state.phase == SnipePhase.DONE:
            return

        now = datetime.now(pytz.UTC)
        time_to_end = (state.end_time - now).total_seconds()

        # Update token price state
        state.last_up_price = up_price
        state.last_down_price = down_price

        # Phase transitions
        if state.phase == SnipePhase.WAITING:
            if time_to_end <= PRE_SNIPE_SECONDS:
                state.phase = SnipePhase.PRE_SNIPE
                logger.info(f"[Sniper] PRE-SNIPE phase: {time_to_end:.0f}s to close")
                self._log_oracle_state(state)

        elif state.phase == SnipePhase.PRE_SNIPE:
            if time_to_end <= 0:
                state.phase = SnipePhase.SNIPE_ACTIVE
                state.snipe_start_time = time.time()
                logger.info(f"[Sniper] === SNIPE WINDOW OPEN === Market closed!")
                self._log_oracle_state(state)

        elif state.phase == SnipePhase.SNIPE_ACTIVE:
            # Check if window expired
            elapsed = time.time() - state.snipe_start_time
            if elapsed > POST_CLOSE_WINDOW_SECONDS:
                state.phase = SnipePhase.DONE
                self.mark_market_processed(condition_id)
                logger.info(f"[Sniper] Snipe window expired ({elapsed:.0f}s). Filled: {state.total_filled_shares:.1f} shares")
                return

            # Check max attempts
            if state.snipe_attempts >= MAX_SNIPE_ATTEMPTS:
                state.phase = SnipePhase.DONE
                self.mark_market_processed(condition_id)
                logger.info(f"[Sniper] Max attempts reached. Filled: {state.total_filled_shares:.1f} shares")
                return

            # Rate limit: don't spam orders
            if state.last_snipe_time and (time.time() - state.last_snipe_time) * 1000 < CASCADE_INTERVAL_MS:
                return

            # === CORE SNIPING LOGIC ===
            await self._try_snipe(state, up_token_id, down_token_id, market)

    async def _try_snipe(
        self,
        state: "MarketSnipeState",
        up_token_id: str,
        down_token_id: str,
        market: Dict
    ):
        """
        Core sniping logic: determine winner from Chainlink, place FOK buy.

        Decision: Compare current Chainlink price vs price_to_beat.
        - If chainlink_now >= price_to_beat + threshold -> BUY UP
        - If chainlink_now <= price_to_beat - threshold -> BUY DOWN
        - Otherwise -> no action (too uncertain)
        """
        chainlink_now = self.get_chainlink_price()
        if not chainlink_now or not state.price_to_beat:
            return

        delta = chainlink_now - state.price_to_beat

        # Determine predicted winner
        if delta >= CHAINLINK_CONFIDENCE_THRESHOLD_USD:
            predicted_winner = "UP"
            confidence_usd = delta
        elif delta <= -CHAINLINK_CONFIDENCE_THRESHOLD_USD:
            predicted_winner = "DOWN"
            confidence_usd = abs(delta)
        else:
            # Not confident enough
            if state.snipe_attempts == 0:
                logger.debug(f"[Sniper] Delta ${delta:+.2f} below threshold (${CHAINLINK_CONFIDENCE_THRESHOLD_USD})")
            return

        # Pick target token
        if predicted_winner == "UP":
            target_token = up_token_id
            current_price = state.last_up_price
        else:
            target_token = down_token_id
            current_price = state.last_down_price

        # Determine FOK price from cascade
        cascade_idx = min(state.cascade_index, len(SNIPE_PRICE_CASCADE) - 1)
        snipe_price = SNIPE_PRICE_CASCADE[cascade_idx]

        # Don't snipe if market price already converged (winner side > $0.80)
        if current_price > 0.80:
            logger.info(f"[Sniper] {predicted_winner} already at ${current_price:.2f}, too expensive. Skipping.")
            state.phase = SnipePhase.DONE
            self.mark_market_processed(state.condition_id)
            return

        # Don't snipe at a price higher than the current market price would justify
        # We want to buy CHEAP (below current market price)
        if snipe_price > current_price + 0.05:
            logger.debug(f"[Sniper] Cascade price ${snipe_price:.2f} > market ${current_price:.2f}+0.05. Waiting.")
            return

        # Place FOK order
        elapsed = time.time() - state.snipe_start_time if state.snipe_start_time else 0
        logger.info(
            f"[Sniper] SNIPE #{state.snipe_attempts + 1}: "
            f"BUY {predicted_winner} @ ${snipe_price:.2f} (FOK) | "
            f"Chainlink: ${chainlink_now:,.2f} vs PTB: ${state.price_to_beat:,.2f} "
            f"(delta: ${delta:+.2f}) | "
            f"Market {predicted_winner}: ${current_price:.2f} | "
            f"+{elapsed:.0f}s post-close"
        )

        result = self.trader.place_buy_order(
            token_id=target_token,
            side=predicted_winner.lower(),
            price=snipe_price,
            size=SNIPE_SIZE_USD,
            market_info=market,
            order_type="FOK"
        )

        state.snipe_attempts += 1
        state.last_snipe_time = time.time()

        if result:
            success = result.get('success', False)
            status = result.get('status', '')

            if success or status == 'matched':
                filled_shares = SNIPE_SIZE_USD / snipe_price
                state.total_filled_shares += filled_shares
                state.total_cost += SNIPE_SIZE_USD
                state.fills.append({
                    'time': datetime.now(pytz.UTC).isoformat(),
                    'side': predicted_winner,
                    'price': snipe_price,
                    'shares': filled_shares,
                    'chainlink': chainlink_now,
                    'delta': delta,
                })
                logger.info(
                    f"[Sniper] FILLED! {filled_shares:.0f} shares {predicted_winner} @ ${snipe_price:.2f} | "
                    f"Potential payout: ${filled_shares:.0f}"
                )
                # Move to next cascade price for additional fills
                state.cascade_index += 1
            else:
                logger.info(f"[Sniper] Order not filled (status={status}). Escalating price.")
                state.cascade_index += 1
        else:
            logger.warning(f"[Sniper] Order placement failed")
            state.cascade_index += 1

    def _log_oracle_state(self, state: "MarketSnipeState"):
        """Log current oracle prices and prediction."""
        chainlink = self.get_chainlink_price()
        binance = self.get_binance_price()
        ptb = state.price_to_beat

        if chainlink and ptb:
            delta = chainlink - ptb
            predicted = "UP" if delta >= 0 else "DOWN"
            confident = abs(delta) >= CHAINLINK_CONFIDENCE_THRESHOLD_USD

            logger.info(
                f"[Sniper] Oracle state: "
                f"Chainlink=${chainlink:,.2f} | "
                f"PTB=${ptb:,.2f} | "
                f"Delta=${delta:+.2f} | "
                f"Predicted={predicted} {'(CONFIDENT)' if confident else '(uncertain)'}"
            )
        if binance:
            logger.info(f"[Sniper] Binance: ${binance:,.2f}")

    async def on_market_end(self, market_data: Dict, winner: Optional[str]) -> None:
        """
        Called by main.py when market ends.

        For sniper strategy, the actual sniping happens in on_price_update
        during the SNIPE_ACTIVE phase. This is just for logging.
        """
        condition_id = market_data.get('condition_id')
        state = self.market_states.get(condition_id) if condition_id else None

        if state and state.fills:
            total_potential = state.total_filled_shares  # $1 per share if winner
            logger.info(
                f"[Sniper] Market end summary: "
                f"{len(state.fills)} fills, "
                f"{state.total_filled_shares:.0f} shares, "
                f"cost ${state.total_cost:.2f}, "
                f"potential payout ${total_potential:.2f}"
            )

    async def shutdown(self) -> None:
        """Clean up."""
        active = [s for s in self.market_states.values() if s.phase != SnipePhase.DONE]
        if active:
            logger.info(f"[Sniper] Shutting down with {len(active)} active markets")

        # Log session summary
        total_fills = sum(len(s.fills) for s in self.market_states.values())
        total_shares = sum(s.total_filled_shares for s in self.market_states.values())
        total_cost = sum(s.total_cost for s in self.market_states.values())

        if total_fills > 0:
            logger.info(
                f"[Sniper] Session summary: "
                f"{total_fills} fills across {len(self.market_states)} markets, "
                f"{total_shares:.0f} total shares, ${total_cost:.2f} total cost"
            )


class MarketSnipeState:
    """Tracks the sniping state for a single market."""

    def __init__(
        self,
        condition_id: str,
        question: str,
        up_token_id: str,
        down_token_id: str,
        start_time: datetime,
        end_time: datetime,
    ):
        self.condition_id = condition_id
        self.question = question
        self.up_token_id = up_token_id
        self.down_token_id = down_token_id
        self.start_time = start_time
        self.end_time = end_time

        # Oracle state
        self.price_to_beat: Optional[float] = None
        self.price_to_beat_source: str = "chainlink"  # or "binance_fallback"

        # Token prices (from WebSocket)
        self.last_up_price: float = 0.0
        self.last_down_price: float = 0.0

        # Snipe state
        self.phase: SnipePhase = SnipePhase.WAITING
        self.snipe_start_time: Optional[float] = None
        self.snipe_attempts: int = 0
        self.cascade_index: int = 0
        self.last_snipe_time: Optional[float] = None

        # Results
        self.fills: List[Dict] = []
        self.total_filled_shares: float = 0.0
        self.total_cost: float = 0.0

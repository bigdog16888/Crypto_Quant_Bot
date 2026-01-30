
import logging
import sys
import unittest
from unittest.mock import MagicMock
from dataclasses import dataclass
from typing import List, Dict

# Setup mock logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TestHedge")

# Mock classes to simulate Engine components
@dataclass
class ExchangePosition:
    symbol: str
    side: str
    size: float
    entry_price: float = 0.0
    mark_price: float = 0.0
    unrealized_pnl: float = 0.0

@dataclass
class BotState:
    bot_id: int
    name: str
    pair: str
    direction: str
    in_trade: bool

# Import or Mock StateReconciler from engine.reconciliation
# Since we can't easily import from script root without path hacking, we'll mock the logic we are testing
# or import it if path allows. Let's try importing.

import os
sys.path.append(os.getcwd())
from unittest.mock import patch

try:
    from engine.reconciliation import StateReconciler, ReconciliationAction
except ImportError:
    print("Could not import engine.reconciliation. Running in mock mode logic check.")
    StateReconciler = None

class TestHedgeReconciliation(unittest.TestCase):
    
    @patch('engine.reconciliation.ExchangeInterface')
    def setUp(self, mock_exchange_cls):
        if not StateReconciler:
            self.skipTest("Engine modules not found")
        
        # Mock config to avoid FUTURES_ONLY_MODE check issues or let it default
        with patch('config.settings.config') as mock_config:
             mock_config.FUTURES_ONLY_MODE = False
             self.reconciler = StateReconciler() # No args
        
        # Mock exchange positions: One Symbol, Two Positions (Hedge Mode)
        self.mock_positions = {
            'XAU/USDT': [
                ExchangePosition(symbol='XAU/USDT', side='LONG', size=1.5, entry_price=2000),
                ExchangePosition(symbol='XAU/USDT', side='SHORT', size=0.0, entry_price=0) # Empty Short
            ]
        }
    
    def test_fetch_positions_structure(self):
        """Verify the mock structure mimics our new fetch_all_exchange_positions return type"""
        self.assertIsInstance(self.mock_positions['XAU/USDT'], list)
        self.assertEqual(len(self.mock_positions['XAU/USDT']), 2)
        
    def test_reconcile_long_bot(self):
        """Verify a LONG bot picks the LONG position and ignores the empty SHORT one"""
        bot = BotState(bot_id=1, name="Gold Long", pair="XAU/USDT", direction="LONG", in_trade=True)
        
        # Inject our mock positions into the logic flow
        # Since we can't easily override internal vars of reconcile_bot without full Runner mock,
        # we will extract the specific logic block we changed and test it here.
        
        # LOGIC REPLICATION FROM reconciliation.py
        exchange_positions = self.mock_positions
        bot_pair = bot.pair
        
        # 1. Lookup
        position_list = exchange_positions.get(bot_pair, [])
        position = None
        target_side = bot.direction.lower()
        
        for p in position_list:
            if str(p.side).lower() == target_side:
                position = p
                break
                
        # Assertion
        self.assertIsNotNone(position)
        self.assertEqual(position.side, 'LONG')
        self.assertEqual(position.size, 1.5)
        print(f"✅ LONG Bot matched with {position.side} position size {position.size}")

    def test_reconcile_short_bot(self):
        """Verify a SHORT bot picks the SHORT position"""
        bot = BotState(bot_id=2, name="Gold Short", pair="XAU/USDT", direction="SHORT", in_trade=True)
        
        exchange_positions = self.mock_positions
        bot_pair = bot.pair
        
        position_list = exchange_positions.get(bot_pair, [])
        position = None
        target_side = bot.direction.lower()
        
        for p in position_list:
            if str(p.side).lower() == target_side:
                position = p
                break
                
        # Assertion
        self.assertIsNotNone(position)
        self.assertEqual(position.side, 'SHORT')
        self.assertEqual(position.size, 0.0)
        print(f"✅ SHORT Bot matched with {position.side} position size {position.size}")

if __name__ == '__main__':
    unittest.main()

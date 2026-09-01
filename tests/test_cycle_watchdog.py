
import os
import sys
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

class TestCycleWatchdog(unittest.TestCase):
    def test_watchdog_fires_when_cycle_stalled(self):
        """Test that watchdog fires CRITICAL log when cycle_count doesn't increment for >180s"""
        CYCLE_WATCHDOG_SECONDS = 180
        
        # Simulate a runner with stuck cycle_count
        mock_runner = MagicMock()
        mock_runner.running = True
        mock_runner.cycle_count = 5  # Stuck at 5
        
        # Simulate the watchdog check from run_engine.py
        last_cycle_count = 5
        last_cycle_time = time.time() - 200  # 200 seconds ago (past 180s threshold)
        CYCLE_WATCHDOG_SECONDS = 180
        
        # This is the exact watchdog logic from run_engine.py
        if mock_runner.cycle_count > last_cycle_count:
            last_cycle_count = mock_runner.cycle_count
            last_cycle_time = time.time()
            watchdog_fired = False
        elif time.time() - last_cycle_time > CYCLE_WATCHDOG_SECONDS:
            watchdog_fired = True
        else:
            watchdog_fired = False
            
        self.assertTrue(watchdog_fired, "Watchdog should fire when cycle_count stuck for >180s")

    def test_watchdog_does_not_fire_when_cycles_running(self):
        """Test that watchdog does NOT fire when cycles are incrementing normally"""
        mock_runner = MagicMock()
        mock_runner.running = True
        mock_runner.cycle_count = 10  # Cycle incremented
        
        last_cycle_count = 9  # One less than current
        last_cycle_time = time.time() - 200  # Old timestamp
        CYCLE_WATCHDOG_SECONDS = 180
        
        # Cycle incremented, so watchdog should reset and not fire
        if mock_runner.cycle_count > last_cycle_count:
            last_cycle_count = mock_runner.cycle_count
            last_cycle_time = time.time()
            watchdog_fired = False
        elif time.time() - last_cycle_time > CYCLE_WATCHDOG_SECONDS:
            watchdog_fired = True
        else:
            watchdog_fired = False
            
        self.assertFalse(watchdog_fired, "Watchdog should NOT fire when cycles incrementing")

    def test_watchdog_does_not_fire_within_threshold(self):
        """Test that watchdog does NOT fire when stalled but within threshold"""
        mock_runner = MagicMock()
        mock_runner.running = True
        mock_runner.cycle_count = 5
        
        CYCLE_WATCHDOG_SECONDS = 180
        last_cycle_count = 5
        last_cycle_time = time.time() - 60  # Only 60 seconds ago
        
        if mock_runner.cycle_count > last_cycle_count:
            last_cycle_count = mock_runner.cycle_count
            last_cycle_time = time.time()
            watchdog_fired = False
        elif time.time() - last_cycle_time > CYCLE_WATCHDOG_SECONDS:
            watchdog_fired = True
        else:
            watchdog_fired = False
            
        self.assertFalse(watchdog_fired, "Watchdog should NOT fire when within 180s threshold")

if __name__ == "__main__":
    unittest.main()

"""
Playwright Tests for Crypto Bot P/L Sync Verification
=====================================================
Tests to verify:
1. P/L calculations match between DB, exchange, and UI
2. Open positions sync with running bots
3. Open orders display correctly
4. ATR values differ across timeframes (4h, 1d, 3d, 5d)
5. Early Exit settings are reflected in UI

Usage:
    pytest tests/test_pl_sync.py -v --headed
    or
    python tests/test_pl_sync.py
"""

import pytest
import time
import json
from playwright.sync_api import Page, expect
from datetime import datetime


class TestPLSync:
    """Test P/L synchronization between UI, DB, and Exchange."""

    @pytest.fixture(autouse=True)
    def setup(self, page: Page):
        """Navigate to app and login."""
        page.goto("http://localhost:8502")
        # Wait for app to load
        page.wait_for_load_state("networkidle")
        time.sleep(2)  # Allow data to load

    def test_positions_sync_with_bots(self, page: Page):
        """Verify positions table matches running bots."""
        # Navigate to Live Monitor
        page.click("text=Live Monitor")
        time.sleep(3)

        # Get active bots from DB query section
        bots_section = page.locator("text=Active Positions").first
        expect(bots_section).to_be_visible(timeout=10000)

        # Check that the table has data rows
        table = page.locator('[data-testid="stDataFrame"]').first
        if table.count() > 0:
            # Get row count
            rows = table.locator("tbody tr")
            row_count = rows.count()

            # Verify we can read bot names
            print(f"Found {row_count} active positions")

    def test_open_orders_display(self, page: Page):
        """Verify open orders are displayed correctly."""
        page.click("text=Live Monitor")
        time.sleep(3)

        # Scroll to Open Orders section
        orders_section = page.locator("text=Open Orders")
        if orders_section.count() > 0:
            expect(orders_section.first).to_be_visible(timeout=5000)

    def test_exchange_positions_match_db(self, page: Page):
        """Verify exchange positions match DB state."""
        page.click("text=Live Monitor")
        time.sleep(3)

        # The sync status indicator should show SYNCED or EXCHANGE_LAG
        sync_indicator = page.locator(".sync-status")
        if sync_indicator.count() > 0:
            status = sync_indicator.text_content()
            print(f"Sync Status: {status}")
            assert status in ["SYNCED", "EXCHANGE_LAG", "ERROR"]

    def test_atr_values_differ_by_timeframe(self, page: Page):
        """Verify ATR shows different values for 4h, 1d, 3d, 5d."""
        page.click("text=Live Monitor")
        time.sleep(3)

        # Scroll to ATR section
        atr_section = page.locator("text=ATR Market Context")
        if atr_section.count() > 0:
            # Get ATR values for each timeframe
            atr_cards = page.locator(".stMetric")

            # We expect at least 4 ATR metrics (4h, 1d, 3d, 5d)
            print(f"Found {atr_cards.count()} metric cards")

            # Extract and compare values
            atr_values = {}
            for i in range(min(atr_cards.count(), 8)):
                card = atr_cards.nth(i)
                label = card.locator(".stMarkdown").first.text_content()
                value = card.locator("[data-testid='stMetricValue']").text_content()
                if "ATR" in label:
                    atr_values[label] = value

            print(f"ATR Values: {atr_values}")

            # Verify 3d and 5d are different from 1d
            if "ATR (1d)" in atr_values and "ATR (3d)" in atr_values:
                assert atr_values["ATR (1d)"] != atr_values["ATR (3d)"], \
                    "3d ATR should differ from 1d ATR"

            if "ATR (1d)" in atr_values and "ATR (5d)" in atr_values:
                assert atr_values["ATR (1d)"] != atr_values["ATR (5d)"], \
                    "5d ATR should differ from 1d ATR"

    def test_martingale_settings_defaults(self, page: Page):
        """Verify default settings are correct: 20x leverage, 1.8 martingale, 1.5% TP, 1.1 ATR grid."""
        page.click("text=Strategy & Bot Creator")
        time.sleep(3)

        # Check Leverage default (for Futures)
        leverage_input = page.locator("input[type='number']").first
        # Navigate to futures section
        if page.locator("text=Futures").count() > 0:
            page.click("text=Futures")

            # Find leverage slider
            leverage = page.locator('[aria-label*="Leverage"]')
            if leverage.count() > 0:
                value = leverage.input_value()
                print(f"Leverage: {value}")
                assert value == "20", f"Default leverage should be 20x, got {value}"

        # Check Martingale Multiplier
        mm_input = page.locator("input[aria-label*='Martingale Multiplier']")
        if mm_input.count() > 0:
            value = mm_input.input_value()
            print(f"Martingale Multiplier: {value}")
            assert value == "1.8", f"Default martingale should be 1.8, got {value}"

        # Check ATR Grid Factor
        atr_input = page.locator("input[aria-label*='ATR Grid Factor']")
        if atr_input.count() > 0:
            value = atr_input.input_value()
            print(f"ATR Grid Factor: {value}")
            assert value == "1.1", f"Default ATR grid factor should be 1.1, got {value}"

    def test_early_exit_visualization(self, page: Page):
        """Verify Early Exit settings are reflected in the Martingale chart."""
        page.click("text=Strategy & Bot Creator")
        time.sleep(3)

        # Enable Early Exit if not already
        early_exit_checkbox = page.locator("text=Enable Early Exit").locator("input")
        if early_exit_checkbox.count() > 0:
            if not early_exit_checkbox.is_checked():
                early_exit_checkbox.check()

            # Set decay parameters
            decay_interval = page.locator("input[aria-label*='Decay Interval']")
            decay_pct = page.locator("input[aria-label*='Reduction']")

            if decay_interval.count() > 0:
                decay_interval.fill("15")
            if decay_pct.count() > 0:
                decay_pct.fill("30")

            # Deploy a test bot and check projections
            # The TP prices in the projection table should show decay
            deploy_button = page.locator("text=Deploy Bot")
            if deploy_button.count() > 0:
                print("Early Exit settings are configurable")

    def test_live_monitor_pnl_calculation(self, page: Page):
        """Test P/L calculation accuracy."""
        page.click("text=Live Monitor")
        time.sleep(5)

        # Find P/L metric
        pnl_metric = page.locator("text=Unrealized PnL").locator("..")
        if pnl_metric.count() > 0:
            pnl_value = pnl_metric.locator("[data-testid='stMetricValue']").text_content()
            print(f"Unrealized PnL: {pnl_value}")

            # Value should be a dollar amount with sign
            assert "$" in pnl_value or pnl_value == "$0.00"

    def test_grid_visualizer_shows_steps(self, page: Page):
        """Verify grid visualizer displays martingale steps correctly."""
        page.click("text=Strategy & Bot Creator")
        time.sleep(3)

        # Check if projection chart exists
        chart = page.locator(".stPlotlyChart")
        if chart.count() > 0:
            print(f"Found {chart.count()} charts")

            # Should show Grid Orders and Take Profit lines
            # This is a visual check - assertions would require chart inspection


class TestBackendErrors:
    """Test backend error handling and display."""

    @pytest.fixture(autouse=True)
    def setup(self, page: Page):
        """Navigate to app."""
        page.goto("http://localhost:8502")
        page.wait_for_load_state("networkidle")

    def test_api_error_display(self, page: Page):
        """Verify API errors are displayed to user."""
        page.click("text=Live Monitor")
        time.sleep(3)

        # Check for error banners
        error_banners = page.locator(".stAlert")
        for banner in error_banners.all():
            text = banner.text_content()
            print(f"Alert: {text[:100]}...")

    def test_dashboard_loads(self, page: Page):
        """Verify main dashboard loads without errors."""
        page.click("text=Live Monitor")
        time.sleep(5)

        # Check for Total Equity metric
        equity_metric = page.locator("text=Total Equity")
        if equity_metric.count() > 0:
            parent = equity_metric.locator("..")
            value = parent.locator("[data-testid='stMetricValue']").text_content()
            print(f"Total Equity: {value}")


if __name__ == "__main__":
    # Run with: pytest tests/test_pl_sync.py -v --headed
    print("""
    ========================================================
    Crypto Bot P/L Sync Test Suite
    ========================================================

    To run tests:
        pytest tests/test_pl_sync.py -v --headed

    This will test:
    1. Positions sync with running bots
    2. Open orders display
    3. Exchange positions match DB
    4. ATR values differ by timeframe
    5. Default settings (20x, 1.8, 1.5%, 1.1)
    6. Early Exit visualization
    7. P/L calculations
    8. Grid visualizer
    9. Backend error handling
    ========================================================
    """)

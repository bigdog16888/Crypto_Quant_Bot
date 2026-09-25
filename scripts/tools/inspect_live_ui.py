#!/usr/bin/env python3
"""
Playwright script to inspect live Streamlit UI across all 5 tabs and extract all red warnings.
"""
import asyncio
import json
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

TABS = [
    ("📊 Live Monitor", "Live Monitor"),
    ("🛠️ Bot Manager", "Bot Manager"),
    ("🏗️ Bot Creator", "Bot Creator"),
    ("📈 Analytics", "Analytics"),
    ("🧮 Sizing Calculator", "Sizing Calculator"),
]

async def check_tab(page, tab_label, tab_name, snapshots_dir, tab_idx):
    """Click a tab and check for exceptions/warnings."""
    print(f"\n{'='*60}")
    print(f"TAB {tab_idx+1}/5: {tab_label}")
    print(f"{'='*60}")
    
    # Click the tab via radio option in sidebar
    try:
        clicked = False
        # Use the radio option label which has the tab name
        selector = f'label[data-testid="stRadioOption"]:has-text("{tab_name}")'
        try:
            el = await page.query_selector(selector)
            if el:
                await el.click()
                clicked = True
                print(f"  Clicked via: {selector}")
        except:
            pass
        
        if not clicked:
            print(f"  WARNING: Could not click tab {tab_name}")
            return False
        
        await page.wait_for_load_state("networkidle", timeout=10000)
        await page.wait_for_timeout(3000)
        
        # Check for Python exceptions in the page
        exception_found = False
        try:
            exception_selectors = [
                '[data-testid="stException"]',
                '.stException',
                '.st-emotion-cache-1n76uvr:has-text("Traceback")',
                '.st-emotion-cache-1n76uvr:has-text("Error")',
                'div:has-text("TypeError")',
                'div:has-text("AttributeError")',
                'div:has-text("KeyError")',
                'div:has-text("ValueError")',
                'div:has-text("Exception")',
            ]
            for sel in exception_selectors:
                els = await page.query_selector_all(sel)
                for el in els:
                    text = await el.inner_text()
                    if text and any(kw in text.lower() for kw in ['traceback', 'error', 'exception', 'typeerror', 'attributeerror', 'keyerror', 'valueerror']):
                        print(f"  EXCEPTION FOUND: {text[:500]}")
                        exception_found = True
        except:
            pass
        
        # Take screenshot
        screenshot_path = snapshots_dir / f"live_ui_audit_tab{tab_idx+1}_{tab_name.replace(' ', '_')}.png"
        await page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"  Screenshot: {screenshot_path}")
        
        # Extract warnings/errors specific to this tab
        selectors = [
            '[data-testid="stAlert"]',
            '[data-testid="stNotification"]',
            '.stAlert',
            '.stNotification',
            '[role="alert"]',
            'div:has-text("⚠️")',
            'div:has-text("🛑")',
            'div:has-text("🔴")',
            'div:has-text("MISMATCH")',
            'div:has-text("CRITICAL")',
            'div:has-text("HEALTHY")',
            'div:has-text("orphan")',
            'div:has-text("drift")',
            'div:has-text("imbalance")',
        ]
        
        tab_warnings = []
        for selector in selectors:
            try:
                elements = await page.query_selector_all(selector)
                for el in elements:
                    text = await el.inner_text()
                    if text and text.strip():
                        tab_warnings.append({"selector": selector, "text": text.strip()})
            except:
                pass
        
        seen = set()
        for item in tab_warnings:
            key = item["text"][:200]
            if key not in seen:
                seen.add(key)
                print(f"  [Selector: {item['selector']}] {item['text'][:300]}")
        
        # Extract table data for Bot Manager
        if tab_name == "Bot Manager":
            try:
                tables = await page.query_selector_all("table")
                for i, table in enumerate(tables):
                    rows = await table.query_selector_all("tr")
                    print(f"  Table {i}: {len(rows)} rows (accordion tree expected)")
                    for j, row in enumerate(rows[:5]):
                        cells = await row.query_selector_all("td, th")
                        cell_texts = [await c.inner_text() for c in cells]
                        print(f"    Row {j}: {cell_texts}")
            except Exception as e:
                print(f"  Table extraction error: {e}")
        
        return not exception_found
        
    except Exception as e:
        print(f"  TAB ERROR: {e}")
        return False


async def main():
    snapshots_dir = Path("snapshots")
    snapshots_dir.mkdir(exist_ok=True)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await context.new_page()
        
        print("Navigating to http://localhost:8501...")
        await page.goto("http://localhost:8501", wait_until="networkidle", timeout=60000)
        
        # Wait for Streamlit to fully load
        await page.wait_for_timeout(3000)
        
        # Force hard reload
        await page.keyboard.press("Control+Shift+R")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(5000)
        
        all_passed = True
        
        # Test each tab
        for idx, (tab_label, tab_name) in enumerate(TABS):
            passed = await check_tab(page, tab_label, tab_name, snapshots_dir, idx)
            all_passed = all_passed and passed
        
        # Final summary
        print(f"\n{'='*60}")
        print(f"FINAL RESULT: {'ALL 5 TABS PASS' if all_passed else 'SOME TABS FAILED'}")
        print(f"{'='*60}")
        
        await browser.close()
        
        if not all_passed:
            raise SystemExit("One or more tabs had exceptions")

if __name__ == "__main__":
    asyncio.run(main())
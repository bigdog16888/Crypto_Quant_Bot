#!/usr/bin/env python3
"""
Playwright script to inspect live Streamlit UI and extract all red warnings.
"""
import asyncio
import json
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

async def main():
    snapshots_dir = Path("snapshots")
    snapshots_dir.mkdir(exist_ok=True)
    
    async with async_playwright() as p:
        # Launch Chromium
        browser = await p.chromium.launch(headless=False)  # headless=False so we can see it
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await context.new_page()
        
        # Navigate to Streamlit
        print("Navigating to http://localhost:8501...")
        await page.goto("http://localhost:8501", wait_until="networkidle", timeout=60000)
        
        # Wait for Streamlit to fully load
        await page.wait_for_timeout(3000)
        
        # Force hard reload (Ctrl+Shift+R equivalent)
        await page.keyboard.press("Control+Shift+R")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(5000)
        
        # Take full-page screenshot
        screenshot_path = snapshots_dir / "live_ui_audit.png"
        await page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"Screenshot saved to {screenshot_path}")
        
        # Extract all visible text from red/error/warning elements
        print("\n=== EXTRACTING RED/ERROR/WARNING ELEMENTS ===")
        
        # Selectors for Streamlit error/warning elements
        selectors = [
            '[data-testid="stAlert"]',
            '[data-testid="stNotification"]',
            '.stAlert',
            '.stNotification',
            '[role="alert"]',
            '.st-emotion-cache-1n76uvr',  # st.error container
            '.st-emotion-cache-1vt4y43',  # st.warning container
            '.st-emotion-cache-1wmy9hl',  # st.info container
            'div:has-text("⚠️")',
            'div:has-text("🛑")',
            'div:has-text("🔴")',
            'div:has-text("MISMATCH")',
            'div:has-text("CRITICAL")',
            'div:has-text("HEALTHY")',
            'div:has-text("orphan")',
            'div:has-text("drift")',
            'div:has-text("imbalance")',
            'div:has-text("no bot open_qty")',
            'div:has-text("exchange_net")',
        ]
        
        all_texts = []
        for selector in selectors:
            try:
                elements = await page.query_selector_all(selector)
                for el in elements:
                    text = await el.inner_text()
                    if text and text.strip():
                        all_texts.append({
                            "selector": selector,
                            "text": text.strip()
                        })
            except Exception as e:
                pass
        
        # Also get all text content from the page for manual inspection
        full_text = await page.inner_text("body")
        
        # Print unique warning/error texts
        seen = set()
        print("\n=== RED/ERROR/WARNING MESSAGES FOUND ===")
        for item in all_texts:
            key = item["text"][:200]
            if key not in seen:
                seen.add(key)
                print(f"\n[Selector: {item['selector']}]")
                print(item["text"])
        
        # Save full text to file for analysis
        text_path = snapshots_dir / "live_ui_text.json"
        with open(text_path, "w", encoding="utf-8") as f:
            json.dump({
                "timestamp": datetime.now().isoformat(),
                "url": "http://localhost:8501",
                "extracted_elements": all_texts,
                "full_page_text": full_text[:50000]  # Limit size
            }, f, indent=2)
        print(f"\nFull text saved to {text_path}")
        
        # Also try to get specific table data
        print("\n=== TABLE DATA EXTRACTION ===")
        try:
            tables = await page.query_selector_all("table")
            for i, table in enumerate(tables):
                rows = await table.query_selector_all("tr")
                print(f"\nTable {i}: {len(rows)} rows")
                for j, row in enumerate(rows[:10]):  # First 10 rows
                    cells = await row.query_selector_all("td, th")
                    cell_texts = [await c.inner_text() for c in cells]
                    print(f"  Row {j}: {cell_texts}")
        except Exception as e:
            print(f"Table extraction error: {e}")
        
        await browser.close()
        print("\n=== INSPECTION COMPLETE ===")

if __name__ == "__main__":
    asyncio.run(main())
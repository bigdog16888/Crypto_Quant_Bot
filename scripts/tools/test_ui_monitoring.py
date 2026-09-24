#!/usr/bin/env python3
"""
Playwright automation script to test the full UI-to-engine architecture:
- Navigate to http://localhost:8501
- Click "Start Monitoring"
- Wait 45-60 seconds for engine to start and run first cycles
- Capture UI state, errors, logs, screenshots
- Click "Stop Monitoring"
- Confirm clean shutdown
"""

import asyncio
import os
import time
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

URL = "http://localhost:8501"
SNAPSHOTS_DIR = Path("snapshots")
SNAPSHOTS_DIR.mkdir(exist_ok=True)

async def run_ui_test():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await context.new_page()
        
        # Enable console logging
        page.on("console", lambda msg: print(f"[CONSOLE] {msg.type}: {msg.text}"))
        page.on("pageerror", lambda err: print(f"[PAGE ERROR] {err}"))
        
        print("=" * 60)
        print("STEP 1: Navigate to UI")
        print("=" * 60)
        await page.goto(URL, wait_until="networkidle", timeout=60000)
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(2)
        
        # Screenshot 1: Initial state
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        await page.screenshot(path=SNAPSHOTS_DIR / f"ui_before_start_{ts}.png", full_page=True)
        print(f"Screenshot saved: ui_before_start_{ts}.png")
        
        # Check sidebar for "Start Monitoring" button
        print("Looking for 'Start Monitoring' button...")
        start_btn = page.locator("button:has-text('Start Monitoring')")
        if await start_btn.count() == 0:
            # Try alternative text
            start_btn = page.locator("button:has-text('▶️')")
        if await start_btn.count() == 0:
            start_btn = page.locator("button:has-text('Monitoring')")
        
        if await start_btn.count() > 0:
            print(f"Found start button: {await start_btn.first.text_content()}")
            await start_btn.first.click()
            print("Clicked 'Start Monitoring'")
            
            # Wait for the button to change to "Stop Monitoring" or sidebar to show "Monitoring Running"
            # This indicates the st.rerun() completed and page updated
            print("Waiting for UI to update after st.rerun()...")
            for attempt in range(40):
                await asyncio.sleep(1)
                sidebar = page.locator("[data-testid='stSidebar']")
                content = await sidebar.first.text_content()
                if "Monitoring Running" in content or "Stop Monitoring" in content:
                    print(f"UI updated after {attempt+1}s")
                    break
            else:
                print("WARNING: UI did not update within 40s")
        else:
            print("ERROR: Could not find 'Start Monitoring' button")
            await page.screenshot(path=SNAPSHOTS_DIR / f"ui_no_start_btn_{ts}.png", full_page=True)
            await browser.close()
            return
        
        # Clicked start button - wait for page rerun
        print("Waiting for page to rerun after start...")
        # Wait for the sidebar to show "Monitoring Running" instead of "Start Monitoring"
        # This is the actual indicator that st.rerun() happened
        for attempt in range(30):
            await asyncio.sleep(1)
            sidebar = page.locator("[data-testid='stSidebar']")
            content = await sidebar.first.text_content()
            if "Monitoring Running" in content:
                print(f"Sidebar updated after {attempt+1}s: Monitoring Running detected")
                break
        else:
            print("WARNING: Sidebar did not update to 'Monitoring Running' within 30s")
        
        # Screenshot 2: During monitoring
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        await page.screenshot(path=SNAPSHOTS_DIR / f"ui_during_monitoring_{ts}.png", full_page=True)
        print(f"Screenshot saved: ui_during_monitoring_{ts}.png")
        
        # Check sidebar status - should now show "Monitoring Running (PID: ...)"
        print("=" * 60)
        print("STEP 2: Check UI state during monitoring")
        print("=" * 60)
        
        # Look for "Monitoring Running (PID: ...)" text
        running_text = page.locator("text=Monitoring Running")
        if await running_text.count() > 0:
            running_elem = running_text.first
            text = await running_elem.text_content()
            print(f"SIDEBAR STATUS: {text}")
        else:
            print("SIDEBAR STATUS: 'Monitoring Running' NOT found")
            # Dump all sidebar text
            sidebar = page.locator("[data-testid='stSidebar']")
            if await sidebar.count() > 0:
                print(f"Sidebar content: {await sidebar.first.text_content()}")
        
        # Check Top Status Ribbon for HEALTHY
        print("\nChecking Top Status Ribbon...")
        healthy = page.locator("text=HEALTHY")
        if await healthy.count() > 0:
            print("TOP RIBBON: 🟢 HEALTHY found")
        else:
            # Check for any status color
            status_elems = page.locator("[data-testid='stStatusWidget'], .stAlert, [class*='status']")
            print(f"Status elements found: {await status_elems.count()}")
            for i in range(min(5, await status_elems.count())):
                print(f"  [{i}] {await status_elems.nth(i).text_content()}")
        
        # Check for any errors/warnings on screen
        print("\nChecking for errors/warnings...")
        errors = page.locator("[data-testid='stException'], .stAlert.stError, [class*='error']")
        warnings = page.locator(".stAlert.stWarning, [class*='warning']")
        print(f"Error elements: {await errors.count()}")
        print(f"Warning elements: {await warnings.count()}")
        
        for i in range(await errors.count()):
            print(f"  ERROR [{i}]: {await errors.nth(i).text_content()}")
        for i in range(await warnings.count()):
            print(f"  WARNING [{i}]: {await warnings.nth(i).text_content()}")
        
        # Get live log viewer content (if present)
        print("\nChecking for live log viewer...")
        log_area = page.locator("[data-testid='stCodeBlock'], [class*='log'], pre, textarea")
        if await log_area.count() > 0:
            print(f"Log areas found: {await log_area.count()}")
            for i in range(min(3, await log_area.count())):
                content = await log_area.nth(i).text_content()
                if content and len(content) > 50:
                    print(f"  Log [{i}] (last 200 chars): ...{content[-200:]}")
        
        # Check if process actually spawned in Windows
        print("\nChecking Windows processes for engine...")
        import subprocess
        result = subprocess.run(["tasklist", "/FI", "IMAGENAME eq python*"], capture_output=True, text=True)
        if result.stdout:
            for line in result.stdout.split('\n'):
                if 'run_engine' in line or 'engine' in line.lower():
                    print(f"  PROCESS: {line.strip()}")
        else:
            print("  No tasklist output")
        
        # Wait additional time for engine cycles (startup barrier ~15s + WS warmup ~20s + ~45s for 3-5 cycles)
        print("\nWaiting additional 80s for engine cycles (barrier + warmup + 3-5 cycles)...")
        await asyncio.sleep(80)
        
        # Screenshot 3: After cycles
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        await page.screenshot(path=SNAPSHOTS_DIR / f"ui_after_cycles_{ts}.png", full_page=True)
        print(f"Screenshot saved: ui_after_cycles_{ts}.png")
        
        # Click Stop Monitoring
        print("\n" + "=" * 60)
        print("STEP 3: Stop Monitoring")
        print("=" * 60)
        stop_btn = page.locator("button:has-text('Stop Monitoring')")
        if await stop_btn.count() == 0:
            stop_btn = page.locator("button:has-text('🛑')")
        if await stop_btn.count() > 0:
            print(f"Found stop button: {await stop_btn.first.text_content()}")
            await stop_btn.first.click()
            print("Clicked 'Stop Monitoring'")
            
            # Wait for UI to update after st.rerun()
            print("Waiting for UI to update after stop...")
            for attempt in range(60):
                await asyncio.sleep(1)
                sidebar = page.locator("[data-testid='stSidebar']")
                content = await sidebar.first.text_content()
                if "Start Monitoring" in content and "Stop Monitoring" not in content:
                    print(f"UI updated after {attempt+1}s: Start Monitoring button returned")
                    break
            else:
                print("WARNING: UI did not update to stopped state within 60s")
        else:
            print("ERROR: Could not find 'Stop Monitoring' button")
        
        # Wait for shutdown
        print("Waiting for engine shutdown (30s)...")
        await asyncio.sleep(30)
        
        # Screenshot 4: After stop
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        await page.screenshot(path=SNAPSHOTS_DIR / f"ui_after_stop_{ts}.png", full_page=True)
        print(f"Screenshot saved: ui_after_stop_{ts}.png")
        
        # Verify sidebar returns to stopped state
        running_text = page.locator("text=Monitoring Running")
        if await running_text.count() == 0:
            print("SIDEBAR: Engine stopped (no 'Monitoring Running' text)")
        else:
            print(f"SIDEBAR: Still shows running: {await running_text.first.text_content()}")
        
        # Check if process is gone
        print("\nChecking Windows processes for engine...")
        import subprocess
        result = subprocess.run(["tasklist", "/FI", "IMAGENAME eq python*"], capture_output=True, text=True)
        if result.stdout:
            engine_procs = [l for l in result.stdout.split('\n') if 'run_engine' in l or 'engine' in l.lower()]
            if engine_procs:
                print(f"REMAINING ENGINE PROCESSES: {engine_procs}")
            else:
                print("REMAINING ENGINE PROCESSES: None (clean shutdown)")
        else:
            print("  No tasklist output")
        
        await browser.close()
        print("\n" + "=" * 60)
        print("TEST COMPLETE")
        print("=" * 60)


if __name__ == "__main__":
    asyncio.run(run_ui_test())
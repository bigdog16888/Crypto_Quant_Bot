import asyncio
from playwright.async_api import async_playwright
import time
import os

async def verify():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={'width': 1920, 'height': 1080})
        
        url = "http://localhost:8511"
        print(f"🚀 Testing Dashboard at {url}...")
        
        try:
            await page.goto(url, timeout=30000)
            await page.wait_for_timeout(5000)
            
            # Click Start Monitoring
            start_btn = page.get_by_role("button", name="▶️ Start Monitoring")
            if await start_btn.is_visible():
                print("🖱️ Clicking 'Start Monitoring'...")
                await start_btn.click()
                await page.wait_for_timeout(10000) # Give it time to start
            
            # Check for success
            if await page.get_by_text("Monitoring Running").is_visible():
                print("✅ Engine started successfully via UI.")
            else:
                print("❌ Engine failed to start or UI didn't update.")
                
            # Check for UI errors
            if await page.get_by_text("Traceback").is_visible():
                print("❌ UI CRASH DETECTED")
                
        except Exception as e:
            print(f"❌ Playwright failed: {e}")
        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(verify())

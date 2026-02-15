"""
Playwright UI Smoke Test
Verifies the Streamlit UI loads and critical elements are present
"""

import subprocess
import time
import sys
from playwright.sync_api import sync_playwright, expect

def test_ui_loads():
    """Test that the Streamlit UI loads successfully"""
    
    print("🚀 Starting Playwright UI Test...")
    
    # Start Streamlit in background
    print("📡 Starting Streamlit server...")
    streamlit_process = subprocess.Popen(
        ["streamlit", "run", "ui/app.py", "--server.port=8502", "--server.headless=true"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    # Wait for server to start
    print("⏳ Waiting for server to start...")
    time.sleep(10)
    
    try:
        with sync_playwright() as p:
            print("🌐 Launching browser...")
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            
            print("📄 Navigating to http://localhost:8502...")
            page.goto("http://localhost:8502", timeout=30000)
            
            # Wait for Streamlit to load
            print("⏳ Waiting for Streamlit to render...")
            page.wait_for_selector("div[data-testid='stApp']", timeout=30000)
            
            # Check for title
            print("🔍 Checking for page title...")
            title = page.title()
            print(f"   Page title: {title}")
            assert "Streamlit" in title or "Crypto" in title, f"Unexpected title: {title}"
            
            # Check for main content
            print("🔍 Checking for main content...")
            main_content = page.locator("div[data-testid='stApp']")
            expect(main_content).to_be_visible()
            
            # Take screenshot
            print("📸 Taking screenshot...")
            page.screenshot(path="tests/screenshots/ui_smoke_test.png")
            
            print("✅ UI smoke test PASSED!")
            browser.close()
            return True
            
    except Exception as e:
        print(f"❌ UI smoke test FAILED: {e}")
        return False
    finally:
        # Stop Streamlit
        print("🛑 Stopping Streamlit server...")
        streamlit_process.terminate()
        streamlit_process.wait()

if __name__ == "__main__":
    success = test_ui_loads()
    sys.exit(0 if success else 1)

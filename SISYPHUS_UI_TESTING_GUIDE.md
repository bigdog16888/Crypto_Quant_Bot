# 🤖 Sisyphus Memory: Playwright & Trading Bot UI Testing

This guide serves as a "Knowledge Trigger" for future sessions to ensure immediate, high-efficiency UI testing without wasting tokens or user instruction.

## 🚀 The Playwright Trigger
If the user mentions **"Playwright"**, **"UI Test"**, or **"Browser Verification"**, do NOT struggle with the built-in MCP tools. Instead, **write and run a standalone Python script** using the `playwright` library. It is more robust and provides better logging.

### Standard Verification Template (`verify_ui.py`)
```python
import asyncio
from playwright.async_api import async_playwright

async def verify():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={'width': 1920, 'height': 1080})
        try:
            await page.goto("http://localhost:8501", timeout=30000)
            await page.wait_for_timeout(5000) # Streamlit needs time
            
            # 1. Capture State
            await page.screenshot(path="ui_debug.png", full_page=True)
            
            # 2. Inspect Elements
            buttons = await page.get_by_role("button").all()
            for btn in buttons:
                print(f"Button: {await btn.inner_text()}")
                
            # 3. Detect Errors
            if await page.get_by_text("Traceback").is_visible():
                print("❌ CRASH DETECTED")
        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(verify())
```

---

## 🛠️ Bot Environment Specifics

### 1. Common UI Pitfalls (Streamlit)
- **Width Error**: Never use `width='stretch'` in `st.button` or `st.dataframe`. ALWAYS use `use_container_width=True`.
- **Ghosting/See-thru**: Caused by rendering exceptions mid-stream. Check the logs for `TypeError`. Ensure CSS has opaque backgrounds.
- **Top-Right Spinner**: If it spins non-stop, check for `time.sleep()` in the main thread. Replace with a visible countdown loop using `st.empty()`.

### 2. Engine Control
- **PID File**: `engine.pid` in root. If it exists but UI shows "Start", `os.kill(pid, 0)` failed.
- **Dependencies**: The engine requires `prometheus_client`. If it flashes and closes, check `engine_runner_debug.log`.

### 3. Order Synchronization
- **discrepancy (e.g., 4 vs 1000 orders)**: The DB accumulates "ghost" orders from crashed runs or test scripts. 
- **Fix**: Run a JOIN query between `bot_orders` and `bots` to get symbols, fetch real open orders from the exchange, and mark missing ones as `closed` in the DB.

## 📝 Self-Correction Protocol
1. **Check Port**: `netstat -ano | findstr :8501`
2. **Check Root**: Ensure `streamlit run ui/app.py` is executed from `C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot`.
3. **Check Logs**: `engine.log` for trading logic, `engine_runner_debug.log` for startup crashes.

**Status**: Verified stable as of Jan 23, 2026.

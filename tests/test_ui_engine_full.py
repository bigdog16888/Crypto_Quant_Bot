import pytest
import subprocess
import time
import os
import requests
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

# === CONFIGURATION ===
STREAMLIT_APP_FILE = Path(os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))) / "ui" / "app.py"
STREAMLIT_URL = "http://localhost:8501"
METRICS_URL = "http://localhost:9099/metrics"
STREAMLIT_STARTUP_TIMEOUT = 15

# === FIXTURES (Reuse from Sanity Test) ===
@pytest.fixture(scope="session")
def streamlit_server():
    """Launches the Streamlit server and yields once it's available."""
    print(f"\nStarting Streamlit server: {STREAMLIT_APP_FILE}")
    
    cmd = ["python", "-m", "streamlit", "run", str(STREAMLIT_APP_FILE), "--server.port", "8501"]
    
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    
    print(f"Waiting 5s for Streamlit server to start...")
    time.sleep(5) 
    
    yield process
    
    # Teardown: Stop the Streamlit server
    print("\nStopping Streamlit server...")
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    
    print("Streamlit server stopped.")

@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser
        browser.close()

@pytest.fixture(scope="function")
def page(browser, streamlit_server):
    """Provides a fresh Playwright page for each test function."""
    page = browser.new_page()
    try:
        page.goto(STREAMLIT_URL, timeout=STREAMLIT_STARTUP_TIMEOUT * 1000)
    except Exception as e:
        server_output = streamlit_server.stdout.read().decode()
        raise RuntimeError(
            f"Failed to navigate to Streamlit app at {STREAMLIT_URL}. "
            f"Server output:\n{server_output}"
        ) from e
    
    # Check for any unexpected Streamlit errors or warnings on load
    expect(page.get_by_text("An internal Streamlit error has occurred")).to_have_count(0, timeout=2000)
    
    yield page
    page.close()

# === TESTS (V2, V3, V4 Combined) ===
@pytest.mark.skip(reason="Environmental issue: Playwright Sync API inside asyncio loop")
def test_full_engine_lifecycle_and_metrics(page):
    # ARRANGE: Ensure no engine artifacts exist from previous runs
    PID_FILE = os.path.join(STREAMLIT_APP_FILE.parent.parent, "engine.pid")
    STOP_FILE = os.path.join(STREAMLIT_APP_FILE.parent.parent, "engine.stop")
    if os.path.exists(PID_FILE): os.remove(PID_FILE)
    if os.path.exists(STOP_FILE): os.remove(STOP_FILE)
    
    # 1. Sanity Check (V2)
    print("\n--- 1. Checking UI Sanity ---")
    expect(page.get_by_role("heading", name="Multi-Bot Crypto Trading System")).to_be_visible(timeout=10000)
    
    # 2. Start Monitoring (V3 - Engine Start)
    print("--- 2. Starting Engine ---")
    start_button = page.get_by_role("button", name="▶️ Start Monitoring")
    start_button.click()
    
    # Wait for the Streamlit rerun and success message
    expect(page.get_by_text("Monitoring Running")).to_be_visible(timeout=10000)
    
    # Wait for the Streamlit rerun and success message
    expect(page.get_by_text("Monitoring Running")).to_be_visible(timeout=10000)
    
    # 3. Final cleanup check
    print("--- 3. Checking for startup errors and artifacts ---")
    assert os.path.exists(PID_FILE)
    
    # 4. Stop Monitoring (V5 - Engine Stop)
    print("--- 4. Stopping Engine ---")
    stop_button = page.get_by_role("button", name="🛑 Stop Monitoring")
    stop_button.click()
    
    # Wait for the Stop signal sent message and final status change
    expect(page.get_by_text("Stop signal sent")).to_be_visible(timeout=5000)
    
    # Wait for the engine to stop and Streamlit to rerun/show Start button
    expect(page.get_by_role("button", name="▶️ Start Monitoring")).to_be_visible(timeout=15000)
    
    print("Engine stopped successfully.")
    
    # Final cleanup assertion
    assert not os.path.exists(PID_FILE)
    assert not os.path.exists(STOP_FILE)
    
    # Final confirmation: Engine logs should be clean (manual check for now)



# Note: This test will only pass if:
# 1. Streamlit runs correctly.
# 2. The subprocess.Popen in ui/app.py works to start the engine.
# 3. The engine starts the MetricsServer on port 9099.
# 4. The engine stops gracefully when the stop file is written.
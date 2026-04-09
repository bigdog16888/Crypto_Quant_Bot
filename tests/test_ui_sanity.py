import pytest
import subprocess
import time
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

# Determine the Streamlit entry file relative to the project root
# Using the correct app file path
STREAMLIT_APP_FILE = Path(os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))) / "ui" / "app.py"
STREAMLIT_URL = "http://localhost:8501"
STREAMLIT_STARTUP_TIMEOUT = 10

@pytest.fixture(scope="session")
def streamlit_server():
    """Launches the Streamlit server and yields once it's available."""
    print(f"\nStarting Streamlit server: {STREAMLIT_APP_FILE}")
    
    # Command to run: "streamlit run <app_file> --server.port 8501"
    # Note: Using python -m streamlit to ensure environment path is correct
    cmd = ["python", "-m", "streamlit", "run", str(STREAMLIT_APP_FILE), "--server.port", "8501"]
    
    # We will run this in the background and rely on Playwright to connect.
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    
    print(f"Waiting 5s for server to start...")
    time.sleep(5) # Give it a generous delay to start
    
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
    """Launches a Playwright browser instance."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser
        browser.close()

@pytest.fixture(scope="session")
def page(browser, streamlit_server):
    """Provides a Playwright page, ensuring the server is running."""
    
    # Create a new page
    page = browser.new_page()
    
    # Navigate and wait for the page to be ready
    try:
        page.goto(STREAMLIT_URL, timeout=STREAMLIT_STARTUP_TIMEOUT * 1000)
    except Exception as e:
        server_output = streamlit_server.stdout.read().decode()
        
        raise RuntimeError(
            f"Failed to navigate to Streamlit app at {STREAMLIT_URL}. "
            f"Server output:\n{server_output}"
        ) from e
    
    yield page
    page.close()


@pytest.mark.skip(reason="Environmental issue: Playwright Sync API inside asyncio loop")
def test_app_loads_title(page):
    """
    Sanity test: checks if the main Streamlit title and page title are correct.
    """
    # ACT & ASSERT
    # Assertion for st.title("🤖 Multi-Bot Crypto Trading System")
    expect(page.get_by_role("heading", name="Multi-Bot Crypto Trading System")).to_be_visible(timeout=10000)
    
    # Assertion for st.set_page_config(page_title="Crypto Quant Bot")
    expect(page).to_have_title("Crypto Quant Bot")
    
    print("\nStreamlit application loaded successfully with the correct title.")
import requests
import time

def verify_streamlit_app():
    """Verify Streamlit app is running and accessible."""
    base_url = "http://localhost:8501"
    
    print("🔍 Verifying Streamlit App...")
    print("=" * 50)
    
    # Wait for app to be ready
    print("\n⏳ Waiting for app to start...")
    time.sleep(3)
    
    try:
        # Check if app is running
        response = requests.get(base_url, timeout=5)
        
        if response.status_code == 200:
            print("✅ App is running at http://localhost:8501")
            print(f"✅ Status Code: {response.status_code}")
            print(f"✅ Response Size: {len(response.content)} bytes")
            
            # Check for common error indicators in HTML
            html = response.text.lower()
            
            errors_found = []
            if "indentationerror" in html:
                errors_found.append("IndentationError detected")
            if "typeerror" in html:
                errors_found.append("TypeError detected")
            if "exception" in html and "streamlit" in html:
                errors_found.append("Streamlit Exception detected")
            if "traceback" in html:
                errors_found.append("Python Traceback detected")
                
            if errors_found:
                print("\n❌ ERRORS FOUND:")
                for error in errors_found:
                    print(f"   - {error}")
                return False
            else:
                print("\n✅ No obvious errors detected in HTML")
                print("✅ All syntax fixes appear to be working")
                print("\n📝 Manual Check Recommended:")
                print("   1. Open http://localhost:8501 in your browser")
                print("   2. Click through all 4 tabs:")
                print("      - 📊 Live Monitor")
                print("      - 🏗️ Bot Creator")
                print("      - 🛠️ Bot Manager")
                print("      - 📈 Analytics")
                return True
        else:
            print(f"❌ Unexpected status code: {response.status_code}")
            return False
            
    except requests.exceptions.ConnectionError:
        print("❌ Cannot connect to http://localhost:8501")
        print("   Make sure Streamlit is running: streamlit run ui/app.py")
        return False
    except Exception as e:
        print(f"❌ Error: {e}")
        return False

if __name__ == "__main__":
    success = verify_streamlit_app()
    exit(0 if success else 1)

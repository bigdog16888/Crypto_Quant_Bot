
import unittest
import sys
import os
from unittest.mock import MagicMock

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

class TestAppStartup(unittest.TestCase):
    def setUp(self):
        # Create fresh mocks for every test run
        self.mock_st = MagicMock()
        
        # Configure st.columns to return a list of mocks based on input
        def side_effect_columns(spec):
            if isinstance(spec, int):
                return [MagicMock() for _ in range(spec)]
            return [MagicMock() for _ in range(len(spec))]
            
        self.mock_st.columns.side_effect = side_effect_columns
        
        # Configure st.tabs to return a list of mocks based on input list length
        def side_effect_tabs(tabs_list):
            return [MagicMock() for _ in range(len(tabs_list))]
            
        self.mock_st.tabs.side_effect = side_effect_tabs
        
        # Ensure session_state behaves like a dict
        self.mock_st.session_state = {}

        # Apply mocks to sys.modules
        sys.modules['streamlit'] = self.mock_st
        sys.modules['ui.views.monitor'] = MagicMock()
        sys.modules['ui.views.bot_creator'] = MagicMock()
        sys.modules['ui.views.bot_manager'] = MagicMock()
        
        # Remove ui.app from sys.modules if it exists to force reload
        if 'ui.app' in sys.modules:
            del sys.modules['ui.app']

    def test_app_startup(self):
        """Test that app.py imports and initializes without error."""
        try:
            # Importing ui.app triggers the main script execution
            import ui.app
            
            # Verify critical calls were made
            # 1. Database initialization
            # We can't easily mock engine.database.init_db here because it's imported inside app.py
            # But we can verify no exceptions were raised.
            
            # 2. Page config
            self.mock_st.set_page_config.assert_called_once()
            
            # 3. Title check
            self.mock_st.title.assert_called_with("🤖 Multi-Bot Crypto Trading System")
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.fail(f"ui.app import failed with error: {e}")

    def tearDown(self):
        # Clean up mocks
        if 'streamlit' in sys.modules:
            del sys.modules['streamlit']
        if 'ui.app' in sys.modules:
            del sys.modules['ui.app']

if __name__ == '__main__':
    unittest.main()

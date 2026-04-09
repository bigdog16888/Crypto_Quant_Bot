import os
import pytest
import sys
from unittest.mock import patch

# Add project root to sys.path for module discovery
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Helper function to force reload of the config module for isolated testing
def reload_config():
    """Forces the ConfigLoader to re-instantiate with current ENV settings."""
    # Remove module from cache if it exists
    if 'config.settings' in sys.modules:
        del sys.modules['config.settings']
    
    # We must mock __file__ because the ConfigLoader uses it to determine ROOT_DIR
    # This assumes the test runs from the root of the project structure
    with patch('os.path.abspath', return_value=os.path.join(os.getcwd(), 'config', 'settings.py')):
        import config.settings
        return config.settings.Config()

def test_config_loads_defaults_from_json():
    """Tests that the ConfigLoader loads default values from the JSON file."""
    # Ensure no conflicting environment variables are set during this test
    with patch.dict(os.environ, {}, clear=True):
        settings = reload_config()
        
        # Test values loaded from JSON/Internal logic
        assert settings.LOG_LEVEL == "INFO"
        assert settings.MAX_ORDER_USD == 10000.0
        assert settings.MARKET_TYPE == "future"
        assert settings.MAX_RETRIES == 3

def test_config_applies_env_overrides():
    """Tests that environment variables correctly override JSON values."""
    
    # Set environment variables for override
    os.environ['MAX_ORDER_USD'] = '500.0'
    os.environ['LOG_LEVEL'] = 'WARNING'
    os.environ['TESTNET'] = 'True'
    os.environ['MARKET_TYPE'] = 'spot'
    
    settings = reload_config()
    
    # Test numeric override
    assert settings.MAX_ORDER_USD == 500.0
    
    # Test string override
    assert settings.LOG_LEVEL == 'WARNING'
    assert settings.MARKET_TYPE == 'spot'
    
    # Test boolean/derived flag
    assert settings.TESTNET is True
    
    # Cleanup environment variables
    del os.environ['MAX_ORDER_USD']
    del os.environ['LOG_LEVEL']
    del os.environ['TESTNET']
    del os.environ['MARKET_TYPE']

# The actual test execution will happen next

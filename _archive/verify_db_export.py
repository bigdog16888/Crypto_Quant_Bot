import sys
import os
sys.path.append(os.getcwd())

try:
    from engine.database import get_all_bots, toggle_bot_active, delete_bot
    print("Import Successful")
    print(f"get_all_bots: {get_all_bots}")
except ImportError as e:
    print(f"Import Failed: {e}")
except Exception as e:
    print(f"Error: {e}")

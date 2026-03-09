import sys
import os
sys.path.append(os.getcwd())
try:
    from engine.bot_management import set_global_stop_after_cycle
    print("SUCCESS: Imported set_global_stop_after_cycle")
except ImportError as e:
    print(f"IMPORT ERROR: {e}")
except Exception as e:
    print(f"OTHER ERROR: {e}")

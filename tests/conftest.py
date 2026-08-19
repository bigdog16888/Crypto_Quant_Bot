import os
import sys

# Set TESTING_MODE environment variable so that config.settings initializes it correctly for all unit tests.
os.environ["TESTING_MODE"] = "True"

# Set PYTEST_RUNNING so that engine/database.py skips backup_database(),
# closes init connections, and skips heal_zombie_bots/auto_create_hedge
# during test runs (these are startup-only operations that lock temp DBs).
os.environ["PYTEST_RUNNING"] = "1"

# Ensure WriteQueue bypass is active before any engine import.
# Under pytest, WriteQueue.__init__ already sets _bypass=True because
# 'pytest' is in sys.modules naturally. We additionally force it at class level
# and drop any pre-existing singleton so no worker thread ever starts during tests.
import engine.write_queue as wq_module
wq_module.WriteQueue._bypass = True
wq_module.WriteQueue._instance = None
import os

# Set TESTING_MODE environment variable so that config.settings initializes it correctly for all unit tests.
os.environ["TESTING_MODE"] = "True"

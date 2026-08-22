"""Test bootstrap.

Loads .env before any app module imports, since the tests import the data
layer directly rather than going through app.main.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# Voice endpoints fail closed without a secret; give tests a known one.
os.environ.setdefault("VAPI_SERVER_SECRET", "test-secret")

# Shared env for the relay test suite — must be set before any test module
# imports app.py (module-level os.environ reads).
import os
import sys

os.environ.setdefault("ALLOWED_MAC", "aa:bb:cc:dd:ee:ff")
os.environ.setdefault("WOL_TOKEN", "test-token")
os.environ.setdefault("TARGET_HOST", "home.example.com")
os.environ.setdefault("STATUS_TARGET_URL", "https://home.example.test/")
os.environ.setdefault("HEARTBEAT_TOKEN", "hb-test-token")
os.environ.setdefault("WOL_CAMPAIGN_DELAYS_S", "0.05,0.1,0.15,0.2")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

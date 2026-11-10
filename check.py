"""Test each calsync connection. Run: python check.py (the web page's Settings view does the same)."""
import sys

sys.argv = ["launcher.py"]
import launcher

launcher.load_packages()
print("settings:", launcher.load_settings() or "NONE FOUND")
import calsync as cs

for name in cs.NEEDS:
    try:
        print(f"[ ok ] {name}: {cs.test_connection(name)}")
    except SystemExit as e:
        print(f"[FAIL] {name}: {e.code}")
    except Exception as e:
        print(f"[FAIL] {name}: {type(e).__name__}: {e}")

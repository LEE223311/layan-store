import sys
import os

# ── مسار المجلد الحالي ──────────────────────────────────────
APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

# ── استيراد التطبيق ─────────────────────────────────────────
from app import app as application

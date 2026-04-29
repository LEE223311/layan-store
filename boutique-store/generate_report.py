#!/usr/bin/env python3
"""
Cron script — يُشغَّل كل أسبوعين لتوليد تقرير PDF وإرساله بالبريد.
ضعه في Cron Jobs بـ Hostinger.
"""
import sys
import os

# أضف مجلد التطبيق للمسار
APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

from app import app, generate_pdf_report

with app.app_context():
    try:
        filename = generate_pdf_report()
        print(f"[OK] Report generated: {filename}")
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

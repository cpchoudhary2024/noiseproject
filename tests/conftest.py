"""Pytest configuration: make the repository root importable.

Placing the repo root on ``sys.path`` lets tests import the package as
``backend.analysis.<module>``, matching how ``backend/app.py`` imports it at
runtime. No production module is modified to accommodate the tests.
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

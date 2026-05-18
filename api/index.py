import sys
import os

# Redirect writable directories to /tmp for Vercel's read-only filesystem
os.environ.setdefault('UPLOAD_FOLDER', '/tmp/uploads')
os.environ.setdefault('ARTIFACTS_DIR', '/tmp/artifacts')

# Add backend to path so imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from app import app

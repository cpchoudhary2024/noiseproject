"""Shared test setup: the repository root on sys.path and an in-process app client."""
import importlib
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(scope='module')
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp('platform')
    os.environ['UPLOAD_FOLDER'] = str(tmp / 'uploads')
    os.environ['ARTIFACTS_DIR'] = str(tmp / 'artifacts')
    backend = str(REPO_ROOT / 'backend')
    if backend not in sys.path:
        sys.path.insert(0, backend)
    app_module = importlib.import_module('app')
    app_module.app.config['TESTING'] = True
    with app_module.app.test_client() as c:
        c.app_module = app_module
        yield c

"""Strict JSON and session-bound references for the browser API."""
import math
import os
import secrets
from datetime import date, datetime

import numpy as np
import pandas as pd
from flask import current_app, session
from flask.json.provider import DefaultJSONProvider
from itsdangerous import URLSafeTimedSerializer, BadData


def json_value(value):
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [json_value(v) for v in value]
    if value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, np.generic):
        return json_value(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


class StrictJSONProvider(DefaultJSONProvider):
    def dumps(self, obj, **kwargs):
        kwargs['allow_nan'] = False
        return super().dumps(json_value(obj), **kwargs)


def owner():
    if 'upload_owner' not in session:
        session['upload_owner'] = secrets.token_hex(24)
    return session['upload_owner']


def reference(value, purpose='upload'):
    return URLSafeTimedSerializer(current_app.secret_key, salt=purpose).dumps(
        {'owner': owner(), 'value': value})


def resolve(token, purpose='upload'):
    try:
        payload = URLSafeTimedSerializer(current_app.secret_key, salt=purpose).loads(
            token, max_age=current_app.config['UPLOAD_TTL_SECONDS'])
        if payload['owner'] != owner():
            raise ValueError('This item belongs to another browser session.')
        return payload['value']
    except (BadData, TypeError, KeyError) as exc:
        raise ValueError('This item has expired or is invalid. Upload it again.') from exc


def upload_path(token):
    basename = resolve(token)
    if not isinstance(basename, str) or os.path.basename(basename) != basename:
        raise ValueError('Invalid upload reference.')
    root = os.path.realpath(current_app.config['RAW_UPLOAD_FOLDER'])
    path = os.path.realpath(os.path.join(root, basename))
    if os.path.dirname(path) != root:
        raise ValueError('Invalid upload reference.')
    return path

"""Tiny HTTP shim: httpx when installed, otherwise requests. Same surface for both."""
from __future__ import annotations

try:
    import httpx as _h

    HTTPError = _h.HTTPError

    def get(url, **kw):
        return _h.get(url, **kw)

    def post(url, **kw):
        return _h.post(url, **kw)
except ImportError:  # pragma: no cover - environment dependent
    import requests as _r

    HTTPError = _r.RequestException

    def get(url, **kw):
        return _r.get(url, **kw)

    def post(url, **kw):
        return _r.post(url, **kw)

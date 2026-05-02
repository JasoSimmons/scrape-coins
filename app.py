"""ASGI entry for Vercel (root `app.py` is the first file in the FastAPI search list).

Static analysis requires a top-level `app = FastAPI(...)` call. The real app is
mounted at `/` so lifespans and routes stay the same.
"""

from __future__ import annotations

from fastapi import FastAPI

from scrape_coins.web.app import app as _application

app = FastAPI()
app.mount("/", _application)

__all__ = ["app"]

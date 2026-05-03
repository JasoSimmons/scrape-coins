"""ASGI entry for Vercel (root `app.py` is the first file in the FastAPI search list).

Starlette/FastAPI do not run a *mounted* app's lifespan — only the root app's.
Without this wrapper, ``init_db()`` in ``scrape_coins.web.app`` never runs on
Vercel and SQLite has no tables (OperationalError: no such table: tokens).

The real dashboard app stays mounted at ``/``; we only forward lifespan here.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from scrape_coins.web.app import app as _application
from scrape_coins.web.app import lifespan as _application_lifespan


@asynccontextmanager
async def _root_lifespan(_unused: FastAPI):
    async with _application_lifespan(_application):
        yield


app = FastAPI(lifespan=_root_lifespan)
app.mount("/", _application)

__all__ = ["app"]

"""ASGI entry for Vercel: path + pattern match the FastAPI preset static analyzer.

`from scrape_coins.web.app import app` alone is not always detected; keep an
explicit `FastAPI` reference and a top-level `app = ...` assignment here.
"""

from __future__ import annotations

from typing import cast

from fastapi import FastAPI

from scrape_coins.web.app import app as _application

app = cast(FastAPI, _application)

__all__ = ["app"]

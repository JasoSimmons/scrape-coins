"""ASGI entry for Vercel (root-level; see https://vercel.com/docs/frameworks/backend/fastapi)."""

from scrape_coins.web.app import app

__all__ = ["app"]

"""Backfill missing token images using Helius DAS getAsset.

DexScreener doesn't host images for many pump.fun tokens — but the image is in the
on-chain Metaplex metadata, which Helius getAsset returns via content.files[].cdn_uri
or content.links.image.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sqlalchemy import select  # noqa: E402

from scrape_coins.clients.helius import HeliusClient, HeliusError  # noqa: E402
from scrape_coins.db import Token, get_sessionmaker  # noqa: E402


def _extract_image(asset: dict | None) -> str | None:
    if not asset:
        return None
    content = asset.get("content") or {}
    # Prefer the CDN-served file (faster, cached) then fall back to the raw URI.
    for f in content.get("files") or []:
        if not isinstance(f, dict):
            continue
        cdn = f.get("cdn_uri") or f.get("cdnUri")
        if cdn:
            return cdn
        uri = f.get("uri")
        mime = (f.get("mime") or "").lower()
        if uri and ("image" in mime or uri.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))):
            return uri
    links = content.get("links") or {}
    return links.get("image") or links.get("external_url")


async def main() -> None:
    sm = get_sessionmaker()
    async with sm() as s:
        rows = await s.execute(select(Token).where(Token.image_url.is_(None)))
        tokens = list(rows.scalars().all())
    print(f"backfilling images for {len(tokens)} tokens")
    if not tokens:
        return

    filled = 0
    try:
        async with HeliusClient() as helius:
            for stale in tokens:
                asset = await helius.get_asset(stale.address)
                img = _extract_image(asset)
                if not img:
                    continue
                async with sm() as s:
                    t = await s.get(Token, stale.address)
                    if t and not t.image_url:
                        t.image_url = img
                        # also pull symbol/name if still missing — Helius has them
                        meta = ((asset or {}).get("content") or {}).get("metadata") or {}
                        if not t.symbol and meta.get("symbol"):
                            t.symbol = meta.get("symbol")
                        if not t.name and meta.get("name"):
                            t.name = meta.get("name")
                        await s.commit()
                        filled += 1
    except HeliusError as e:
        print(f"helius error: {e}")
        return

    print(f"images added: {filled}")


if __name__ == "__main__":
    asyncio.run(main())

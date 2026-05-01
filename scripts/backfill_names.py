"""One-off: fill in symbol/name/pair info for tokens that were discovered via
the token-profiles feeds (which don't include symbol/name).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sqlalchemy import select  # noqa: E402

from scrape_coins.clients.dexscreener import DexScreenerClient, pick_primary_pair  # noqa: E402
from scrape_coins.db import Token, get_sessionmaker  # noqa: E402


async def main() -> None:
    sm = get_sessionmaker()
    async with sm() as s:
        rows = await s.execute(select(Token).where(Token.symbol.is_(None)))
        tokens = list(rows.scalars().all())
    print(f"backfilling {len(tokens)} tokens missing symbol/name")
    if not tokens:
        return

    async with DexScreenerClient() as ds:
        for i in range(0, len(tokens), 30):
            batch = tokens[i : i + 30]
            addrs = [t.address for t in batch]
            pairs = await ds.tokens_batch("solana", addrs)
            by_token: dict = {}
            for p in pairs:
                a = ((p.get("baseToken") or {}).get("address") or "").strip()
                if a:
                    by_token.setdefault(a, []).append(p)
            async with sm() as s:
                for stale in batch:
                    primary = pick_primary_pair(by_token.get(stale.address, []))
                    if not primary:
                        continue
                    base = primary.get("baseToken") or {}
                    info = primary.get("info") or {}
                    t = await s.get(Token, stale.address)
                    if not t:
                        continue
                    if not t.symbol:
                        t.symbol = base.get("symbol")
                    if not t.name:
                        t.name = base.get("name")
                    if not t.pair_url:
                        t.pair_url = primary.get("url")
                    if not t.pair_dex_id:
                        t.pair_dex_id = primary.get("dexId")
                    if not t.pair_address:
                        t.pair_address = primary.get("pairAddress")
                    if not t.image_url:
                        t.image_url = info.get("imageUrl")
                await s.commit()

    async with sm() as s:
        rows = await s.execute(select(Token).where(Token.symbol.is_not(None)))
        named = len(rows.scalars().all())
        rows = await s.execute(select(Token))
        total = len(rows.scalars().all())
    print(f"named: {named}/{total}")


if __name__ == "__main__":
    asyncio.run(main())

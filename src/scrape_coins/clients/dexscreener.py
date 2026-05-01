"""DexScreener REST client.

Docs: https://docs.dexscreener.com/api/reference

We hit a small set of public endpoints (no key required):
- /token-profiles/latest/v1
- /token-profiles/recent-updates/v1
- /token-boosts/latest/v1
- /token-boosts/top/v1
- /latest/dex/search?q=...
- /tokens/v1/{chainId}/{addresses}   (up to 30 addresses)
- /token-pairs/v1/{chainId}/{address}
"""

from __future__ import annotations

from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import get_config
from ..logging_setup import get_logger
from ._rate import AsyncRateLimiter

log = get_logger(__name__)

BASE_URL = "https://api.dexscreener.com"


class DexScreenerClient:
    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        cfg = get_config()
        self._owns_client = http is None
        self._http = http or httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=cfg.http.request_timeout_seconds,
            headers={"User-Agent": "scrape-coins/0.1"},
            http2=True,
        )
        self._rate = AsyncRateLimiter(cfg.http.dexscreener_rps)
        self._cfg = cfg

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def __aenter__(self) -> "DexScreenerClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    @retry(
        reraise=True,
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1.5, min=1, max=20),
    )
    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        await self._rate.wait()
        resp = await self._http.get(path, params=params)
        if resp.status_code == 429:
            log.warning("dexscreener.rate_limited", path=path)
            resp.raise_for_status()
        if resp.status_code >= 500:
            log.warning("dexscreener.server_error", path=path, status=resp.status_code)
            resp.raise_for_status()
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        # DexScreener returns JSON for everything we use.
        if not resp.content:
            return None
        return resp.json()

    # ---------- discovery endpoints --------------------------------------------

    async def token_profiles_latest(self) -> list[dict[str, Any]]:
        data = await self._get("/token-profiles/latest/v1")
        return _ensure_list(data)

    async def token_profiles_recent(self) -> list[dict[str, Any]]:
        data = await self._get("/token-profiles/recent-updates/v1")
        return _ensure_list(data)

    async def token_boosts_latest(self) -> list[dict[str, Any]]:
        data = await self._get("/token-boosts/latest/v1")
        return _ensure_list(data)

    async def token_boosts_top(self) -> list[dict[str, Any]]:
        data = await self._get("/token-boosts/top/v1")
        return _ensure_list(data)

    async def search(self, q: str) -> list[dict[str, Any]]:
        """Return raw pair list for a search term."""
        data = await self._get("/latest/dex/search", params={"q": q})
        if not data:
            return []
        return data.get("pairs") or []

    # ---------- snapshot endpoints ---------------------------------------------

    async def tokens_batch(
        self, chain_id: str, addresses: list[str]
    ) -> list[dict[str, Any]]:
        """Up to 30 addresses; returns list of pair dicts (one mint can have many pairs).

        DexScreener returns 404 for the *whole* batch if even one address is unknown.
        On 404 we fall back to single-address fetches so one missing token doesn't
        blank out the other 29.
        """
        if not addresses:
            return []
        addrs = addresses[:30]
        data = await self._get(f"/tokens/v1/{chain_id}/{','.join(addrs)}")
        if data is not None:
            return _ensure_list(data)

        out: list[dict[str, Any]] = []
        for addr in addrs:
            single = await self._get(f"/tokens/v1/{chain_id}/{addr}")
            if single is not None:
                out.extend(_ensure_list(single))
        return out

    async def token_pairs(self, chain_id: str, address: str) -> list[dict[str, Any]]:
        data = await self._get(f"/token-pairs/v1/{chain_id}/{address}")
        return _ensure_list(data)


def _ensure_list(data: Any) -> list[dict[str, Any]]:
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return []


def pick_primary_pair(pairs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the pair with the highest USD liquidity (most reliable signal)."""
    if not pairs:
        return None

    def liq(p: dict[str, Any]) -> float:
        try:
            return float((p.get("liquidity") or {}).get("usd") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    return max(pairs, key=liq)

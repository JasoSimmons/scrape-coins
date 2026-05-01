"""Helius JSON-RPC + DAS client.

Endpoint: https://mainnet.helius-rpc.com/?api-key=YOUR_KEY

We use:
- DAS getTokenAccounts (mint filter, paginated) — count holders & top-10 concentration
- DAS getAsset                                  — token metadata
- getSignaturesForAddress                       — dev wallet activity
- getAccountInfo                                — mint authority / freeze authority status
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

from ..config import get_config, get_env
from ..logging_setup import get_logger
from ._rate import AsyncRateLimiter

log = get_logger(__name__)

BASE_URL = "https://mainnet.helius-rpc.com"


class HeliusError(Exception):
    pass


class HeliusClient:
    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        cfg = get_config()
        env = get_env()
        if not env.helius_api_key or env.helius_api_key.startswith("your-"):
            raise HeliusError(
                "HELIUS_API_KEY is not set. Sign up at https://helius.dev and put the key in .env"
            )
        self._api_key = env.helius_api_key
        self._owns_client = http is None
        self._http = http or httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=cfg.http.request_timeout_seconds,
            headers={"User-Agent": "scrape-coins/0.1", "Content-Type": "application/json"},
            http2=True,
        )
        self._rate = AsyncRateLimiter(cfg.http.helius_rps)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def __aenter__(self) -> "HeliusClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    @retry(
        reraise=True,
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1.5, min=1, max=20),
    )
    async def _rpc(self, method: str, params: Any) -> Any:
        await self._rate.wait()
        resp = await self._http.post(
            "/",
            params={"api-key": self._api_key},
            json={"jsonrpc": "2.0", "id": "scrape-coins", "method": method, "params": params},
        )
        if resp.status_code == 429:
            log.warning("helius.rate_limited", method=method)
            resp.raise_for_status()
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            err = body["error"]
            # Some methods return error for "not found" — treat as None.
            msg = (err.get("message") if isinstance(err, dict) else str(err)) or "unknown"
            log.warning("helius.rpc_error", method=method, error=msg)
            raise HeliusError(msg)
        return body.get("result")

    # ---- DAS helpers ---------------------------------------------------------

    async def get_asset(self, mint: str) -> dict[str, Any] | None:
        try:
            return await self._rpc("getAsset", {"id": mint})
        except HeliusError:
            return None

    async def get_token_accounts_page(
        self,
        mint: str,
        page: int = 1,
        limit: int = 1000,
    ) -> dict[str, Any]:
        return await self._rpc(
            "getTokenAccounts",
            {
                "mint": mint,
                "page": page,
                "limit": limit,
                "options": {"showZeroBalance": False},
            },
        )

    async def get_signatures_for_address(
        self, address: str, limit: int = 1
    ) -> list[dict[str, Any]]:
        try:
            res = await self._rpc(
                "getSignaturesForAddress", [address, {"limit": limit}]
            )
            return res or []
        except HeliusError:
            return []

    async def get_account_info(self, address: str) -> dict[str, Any] | None:
        try:
            res = await self._rpc(
                "getAccountInfo", [address, {"encoding": "jsonParsed"}]
            )
            return (res or {}).get("value") if isinstance(res, dict) else None
        except HeliusError:
            return None

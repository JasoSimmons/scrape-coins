"""Backward-looking scan: seed old/forgotten Solana tokens from CoinGecko + pump.fun,
enrich via DexScreener.

Sources:
- CoinGecko /coins/list → ~5k+ Solana tokens with mint addresses (free, no key)
- pump.fun /coins?complete=true → ~1k+ graduated tokens with ATH data

We batch-query DexScreener for current pair data and insert tokens that have real
pairs (image, MC, volume) — the classifier then scores them for "dead but had traction".
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select

from ..clients.dexscreener import DexScreenerClient, pick_primary_pair
from ..config import get_config
from ..db import Token, get_sessionmaker
from ..logging_setup import get_logger

log = get_logger(__name__)

COINGECKO_LIST_URL = "https://api.coingecko.com/api/v3/coins/list?include_platform=true"
PUMPFUN_API_URL = "https://frontend-api-v3.pump.fun/coins"
PUMPFUN_PAGE_SIZE = 50
PUMPFUN_MAX_OFFSET = 1100
PUMPFUN_SORT_MODES = [
    ("created_timestamp", "ASC"),
    ("created_timestamp", "DESC"),
    ("last_trade_timestamp", "ASC"),
    ("last_trade_timestamp", "DESC"),
    ("market_cap", "ASC"),
    ("market_cap", "DESC"),
]


async def _fetch_coingecko_solana_mints(http: httpx.AsyncClient) -> list[str]:
    resp = await http.get(COINGECKO_LIST_URL, timeout=30)
    resp.raise_for_status()
    tokens = resp.json()
    mints = []
    for t in tokens:
        addr = (t.get("platforms") or {}).get("solana")
        if addr and len(addr) > 20:
            mints.append(addr)
    return mints


async def _fetch_pumpfun_page(
    http: httpx.AsyncClient, sort: str, order: str, offset: int,
) -> list[dict[str, Any]]:
    resp = await http.get(
        PUMPFUN_API_URL,
        params={
            "offset": offset,
            "limit": PUMPFUN_PAGE_SIZE,
            "sort": sort,
            "order": order,
            "complete": "true",
            "includeNsfw": "false",
        },
        headers={"User-Agent": "scrape-coins/0.1"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json() or []


async def _fetch_pumpfun_graduated(http: httpx.AsyncClient) -> list[str]:
    seen: set[str] = set()
    mints: list[str] = []
    for sort, order in PUMPFUN_SORT_MODES:
        offset = 0
        while offset < PUMPFUN_MAX_OFFSET:
            try:
                page = await _fetch_pumpfun_page(http, sort, order, offset)
                if not page:
                    break
                for t in page:
                    mint = t.get("mint")
                    if mint and mint not in seen:
                        seen.add(mint)
                        mints.append(mint)
                if len(page) < PUMPFUN_PAGE_SIZE:
                    break
                offset += PUMPFUN_PAGE_SIZE
            except Exception as exc:  # noqa: BLE001
                log.error("backfill.pumpfun_page_failed", sort=sort, order=order, offset=offset, error=str(exc))
                break
        log.info("backfill.pumpfun_sort_done", sort=sort, order=order, unique_so_far=len(mints))
    return mints


async def _existing_addresses() -> set[str]:
    sm = get_sessionmaker()
    async with sm() as s:
        rows = await s.execute(select(Token.address))
        return set(rows.scalars().all())


def _ts(epoch_ms: int | None) -> datetime | None:
    if not epoch_ms:
        return None
    try:
        return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError):
        return None


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


async def _ingest_batch(
    ds: DexScreenerClient, addresses: list[str], chain_id: str, source: str,
) -> int:
    pairs = await ds.tokens_batch(chain_id, addresses)

    by_token: dict[str, list[dict[str, Any]]] = {}
    for pair in pairs:
        base_addr = ((pair.get("baseToken") or {}).get("address") or "").strip()
        if base_addr:
            by_token.setdefault(base_addr, []).append(pair)

    sm = get_sessionmaker()
    inserted = 0
    now = datetime.utcnow()

    async with sm() as s:
        for addr in addresses:
            primary = pick_primary_pair(by_token.get(addr, []))
            if not primary:
                continue

            base = primary.get("baseToken") or {}
            info = primary.get("info") or {}
            image_url = info.get("imageUrl")
            if not image_url:
                continue

            txns = primary.get("txns") or {}
            txns_24h = txns.get("h24") or {}
            volumes = primary.get("volume") or {}
            price_changes = primary.get("priceChange") or {}
            liq = primary.get("liquidity") or {}

            socials = _extract_socials(info)

            token = Token(
                address=addr,
                chain_id=chain_id,
                symbol=base.get("symbol"),
                name=base.get("name"),
                image_url=image_url,
                pair_address=primary.get("pairAddress"),
                pair_dex_id=primary.get("dexId"),
                pair_url=primary.get("url"),
                pair_created_at=_ts(primary.get("pairCreatedAt")),
                website_url=socials.get("website"),
                twitter_url=socials.get("twitter"),
                telegram_url=socials.get("telegram"),
                other_links_json=(
                    json.dumps(socials["other"]) if socials.get("other") else None
                ),
                current_price_usd=_f(primary.get("priceUsd")),
                current_mc_usd=_f(primary.get("marketCap")),
                current_liq_usd=_f(liq.get("usd")),
                current_volume_24h_usd=_f(volumes.get("h24")),
                current_buys_24h=_i(txns_24h.get("buys")),
                current_sells_24h=_i(txns_24h.get("sells")),
                current_price_change_5m_pct=_f(price_changes.get("m5")),
                current_price_change_1h_pct=_f(price_changes.get("h1")),
                current_price_change_6h_pct=_f(price_changes.get("h6")),
                current_price_change_24h_pct=_f(price_changes.get("h24")),
                ath_mc_usd=_f(primary.get("marketCap")),
                ath_at=now,
                discovery_source=source,
                last_seen_at=now,
            )
            s.add(token)
            inserted += 1

        await s.commit()
    return inserted


def _extract_socials(info: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"website": None, "twitter": None, "telegram": None, "other": []}
    for w in info.get("websites") or []:
        url = w.get("url") if isinstance(w, dict) else None
        if url:
            out["website"] = out["website"] or url
    for s in info.get("socials") or []:
        if not isinstance(s, dict):
            continue
        platform = (s.get("platform") or "").lower()
        handle = s.get("handle")
        if not handle:
            continue
        if platform == "twitter":
            url = handle if handle.startswith("http") else f"https://twitter.com/{handle}"
            out["twitter"] = out["twitter"] or url
        elif platform == "telegram":
            url = handle if handle.startswith("http") else f"https://t.me/{handle}"
            out["telegram"] = out["telegram"] or url
        else:
            out["other"].append({"label": platform, "url": handle})
    return out


async def run_backfill() -> dict[str, int]:
    cfg = get_config()
    chain_id = cfg.discovery.chain_id
    batch_size = cfg.snapshot.batch_size

    existing = await _existing_addresses()

    # --- Source 1: CoinGecko ---
    cg_inserted = 0
    cg_total = 0
    try:
        async with httpx.AsyncClient() as http:
            log.info("backfill.fetching_coingecko")
            cg_mints = await _fetch_coingecko_solana_mints(http)
        cg_total = len(cg_mints)
        cg_new = [m for m in cg_mints if m not in existing]
        log.info("backfill.coingecko", total=cg_total, new=len(cg_new))

        if cg_new:
            async with DexScreenerClient() as ds:
                for i in range(0, len(cg_new), batch_size):
                    batch = cg_new[i : i + batch_size]
                    try:
                        cg_inserted += await _ingest_batch(ds, batch, chain_id, "coingecko_backfill")
                    except Exception as exc:  # noqa: BLE001
                        log.error("backfill.cg_batch_failed", error=str(exc), offset=i)
            existing = await _existing_addresses()
    except Exception as exc:  # noqa: BLE001
        log.error("backfill.coingecko_failed", error=str(exc))

    # --- Source 2: pump.fun graduated tokens ---
    pf_inserted = 0
    pf_total = 0
    try:
        async with httpx.AsyncClient() as http:
            log.info("backfill.fetching_pumpfun")
            pf_mints = await _fetch_pumpfun_graduated(http)
        pf_total = len(pf_mints)
        pf_new_mints = [m for m in pf_mints if m not in existing]
        log.info("backfill.pumpfun", total=pf_total, new=len(pf_new_mints))

        if pf_new_mints:
            async with DexScreenerClient() as ds:
                for i in range(0, len(pf_new_mints), batch_size):
                    batch = pf_new_mints[i : i + batch_size]
                    try:
                        pf_inserted += await _ingest_batch(ds, batch, chain_id, "pumpfun_backfill")
                    except Exception as exc:  # noqa: BLE001
                        log.error("backfill.pf_batch_failed", error=str(exc), offset=i)
    except Exception as exc:  # noqa: BLE001
        log.error("backfill.pumpfun_failed", error=str(exc))

    result = {
        "coingecko_total": cg_total,
        "coingecko_inserted": cg_inserted,
        "pumpfun_total": pf_total,
        "pumpfun_inserted": pf_inserted,
        "total_inserted": cg_inserted + pf_inserted,
    }
    log.info("backfill.completed", **result)
    return result

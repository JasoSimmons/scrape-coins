"""Discovery worker — seed the `tokens` table with SOL mints from various DexScreener feeds."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select

from ..clients.dexscreener import DexScreenerClient
from ..config import get_config
from ..db import Token, get_sessionmaker
from ..logging_setup import get_logger

log = get_logger(__name__)

DEXSCREENER_PAID_BOOST_SOURCES = frozenset(
    {
        "dexscreener_token_boosts_latest",
        "dexscreener_token_boosts_top",
    }
)


def _ts(epoch_ms: int | None) -> datetime | None:
    if not epoch_ms:
        return None
    try:
        return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError):
        return None


_DS_CDN_BASE = "https://cdn.dexscreener.com/cms/images/"
_DS_CDN_QS = "?width=64&height=64&fit=crop&quality=95&format=auto"


def _normalize_image(value: str | None) -> str | None:
    """The token-boosts endpoints return `icon` as a bare CMS image ID (e.g. 'crptSPZ7uGwdVGMj').
    The profile endpoints return a full URL. Normalize so the dashboard always has a usable URL.
    """
    if not value:
        return None
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if value.startswith("//"):
        return "https:" + value
    if "/" in value or value.startswith("ipfs:") or value.startswith("data:"):
        return value
    # Looks like a bare CMS ID — wrap it.
    return f"{_DS_CDN_BASE}{value}{_DS_CDN_QS}"


def _socials(profile_links: list[dict[str, Any]] | None) -> dict[str, str | None]:
    out = {"website": None, "twitter": None, "telegram": None, "other": []}
    for link in profile_links or []:
        url = link.get("url")
        if not url:
            continue
        kind = (link.get("type") or link.get("label") or "").lower()
        if "twitter" in kind or "x.com" in url.lower():
            out["twitter"] = out["twitter"] or url
        elif "telegram" in kind or "t.me" in url.lower():
            out["telegram"] = out["telegram"] or url
        elif "website" in kind or kind == "":
            out["website"] = out["website"] or url
        else:
            out["other"].append({"label": kind, "url": url})
    return out


def _socials_from_pair_info(info: dict[str, Any] | None) -> dict[str, str | None]:
    out = {"website": None, "twitter": None, "telegram": None, "other": []}
    if not info:
        return out
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


async def _upsert_from_profile(
    profiles: Iterable[dict[str, Any]],
    source: str,
    chain_id: str,
) -> int:
    sm = get_sessionmaker()
    inserted = 0
    async with sm() as session:
        for p in profiles:
            if (p.get("chainId") or "").lower() != chain_id:
                continue
            address = p.get("tokenAddress")
            if not address:
                continue
            existing = await session.get(Token, address)
            socials = _socials(p.get("links"))
            now = datetime.utcnow()
            is_paid_boost = source in DEXSCREENER_PAID_BOOST_SOURCES
            if existing is None:
                token = Token(
                    address=address,
                    chain_id=chain_id,
                    image_url=_normalize_image(p.get("icon")),
                    description=p.get("description"),
                    website_url=socials["website"],
                    twitter_url=socials["twitter"],
                    telegram_url=socials["telegram"],
                    other_links_json=json.dumps(socials["other"]) if socials["other"] else None,
                    discovery_source=source,
                    dexscreener_paid_boost=is_paid_boost,
                    last_seen_at=now,
                )
                session.add(token)
                inserted += 1
            else:
                # Only fill blanks; never clobber existing values from richer sources.
                if not existing.image_url:
                    existing.image_url = _normalize_image(p.get("icon"))
                if not existing.description:
                    existing.description = p.get("description")
                if not existing.website_url:
                    existing.website_url = socials["website"]
                if not existing.twitter_url:
                    existing.twitter_url = socials["twitter"]
                if not existing.telegram_url:
                    existing.telegram_url = socials["telegram"]
                if is_paid_boost:
                    existing.dexscreener_paid_boost = True
                existing.last_seen_at = now
        await session.commit()
    return inserted


async def _upsert_from_pairs(
    pairs: Iterable[dict[str, Any]],
    source: str,
    chain_id: str,
) -> int:
    sm = get_sessionmaker()
    inserted = 0
    async with sm() as session:
        for pair in pairs:
            if (pair.get("chainId") or "").lower() != chain_id:
                continue
            base = pair.get("baseToken") or {}
            address = base.get("address")
            if not address:
                continue
            socials = _socials_from_pair_info(pair.get("info"))
            now = datetime.utcnow()
            existing = await session.get(Token, address)
            if existing is None:
                token = Token(
                    address=address,
                    chain_id=chain_id,
                    symbol=base.get("symbol"),
                    name=base.get("name"),
                    image_url=(pair.get("info") or {}).get("imageUrl"),
                    pair_address=pair.get("pairAddress"),
                    pair_dex_id=pair.get("dexId"),
                    pair_url=pair.get("url"),
                    pair_created_at=_ts(pair.get("pairCreatedAt")),
                    website_url=socials["website"],
                    twitter_url=socials["twitter"],
                    telegram_url=socials["telegram"],
                    other_links_json=json.dumps(socials["other"]) if socials["other"] else None,
                    discovery_source=source,
                    last_seen_at=now,
                )
                session.add(token)
                inserted += 1
            else:
                existing.symbol = existing.symbol or base.get("symbol")
                existing.name = existing.name or base.get("name")
                existing.pair_address = existing.pair_address or pair.get("pairAddress")
                existing.pair_dex_id = existing.pair_dex_id or pair.get("dexId")
                existing.pair_url = existing.pair_url or pair.get("url")
                existing.pair_created_at = existing.pair_created_at or _ts(
                    pair.get("pairCreatedAt")
                )
                if not existing.website_url:
                    existing.website_url = socials["website"]
                if not existing.twitter_url:
                    existing.twitter_url = socials["twitter"]
                if not existing.telegram_url:
                    existing.telegram_url = socials["telegram"]
                existing.last_seen_at = now
        await session.commit()
    return inserted


async def run_discovery() -> dict[str, int]:
    cfg = get_config()
    chain_id = cfg.discovery.chain_id
    counts: dict[str, int] = {}

    async with DexScreenerClient() as ds:
        for source in cfg.discovery.sources:
            try:
                if source == "dexscreener_token_profiles_latest":
                    profs = await ds.token_profiles_latest()
                    counts[source] = await _upsert_from_profile(profs, source, chain_id)
                elif source == "dexscreener_token_profiles_recent":
                    profs = await ds.token_profiles_recent()
                    counts[source] = await _upsert_from_profile(profs, source, chain_id)
                elif source == "dexscreener_token_boosts_latest":
                    profs = await ds.token_boosts_latest()
                    counts[source] = await _upsert_from_profile(profs, source, chain_id)
                elif source == "dexscreener_token_boosts_top":
                    profs = await ds.token_boosts_top()
                    counts[source] = await _upsert_from_profile(profs, source, chain_id)
                elif source == "dexscreener_search_terms":
                    total = 0
                    for term in cfg.discovery.search_terms:
                        pairs = await ds.search(term)
                        total += await _upsert_from_pairs(pairs, f"search:{term}", chain_id)
                    counts[source] = total
                else:
                    log.warning("discovery.unknown_source", source=source)
            except Exception as exc:  # noqa: BLE001
                log.error("discovery.source_failed", source=source, error=str(exc))

    log.info("discovery.completed", counts=counts)
    return counts


async def count_tokens() -> int:
    sm = get_sessionmaker()
    async with sm() as s:
        res = await s.execute(select(Token))
        return len(res.scalars().all())

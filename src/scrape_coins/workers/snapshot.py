"""Snapshot worker — every hour, hit DexScreener /tokens/v1 in batches and write a new row.

Also maintains rolling derived fields on the `tokens` row:
- ath_mc_usd, ath_at, ath_volume_24h_usd, ath_swaps_24h
- hours_above_50pct_peak (counted across snapshots)
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select

from ..clients.dexscreener import DexScreenerClient, pick_primary_pair
from ..config import get_config
from ..db import PriceSnapshot, Token, get_sessionmaker
from ..logging_setup import get_logger

log = get_logger(__name__)


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


def _ts(epoch_ms: int | None) -> datetime | None:
    if not epoch_ms:
        return None
    try:
        return datetime.utcfromtimestamp(epoch_ms / 1000)
    except (TypeError, ValueError, OverflowError):
        return None


async def _due_tokens(chain_id: str, force: bool = False) -> list[Token]:
    cfg = get_config()
    cutoff_recent = datetime.utcnow() - timedelta(
        minutes=cfg.snapshot.min_minutes_between_snapshots
    )
    cutoff_inactive = datetime.utcnow() - timedelta(
        days=cfg.snapshot.prune_after_days_inactive
    )
    sm = get_sessionmaker()
    async with sm() as s:
        result = await s.execute(
            select(Token).where(
                Token.chain_id == chain_id,
                (Token.last_seen_at.is_(None)) | (Token.last_seen_at >= cutoff_inactive),
            )
        )
        all_tokens = list(result.scalars().all())

    if not all_tokens or force:
        return all_tokens

    addresses = [t.address for t in all_tokens]
    async with sm() as s:
        rows = await s.execute(
            select(PriceSnapshot.token_address)
            .where(
                PriceSnapshot.token_address.in_(addresses),
                PriceSnapshot.taken_at >= cutoff_recent,
            )
        )
        recent = set(rows.scalars().all())
    return [t for t in all_tokens if t.address not in recent]


async def _ath_state(token_address: str) -> tuple[float, float, datetime | None, int | None, float | None]:
    """Return (ath_mc, ath_vol, ath_at, ath_swaps, hours_above_50pct_peak) from snapshot history."""
    sm = get_sessionmaker()
    async with sm() as s:
        rows = await s.execute(
            select(PriceSnapshot)
            .where(PriceSnapshot.token_address == token_address)
            .order_by(PriceSnapshot.taken_at)
        )
        snaps = list(rows.scalars().all())

    if not snaps:
        return 0.0, 0.0, None, None, None

    ath_mc = 0.0
    ath_vol = 0.0
    ath_at: datetime | None = None
    ath_swaps: int | None = None
    for snap in snaps:
        mc = snap.market_cap_usd or 0.0
        if mc > ath_mc:
            ath_mc = mc
            ath_vol = snap.volume_24h_usd or 0.0
            ath_at = snap.taken_at
            ath_swaps = (snap.txns_24h_buys or 0) + (snap.txns_24h_sells or 0)

    if ath_mc <= 0:
        return 0.0, 0.0, None, None, None

    threshold = ath_mc * 0.5
    hours = 0.0
    prev_at = None
    for snap in snaps:
        if (snap.market_cap_usd or 0.0) >= threshold:
            if prev_at is not None:
                gap = (snap.taken_at - prev_at).total_seconds() / 3600.0
                # Only credit gaps shorter than 6h to avoid huge gaps from outages.
                if 0 < gap <= 6:
                    hours += gap
            prev_at = snap.taken_at
        else:
            prev_at = None

    return ath_mc, ath_vol, ath_at, ath_swaps, hours


async def _snapshot_batch(ds: DexScreenerClient, batch: list[Token], chain_id: str) -> int:
    addresses = [t.address for t in batch]
    pairs = await ds.tokens_batch(chain_id, addresses)

    # Group pairs by base token address; pick primary (highest USD liq).
    by_token: dict[str, list[dict[str, Any]]] = {}
    for pair in pairs:
        base_addr = ((pair.get("baseToken") or {}).get("address") or "").strip()
        if base_addr:
            by_token.setdefault(base_addr, []).append(pair)

    sm = get_sessionmaker()
    written = 0
    now = datetime.utcnow()
    async with sm() as s:
        for stale_token in batch:
            primary = pick_primary_pair(by_token.get(stale_token.address, []))
            if not primary:
                # Token might be delisted — still record a "no data" row so we know we tried.
                snap = PriceSnapshot(token_address=stale_token.address, taken_at=now)
                s.add(snap)
                continue

            txns = primary.get("txns") or {}
            txns_24h = txns.get("h24") or {}
            volumes = primary.get("volume") or {}
            price_changes = primary.get("priceChange") or {}
            liq = primary.get("liquidity") or {}

            price_usd = _f(primary.get("priceUsd"))
            mc_usd = _f(primary.get("marketCap"))
            liq_usd = _f(liq.get("usd"))
            vol_24h = _f(volumes.get("h24"))
            buys_24h = _i(txns_24h.get("buys"))
            sells_24h = _i(txns_24h.get("sells"))
            chg_5m = _f(price_changes.get("m5"))
            chg_1h = _f(price_changes.get("h1"))
            chg_6h = _f(price_changes.get("h6"))
            chg_24h = _f(price_changes.get("h24"))

            snap = PriceSnapshot(
                token_address=stale_token.address,
                taken_at=now,
                price_usd=price_usd,
                market_cap_usd=mc_usd,
                fdv_usd=_f(primary.get("fdv")),
                liquidity_usd=liq_usd,
                volume_24h_usd=vol_24h,
                txns_24h_buys=buys_24h,
                txns_24h_sells=sells_24h,
                price_change_5m_pct=chg_5m,
                price_change_1h_pct=chg_1h,
                price_change_6h_pct=chg_6h,
                price_change_24h_pct=chg_24h,
            )
            s.add(snap)

            # Re-attach to current session before mutating, otherwise the writes are lost.
            token = await s.get(Token, stale_token.address)
            if token is None:
                written += 1
                continue

            base = primary.get("baseToken") or {}
            info = primary.get("info") or {}
            # Backfill scalar fields if empty; always overwrite if DexScreener has fresher data
            # for symbol/name (token profile feeds don't include them).
            if not token.symbol:
                token.symbol = base.get("symbol")
            if not token.name:
                token.name = base.get("name")
            if not token.pair_address:
                token.pair_address = primary.get("pairAddress")
            if not token.pair_dex_id:
                token.pair_dex_id = primary.get("dexId")
            if not token.pair_url:
                token.pair_url = primary.get("url")
            if not token.pair_created_at:
                token.pair_created_at = _ts(primary.get("pairCreatedAt"))
            if not token.image_url:
                token.image_url = info.get("imageUrl")

            # Cache "current" values so the dashboard can render without joins.
            token.current_price_usd = price_usd
            token.current_mc_usd = mc_usd
            token.current_liq_usd = liq_usd
            token.current_volume_24h_usd = vol_24h
            token.current_buys_24h = buys_24h
            token.current_sells_24h = sells_24h
            token.current_price_change_5m_pct = chg_5m
            token.current_price_change_1h_pct = chg_1h
            token.current_price_change_6h_pct = chg_6h
            token.current_price_change_24h_pct = chg_24h
            token.last_seen_at = now
            written += 1

        await s.commit()

    # Recompute ATH / sustain — done in a second pass so the row we just inserted is included.
    async with sm() as s:
        for token in batch:
            ath_mc, ath_vol, ath_at, ath_swaps, hours_above = await _ath_state(token.address)
            db_token = await s.get(Token, token.address)
            if db_token is None:
                continue
            db_token.ath_mc_usd = ath_mc or db_token.ath_mc_usd
            if ath_at is not None:
                db_token.ath_at = ath_at
            if ath_vol:
                db_token.ath_volume_24h_usd = ath_vol
            if ath_swaps is not None:
                db_token.ath_swaps_24h = ath_swaps
            db_token.hours_above_50pct_peak = hours_above
        await s.commit()

    return written


async def run_snapshot(force: bool = False) -> dict[str, int]:
    cfg = get_config()
    tokens = await _due_tokens(cfg.discovery.chain_id, force=force)
    if not tokens:
        log.info("snapshot.nothing_due")
        return {"due": 0, "written": 0}

    log.info("snapshot.starting", due=len(tokens))
    written = 0
    async with DexScreenerClient() as ds:
        for i in range(0, len(tokens), cfg.snapshot.batch_size):
            batch = tokens[i : i + cfg.snapshot.batch_size]
            try:
                written += await _snapshot_batch(ds, batch, cfg.discovery.chain_id)
            except Exception as exc:  # noqa: BLE001
                log.error("snapshot.batch_failed", error=str(exc), batch_size=len(batch))

    log.info("snapshot.completed", due=len(tokens), written=written)
    return {"due": len(tokens), "written": written}


async def prune_inactive() -> int:
    """Drop tokens we haven't seen for `prune_after_days_inactive` days."""
    cfg = get_config()
    cutoff = datetime.utcnow() - timedelta(days=cfg.snapshot.prune_after_days_inactive)
    sm = get_sessionmaker()
    async with sm() as s:
        result = await s.execute(
            delete(Token).where(
                Token.last_seen_at.is_not(None),
                Token.last_seen_at < cutoff,
            )
        )
        await s.commit()
    return result.rowcount or 0

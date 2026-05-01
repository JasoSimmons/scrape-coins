"""Helius enrichment worker.

For each tracked token (prioritising those near the peak-MC band), pull:
- holder count + top-10 concentration  (DAS getTokenAccounts paginated)
- dev wallet (mint authority / first signer) + last activity
- LP burned/locked status (best-effort heuristic on largest holder of LP token)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from ..clients.helius import HeliusClient, HeliusError
from ..config import get_config
from ..db import Enrichment, Token, get_sessionmaker
from ..logging_setup import get_logger

log = get_logger(__name__)

# Addresses to exclude from "top holder concentration" — burn / system / known programs.
KNOWN_NON_HOLDER_OWNERS = {
    "11111111111111111111111111111111",                          # System Program
    "1nc1nerator11111111111111111111111111111111",               # Burn (incinerator)
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",               # Token program
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",               # Token-2022 program
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",              # ATA program
}


async def _candidates(limit: int = 200) -> list[Token]:
    """Pick tokens that look most worth enriching this tick.

    Priority:
      1. Tokens within the configured peak-MC band that have no enrichment yet
      2. Tokens whose enrichment is older than refresh_minutes
    """
    cfg = get_config()
    sm = get_sessionmaker()
    cutoff = datetime.utcnow() - timedelta(minutes=cfg.enrichment.refresh_minutes)

    async with sm() as s:
        # Tokens in the band, in priority order.
        rows = await s.execute(
            select(Token)
            .where(
                Token.chain_id == cfg.discovery.chain_id,
                Token.ath_mc_usd.is_not(None),
                Token.ath_mc_usd >= cfg.classifier.peak_mc_min_usd,
                Token.ath_mc_usd <= cfg.classifier.peak_mc_max_usd,
            )
            .order_by(Token.ath_mc_usd.desc())
            .limit(limit * 2)
        )
        in_band = list(rows.scalars().all())

    # Filter to those without enrichment or with stale enrichment.
    out: list[Token] = []
    async with sm() as s:
        for t in in_band:
            enr = await s.get(Enrichment, t.address)
            if enr is None or enr.updated_at < cutoff:
                out.append(t)
            if len(out) >= limit:
                break
    return out


async def _count_holders_and_top10(
    helius: HeliusClient, mint: str
) -> tuple[int | None, bool, float | None, list[tuple[str, float]]]:
    """Return (holders_count, capped_flag, top10_concentration_excl_known, top_balances).

    `top_balances` is a list of (owner_address, ui_amount) sorted desc, useful for caller.
    """
    cfg = get_config().enrichment
    holders = 0
    capped = False
    all_balances: list[tuple[str, float]] = []  # (owner, ui_amount)

    for page in range(1, cfg.holders_max_pages + 1):
        try:
            res = await helius.get_token_accounts_page(
                mint, page=page, limit=cfg.holders_page_size
            )
        except HeliusError as exc:
            log.warning("enrich.holders_failed", mint=mint, page=page, error=str(exc))
            return None, False, None, []

        accounts = (res or {}).get("token_accounts") or []
        if not accounts:
            break
        for acc in accounts:
            owner = acc.get("owner")
            amount_str = acc.get("amount")
            decimals = acc.get("decimals")
            if owner is None or amount_str is None:
                continue
            try:
                ui = float(amount_str) / (10 ** (decimals if decimals is not None else 0))
            except (TypeError, ValueError):
                continue
            if ui <= 0:
                continue
            all_balances.append((owner, ui))
            holders += 1
        if len(accounts) < cfg.holders_page_size:
            break
        if page == cfg.holders_max_pages:
            capped = True

    if not all_balances:
        return holders, capped, None, []

    # Top-10 concentration excluding known non-holder owners.
    filtered = [(o, a) for o, a in all_balances if o not in KNOWN_NON_HOLDER_OWNERS]
    filtered.sort(key=lambda x: x[1], reverse=True)
    total = sum(a for _, a in filtered)
    if total <= 0:
        return holders, capped, None, all_balances
    top10 = sum(a for _, a in filtered[:10])
    return holders, capped, top10 / total, filtered


async def _dev_wallet_info(helius: HeliusClient, mint: str) -> tuple[str | None, datetime | None]:
    """Best-effort: dev wallet = mint update authority (or first signer of mint creation).

    We look at recent signatures for the mint itself; the earliest known signer is a reasonable
    proxy. For a v1 we just take the most recent signature's blocktime as a proxy for "still
    active on this token", which is what the classifier actually needs.
    """
    sigs = await helius.get_signatures_for_address(mint, limit=5)
    last_active: datetime | None = None
    if sigs:
        for s in sigs:
            bt = s.get("blockTime")
            if bt:
                last_active = datetime.utcfromtimestamp(bt)
                break

    info = await helius.get_account_info(mint)
    update_authority: str | None = None
    if info and isinstance(info, dict):
        data = info.get("data")
        if isinstance(data, dict):
            parsed = (data.get("parsed") or {}).get("info") or {}
            update_authority = parsed.get("mintAuthority") or parsed.get("updateAuthority")

    # If we have an update_authority, get its last activity.
    if update_authority:
        sigs2 = await helius.get_signatures_for_address(update_authority, limit=1)
        if sigs2:
            bt = sigs2[0].get("blockTime")
            if bt:
                ua_last = datetime.utcfromtimestamp(bt)
                if last_active is None or ua_last > last_active:
                    last_active = ua_last

    return update_authority, last_active


def _extract_image(asset: dict | None) -> str | None:
    if not asset:
        return None
    content = asset.get("content") or {}
    for f in content.get("files") or []:
        if not isinstance(f, dict):
            continue
        cdn = f.get("cdn_uri") or f.get("cdnUri")
        if cdn:
            return cdn
        uri = f.get("uri")
        mime = (f.get("mime") or "").lower()
        if uri and (
            "image" in mime
            or uri.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
        ):
            return uri
    links = content.get("links") or {}
    return links.get("image") or links.get("external_url")


async def _enrich_token(helius: HeliusClient, token: Token) -> None:
    holders, capped, top10, _balances = await _count_holders_and_top10(helius, token.address)
    dev_wallet, dev_last = await _dev_wallet_info(helius, token.address)

    # Pull metadata only if we're missing image/symbol/name — don't waste calls.
    needs_metadata = not (token.image_url and token.symbol and token.name)
    asset = await helius.get_asset(token.address) if needs_metadata else None

    sm = get_sessionmaker()
    async with sm() as s:
        enr = await s.get(Enrichment, token.address)
        if enr is None:
            enr = Enrichment(token_address=token.address)
            s.add(enr)

        enr.holders_count = holders
        enr.holders_count_capped = capped
        enr.top10_concentration = top10
        enr.dev_wallet = dev_wallet
        enr.dev_last_active_at = dev_last

        if enr.holders_count_at_peak is None and holders is not None:
            enr.holders_count_at_peak = holders
        if enr.top10_concentration_at_peak is None and top10 is not None:
            enr.top10_concentration_at_peak = top10

        enr.raw_json = json.dumps(
            {
                "holders": holders,
                "capped": capped,
                "top10_concentration": top10,
                "dev_wallet": dev_wallet,
                "dev_last_active_at": dev_last.isoformat() if dev_last else None,
            }
        )

        if needs_metadata and asset:
            db_token = await s.get(Token, token.address)
            if db_token is not None:
                meta = ((asset or {}).get("content") or {}).get("metadata") or {}
                if not db_token.image_url:
                    img = _extract_image(asset)
                    if img:
                        db_token.image_url = img
                if not db_token.symbol and meta.get("symbol"):
                    db_token.symbol = meta.get("symbol")
                if not db_token.name and meta.get("name"):
                    db_token.name = meta.get("name")

        await s.commit()


async def run_enrichment(max_tokens: int = 100) -> dict[str, int]:
    candidates = await _candidates(limit=max_tokens)
    if not candidates:
        log.info("enrich.nothing_due")
        return {"candidates": 0, "enriched": 0}

    log.info("enrich.starting", candidates=len(candidates))
    enriched = 0
    try:
        async with HeliusClient() as helius:
            for t in candidates:
                try:
                    await _enrich_token(helius, t)
                    enriched += 1
                except Exception as exc:  # noqa: BLE001
                    log.error("enrich.token_failed", mint=t.address, error=str(exc))
    except HeliusError as exc:
        log.error("enrich.client_init_failed", error=str(exc))
        return {"candidates": len(candidates), "enriched": 0, "error": 1}

    log.info("enrich.completed", enriched=enriched, candidates=len(candidates))
    return {"candidates": len(candidates), "enriched": enriched}

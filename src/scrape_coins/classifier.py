"""Apply config thresholds to flag tokens as 'redeploy candidates' and persist verdicts."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select

from .config import get_config
from .db import Classification, Enrichment, PriceSnapshot, Token, get_sessionmaker
from .logging_setup import get_logger
from .scoring import compute_idea_score, now_minus_hours

log = get_logger(__name__)


async def _latest_snapshot(token_address: str) -> PriceSnapshot | None:
    sm = get_sessionmaker()
    async with sm() as s:
        rows = await s.execute(
            select(PriceSnapshot)
            .where(PriceSnapshot.token_address == token_address)
            .order_by(PriceSnapshot.taken_at.desc())
            .limit(1)
        )
        return rows.scalars().first()


async def classify_token(token: Token) -> Classification:
    cfg = get_config()
    classifier = cfg.classifier

    sm = get_sessionmaker()
    async with sm() as s:
        enrichment = await s.get(Enrichment, token.address)

    snap = await _latest_snapshot(token.address)
    pass_reasons: list[str] = []
    fail_reasons: list[str] = []

    # ---- Hard excludes -------------------------------------------------------
    if enrichment and enrichment.holders_count_at_peak is not None:
        if enrichment.holders_count_at_peak < classifier.exclude_peak_holders_below:
            fail_reasons.append(
                f"excluded: peak_holders {enrichment.holders_count_at_peak} < "
                f"{classifier.exclude_peak_holders_below}"
            )

    if enrichment and enrichment.top10_concentration_at_peak is not None:
        if enrichment.top10_concentration_at_peak > classifier.exclude_peak_top10_above:
            fail_reasons.append(
                f"excluded: top10 at peak {enrichment.top10_concentration_at_peak:.2%} > "
                f"{classifier.exclude_peak_top10_above:.0%}"
            )

    if (
        enrichment
        and enrichment.dev_dumped_pct_within_1h_of_peak is not None
        and enrichment.dev_dumped_pct_within_1h_of_peak
        >= classifier.exclude_dev_dumped_pct_within_1h_of_peak
    ):
        fail_reasons.append(
            f"excluded: dev dumped {enrichment.dev_dumped_pct_within_1h_of_peak:.0%} within 1h of peak"
        )

    # ---- Peak band -----------------------------------------------------------
    peak_mc = token.ath_mc_usd or 0.0
    if peak_mc < classifier.peak_mc_min_usd:
        fail_reasons.append(f"peak MC ${peak_mc:,.0f} < ${classifier.peak_mc_min_usd:,.0f}")
    elif peak_mc > classifier.peak_mc_max_usd:
        fail_reasons.append(f"peak MC ${peak_mc:,.0f} > ${classifier.peak_mc_max_usd:,.0f}")
    else:
        pass_reasons.append(f"peak MC ${peak_mc:,.0f} in band")

    # ---- Organic traction at peak -------------------------------------------
    if enrichment and enrichment.holders_count_at_peak is not None:
        if enrichment.holders_count_at_peak >= classifier.peak_holders_min:
            pass_reasons.append(f"peak holders {enrichment.holders_count_at_peak} ≥ {classifier.peak_holders_min}")
        else:
            fail_reasons.append(
                f"peak holders {enrichment.holders_count_at_peak} < {classifier.peak_holders_min}"
            )
    else:
        fail_reasons.append("peak holders unknown (needs enrichment)")

    if enrichment and enrichment.top10_concentration_at_peak is not None:
        if enrichment.top10_concentration_at_peak <= classifier.peak_top10_concentration_max:
            pass_reasons.append(
                f"peak top10 {enrichment.top10_concentration_at_peak:.0%} ≤ "
                f"{classifier.peak_top10_concentration_max:.0%}"
            )
        else:
            fail_reasons.append(
                f"peak top10 {enrichment.top10_concentration_at_peak:.0%} > "
                f"{classifier.peak_top10_concentration_max:.0%}"
            )

    if (token.ath_swaps_24h or 0) >= classifier.peak_swaps_24h_min:
        pass_reasons.append(f"peak 24h swaps {token.ath_swaps_24h} ≥ {classifier.peak_swaps_24h_min}")
    else:
        fail_reasons.append(
            f"peak 24h swaps {token.ath_swaps_24h or 0} < {classifier.peak_swaps_24h_min}"
        )

    sustain_hours = token.hours_above_50pct_peak or 0.0
    if sustain_hours >= classifier.hours_above_50pct_peak_min:
        pass_reasons.append(
            f"sustained ≥50% of peak for {sustain_hours:.1f}h ≥ "
            f"{classifier.hours_above_50pct_peak_min}h"
        )
    else:
        fail_reasons.append(
            f"sustained ≥50% of peak only {sustain_hours:.1f}h < "
            f"{classifier.hours_above_50pct_peak_min}h"
        )

    if classifier.require_lp_burned_or_locked:
        if enrichment and (enrichment.lp_burned or enrichment.lp_locked):
            pass_reasons.append("LP burned or locked")
        elif enrichment is None or (
            enrichment.lp_burned is None and enrichment.lp_locked is None
        ):
            # Don't outright fail when we just don't know yet — flag as soft fail.
            fail_reasons.append("LP burn/lock status unknown")
        else:
            fail_reasons.append("LP not burned/locked")

    # ---- Currently dead ------------------------------------------------------
    drawdown: float | None = None
    if peak_mc > 0 and snap and snap.market_cap_usd is not None:
        drawdown = max(0.0, 1.0 - (snap.market_cap_usd / peak_mc))
        if drawdown >= classifier.current_drawdown_from_ath_min:
            pass_reasons.append(f"drawdown from ATH {drawdown:.0%}")
        else:
            fail_reasons.append(
                f"drawdown only {drawdown:.0%} < {classifier.current_drawdown_from_ath_min:.0%}"
            )
    else:
        fail_reasons.append("no current MC snapshot")

    if snap and snap.volume_24h_usd is not None:
        if snap.volume_24h_usd <= classifier.current_volume_24h_max_usd:
            pass_reasons.append(f"24h vol ${snap.volume_24h_usd:,.0f} ≤ ${classifier.current_volume_24h_max_usd:,.0f}")
        else:
            fail_reasons.append(
                f"24h vol ${snap.volume_24h_usd:,.0f} > ${classifier.current_volume_24h_max_usd:,.0f}"
            )

    hours_since_ath = now_minus_hours(token.ath_at)
    if hours_since_ath is not None:
        if hours_since_ath >= classifier.hours_since_ath_min:
            pass_reasons.append(f"ATH was {hours_since_ath:.0f}h ago")
        else:
            fail_reasons.append(
                f"ATH only {hours_since_ath:.0f}h ago < {classifier.hours_since_ath_min}h"
            )
    else:
        fail_reasons.append("no ATH timestamp yet")

    if enrichment and enrichment.dev_last_active_at:
        days_inactive = (datetime.utcnow() - enrichment.dev_last_active_at).total_seconds() / 86400.0
        if days_inactive >= classifier.dev_wallet_inactive_days_min:
            pass_reasons.append(f"dev inactive for {days_inactive:.1f} days")
        else:
            fail_reasons.append(
                f"dev still active ({days_inactive:.1f} days ago < "
                f"{classifier.dev_wallet_inactive_days_min}d)"
            )

    is_candidate = len(fail_reasons) == 0 and len(pass_reasons) > 0

    score, breakdown = compute_idea_score(token, enrichment, cfg.idea_score)

    sm = get_sessionmaker()
    async with sm() as s:
        cls = await s.get(Classification, token.address)
        if cls is None:
            cls = Classification(token_address=token.address)
            s.add(cls)
        cls.is_redeploy_candidate = is_candidate
        cls.drawdown_from_ath = drawdown
        cls.hours_since_ath = hours_since_ath
        cls.idea_score = score
        cls.idea_score_breakdown_json = json.dumps(breakdown, default=str)
        cls.fail_reasons_json = json.dumps(fail_reasons)
        cls.pass_reasons_json = json.dumps(pass_reasons)
        await s.commit()

    return cls


async def run_classifier() -> dict[str, int]:
    cfg = get_config()
    sm = get_sessionmaker()
    async with sm() as s:
        rows = await s.execute(
            select(Token).where(
                Token.chain_id == cfg.discovery.chain_id,
                Token.ath_mc_usd.is_not(None),
            )
        )
        tokens = list(rows.scalars().all())

    classified = 0
    candidates = 0
    for t in tokens:
        try:
            cls = await classify_token(t)
            classified += 1
            if cls.is_redeploy_candidate:
                candidates += 1
        except Exception as exc:  # noqa: BLE001
            log.error("classify.failed", mint=t.address, error=str(exc))

    log.info("classify.completed", classified=classified, candidates=candidates)
    return {"classified": classified, "candidates": candidates}

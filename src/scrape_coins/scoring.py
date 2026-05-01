"""Composite 'idea score' — used to rank redeploy candidates by how strongly the
underlying concept resonated, regardless of how the team executed.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any

from .config import IdeaScoreCfg
from .db import Enrichment, Token


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _norm_log(value: float, lo_log: float, hi_log: float) -> float:
    if value <= 0:
        return 0.0
    v = math.log10(value)
    if hi_log <= lo_log:
        return 0.0
    return _clamp01((v - lo_log) / (hi_log - lo_log))


def _ticker_quality(symbol: str | None, name: str | None) -> float:
    """Short, all-caps, alpha tickers score higher; long noisy ones score lower."""
    if not symbol:
        return 0.0
    sym = symbol.strip()
    if not sym:
        return 0.0
    score = 0.0
    if 2 <= len(sym) <= 6:
        score += 0.5
    elif len(sym) <= 8:
        score += 0.3
    if re.fullmatch(r"[A-Z0-9]+", sym):
        score += 0.3
    if name and 1 <= len(name.strip()) <= 24:
        score += 0.2
    return _clamp01(score)


def _social_presence(token: Token) -> float:
    score = 0.0
    if token.website_url:
        score += 0.34
    if token.twitter_url:
        score += 0.33
    if token.telegram_url:
        score += 0.33
    return _clamp01(score)


def compute_idea_score(
    token: Token,
    enrichment: Enrichment | None,
    cfg: IdeaScoreCfg,
) -> tuple[float, dict[str, Any]]:
    """Return (score in 0..1, breakdown dict for the dashboard)."""

    weights = cfg.weights
    breakdown: dict[str, Any] = {}

    # 1) Peak holders
    peak_holders = (enrichment.holders_count_at_peak if enrichment else None) or 0
    s_holders = _norm_log(peak_holders, cfg.peak_holders_log_min, cfg.peak_holders_log_max)
    breakdown["peak_holders"] = {"value": peak_holders, "score": s_holders}

    # 2) Holder diversity at peak (1 - top10 concentration)
    top10_peak = (enrichment.top10_concentration_at_peak if enrichment else None)
    s_div = _clamp01(1.0 - top10_peak) if top10_peak is not None else 0.0
    breakdown["holder_diversity"] = {"value": top10_peak, "score": s_div}

    # 3) Volume intensity at peak: peak_24h_vol / peak_mc
    peak_mc = token.ath_mc_usd or 0.0
    peak_vol = token.ath_volume_24h_usd or 0.0
    intensity = (peak_vol / peak_mc) if peak_mc > 0 else 0.0
    s_intensity = _norm_log(
        intensity,
        cfg.volume_intensity_log_min,
        cfg.volume_intensity_log_max,
    )
    breakdown["volume_intensity"] = {"value": intensity, "score": s_intensity}

    # 4) Sustain — hours_above_50pct_peak / 168 (one week)
    sustain_hours = token.hours_above_50pct_peak or 0.0
    s_sustain = _clamp01(sustain_hours / 168.0)
    breakdown["sustain"] = {"value": sustain_hours, "score": s_sustain}

    # 5) Holder retention now / peak
    holders_now = (enrichment.holders_count if enrichment else None) or 0
    retention = (holders_now / peak_holders) if peak_holders > 0 else 0.0
    s_retention = _clamp01(retention)
    breakdown["holder_retention"] = {"value": retention, "score": s_retention}

    # 6) Time to peak (faster = more viral). Best signal we have: pair_created_at -> ath_at.
    created = token.pair_created_at
    ath_at = token.ath_at
    if created and ath_at and ath_at > created:
        hours_to_peak = (ath_at - created).total_seconds() / 3600.0
        # 1h or less = 1.0; 24h+ = 0.0; linear in between.
        s_ttp = _clamp01(1.0 - (hours_to_peak - 1.0) / 23.0) if hours_to_peak >= 1 else 1.0
    else:
        hours_to_peak = None
        s_ttp = 0.0
    breakdown["time_to_peak"] = {"value": hours_to_peak, "score": s_ttp}

    # 7) Social presence
    s_social = _social_presence(token)
    breakdown["social_presence"] = {"score": s_social}

    # 8) Ticker / name quality
    s_ticker = _ticker_quality(token.symbol, token.name)
    breakdown["ticker_quality"] = {"score": s_ticker}

    weighted = (
        weights.peak_holders * s_holders
        + weights.holder_diversity * s_div
        + weights.volume_intensity * s_intensity
        + weights.sustain * s_sustain
        + weights.holder_retention * s_retention
        + weights.time_to_peak * s_ttp
        + weights.social_presence * s_social
        + weights.ticker_quality * s_ticker
    )
    total_w = (
        weights.peak_holders
        + weights.holder_diversity
        + weights.volume_intensity
        + weights.sustain
        + weights.holder_retention
        + weights.time_to_peak
        + weights.social_presence
        + weights.ticker_quality
    )
    score = weighted / total_w if total_w > 0 else 0.0
    return _clamp01(score), breakdown


def now_minus_hours(ath_at: datetime | None) -> float | None:
    if ath_at is None:
        return None
    return (datetime.utcnow() - ath_at).total_seconds() / 3600.0

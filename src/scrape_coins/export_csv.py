"""Export joined token / classification / enrichment rows to CSV."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from sqlalchemy import and_, select

from .db import Classification, Enrichment, Token, get_sessionmaker, init_db


def _fmt_dt(v: datetime | None) -> str:
    if v is None:
        return ""
    return v.isoformat(timespec="seconds")


def _cell_bool(v: bool | None) -> str:
    if v is None:
        return ""
    return "true" if v else "false"


def _cell_float(v: float | None) -> str:
    if v is None:
        return ""
    return str(v)


def _cell_int(v: int | None) -> str:
    if v is None:
        return ""
    return str(v)


async def export_tokens_csv(
    out_path: str | Path,
    *,
    only_candidates: bool = False,
    only_dexscreener_paid_boost: bool = False,
) -> int:
    """Write one CSV row per token; returns row count (excluding header)."""
    await init_db()
    sm = get_sessionmaker()
    out = Path(out_path)

    headers = [
        "address",
        "symbol",
        "name",
        "chain_id",
        "pair_url",
        "pair_created_at",
        "discovered_at",
        "ath_mc_usd",
        "ath_at",
        "current_mc_usd",
        "current_liq_usd",
        "current_volume_24h_usd",
        "current_price_change_24h_pct",
        "website_url",
        "twitter_url",
        "telegram_url",
        "is_redeploy_candidate",
        "idea_score",
        "drawdown_from_ath",
        "hours_since_ath",
        "fail_reasons_json",
        "pass_reasons_json",
        "holders_count",
        "top10_concentration",
        "holders_count_at_peak",
        "top10_concentration_at_peak",
        "dev_wallet",
        "dev_last_active_at",
        "lp_burned",
        "lp_locked",
    ]

    q = (
        select(Token, Classification, Enrichment)
        .outerjoin(Classification, Classification.token_address == Token.address)
        .outerjoin(Enrichment, Enrichment.token_address == Token.address)
    )
    if only_candidates or only_dexscreener_paid_boost:
        conds = []
        if only_candidates:
            conds.append(Classification.is_redeploy_candidate.is_(True))
        if only_dexscreener_paid_boost:
            conds.append(Token.dexscreener_paid_boost.is_(True))
        q = q.where(and_(*conds))

    async with sm() as session:
        result = await session.execute(q)
        rows = result.all()

    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for token, cls, enr in rows:
            w.writerow(
                [
                    token.address,
                    token.symbol or "",
                    token.name or "",
                    token.chain_id,
                    token.pair_url or "",
                    _fmt_dt(token.pair_created_at),
                    _fmt_dt(token.discovered_at),
                    _cell_float(token.ath_mc_usd),
                    _fmt_dt(token.ath_at),
                    _cell_float(token.current_mc_usd),
                    _cell_float(token.current_liq_usd),
                    _cell_float(token.current_volume_24h_usd),
                    _cell_float(token.current_price_change_24h_pct),
                    token.website_url or "",
                    token.twitter_url or "",
                    token.telegram_url or "",
                    _cell_bool(cls.is_redeploy_candidate if cls else None),
                    _cell_float(cls.idea_score if cls else None),
                    _cell_float(cls.drawdown_from_ath if cls else None),
                    _cell_float(cls.hours_since_ath if cls else None),
                    (cls.fail_reasons_json if cls and cls.fail_reasons_json else "")
                    or "",
                    (cls.pass_reasons_json if cls and cls.pass_reasons_json else "")
                    or "",
                    _cell_int(enr.holders_count if enr else None),
                    _cell_float(enr.top10_concentration if enr else None),
                    _cell_int(enr.holders_count_at_peak if enr else None),
                    _cell_float(enr.top10_concentration_at_peak if enr else None),
                    (enr.dev_wallet or "") if enr else "",
                    _fmt_dt(enr.dev_last_active_at if enr else None),
                    _cell_bool(enr.lp_burned if enr else None),
                    _cell_bool(enr.lp_locked if enr else None),
                ]
            )
            n += 1
    return n

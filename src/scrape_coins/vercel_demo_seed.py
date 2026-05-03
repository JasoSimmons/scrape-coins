"""Pre-filled demo listings for serverless deployments (instant dashboard).

Inserted only when ``VERCEL`` is set and the ``tokens`` table is empty — see
``apply_vercel_demo_if_empty``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import insert
from sqlalchemy.engine import Connection

from .db import Classification, Token


def apply_vercel_demo_if_empty(sync_conn: Connection, *, now: datetime) -> None:
    if not os.environ.get("VERCEL") or os.environ.get("VERCEL_SKIP_DEMO_SEED"):
        return
    cnt = sync_conn.exec_driver_sql("SELECT COUNT(*) FROM tokens").scalar() or 0
    if cnt > 0:
        return

    tokens: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    defs = (
        ("DEMO", "Demo Runner", False, 0.42),
        ("VCTL", "Vercel Torch", False, 0.38),
        ("SEED", "Seed Capsule", True, 0.55),
        ("MOOK", "Mock Moon Coin", False, 0.21),
        ("PRXY", "Proxy Peak", True, 0.48),
        ("HODL", "Hold Sample", False, 0.33),
        ("PUMP", "Pump Preview", False, 0.27),
        ("REDP", "Redeploy Test", True, 0.61),
        ("ATHX", "ATH Example", False, 0.19),
        ("DROP", "Drawdown Doll", True, 0.52),
        ("WAVE", "Volume Wave", False, 0.31),
        ("SOLS", "Solana Sample", False, 0.25),
        ("MEME", "Meme Sandbox", False, 0.29),
        ("DEAD", "Dead Cat Bounce", True, 0.58),
        ("LPXX", "Liquidity Lesson", False, 0.24),
    )

    for i, (sym, name, cand, idea) in enumerate(defs):
        addr = f"Demo{i:02d}" + ("X" * (44 - 6))
        addr = addr[:44]
        days_ago = 3 + (i % 30)
        paired = now - timedelta(days=days_ago)
        ath = paired + timedelta(hours=6)
        dd = (0.90 + i * 0.002) % 0.995
        cur_mc = 400_000.0 * max(0.03, (1.0 - dd))

        tokens.append(
            {
                "address": addr,
                "chain_id": "solana",
                "symbol": sym,
                "name": name,
                "pair_dex_id": "orca" if i % 2 == 0 else "raydium",
                "pair_url": f"https://dexscreener.com/solana/{addr.lower()}",
                "pair_created_at": paired,
                "discovered_at": paired - timedelta(minutes=5),
                "discovery_source": "vercel_demo_seed",
                "dexscreener_paid_boost": bool(i % 5 == 0),
                "ath_mc_usd": 400_000.0 + i * 8000,
                "ath_at": ath,
                "ath_volume_24h_usd": 650_000.0 + i * 10_000,
                "ath_swaps_24h": 1200 + i * 100,
                "hours_above_50pct_peak": 31.5 + float(i % 48),
                "current_price_usd": 5e-5 + i * 1e-6,
                "current_mc_usd": cur_mc,
                "current_liq_usd": max(3500.0, 9000.0 - i * 200),
                "current_volume_24h_usd": 1800.0 if i % 3 else 980.0,
                "current_buys_24h": 400 + i,
                "current_sells_24h": 355 + i,
                "current_price_change_5m_pct": -0.35 + i * 0.1,
                "current_price_change_1h_pct": -1.1 + i * 0.12,
                "current_price_change_6h_pct": -8.5 + float(i % 17),
                "current_price_change_24h_pct": -12.7 + float(i % 39),
                "last_seen_at": now - timedelta(minutes=10 + i),
            }
        )

        hrs_ath = (now - ath).total_seconds() / 3600.0
        rows.append(
            {
                "token_address": addr,
                "is_redeploy_candidate": cand,
                "drawdown_from_ath": dd,
                "hours_since_ath": hrs_ath,
                "idea_score": idea,
                "idea_score_breakdown_json": json.dumps({"demo": True, "stub": sym}),
                "fail_reasons_json": json.dumps(
                    [] if cand else ["demo: illustrative fail reason"]
                ),
                "pass_reasons_json": json.dumps(
                    ["demo: illustrative pass"] if cand else []
                ),
            }
        )

    sync_conn.execute(insert(Token), tokens)
    sync_conn.execute(insert(Classification), rows)

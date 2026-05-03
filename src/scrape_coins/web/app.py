"""FastAPI dashboard + scheduler host."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, case, cast, func as sqlfunc, or_, select, true as sql_true
from sqlalchemy.types import Float as SqlFloat

from ..classifier import run_classifier
from ..config import get_config, reload_config
from ..db import (
    Classification,
    Enrichment,
    PriceSnapshot,
    Token,
    get_sessionmaker,
    init_db,
)
from ..logging_setup import get_logger
from ..scheduler import build_scheduler
from ..workers.discovery import run_discovery
from ..workers.enrich import run_enrichment
from ..workers.snapshot import run_snapshot

log = get_logger(__name__)

WEB_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # APScheduler is for long-running `scrape-coins serve`; serverless has no persistent job loop.
    if os.environ.get("VERCEL"):
        log.info("web.scheduler_skipped", reason="vercel")
        yield
        return
    sched = build_scheduler()
    sched.start()
    log.info("web.scheduler_started")
    try:
        yield
    finally:
        sched.shutdown(wait=False)
        log.info("web.scheduler_stopped")


app = FastAPI(title="scrape-coins", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")


# ------------------ helpers ----------------------------------------------------


SORT_COLUMNS = {
    "idea_score": Classification.idea_score,
    "peak_mc": Token.ath_mc_usd,
    "current_mc": Token.current_mc_usd,
    "current_liq": Token.current_liq_usd,
    "current_vol": Token.current_volume_24h_usd,
    "drawdown": Classification.drawdown_from_ath,
    "hours_since_ath": Classification.hours_since_ath,
    "age": Token.pair_created_at,
    "symbol": Token.symbol,
    "change_24h": Token.current_price_change_24h_pct,
    "change_1h": Token.current_price_change_1h_pct,
}


def _best_candidate_order(direction: str):
    """Rank redeploy picks: strongest idea first, then deepest drawdown, then bigger peak.

    `direction` flips all three keys together so "asc" still behaves consistently
    (mostly useful for debugging — the default desc is what you want day-to-day).
    """
    if direction == "asc":
        return (
            Classification.idea_score.asc().nullslast(),
            Classification.drawdown_from_ath.asc().nullslast(),
            Token.ath_mc_usd.asc().nullslast(),
        )
    return (
        Classification.idea_score.desc().nullslast(),
        Classification.drawdown_from_ath.desc().nullslast(),
        Token.ath_mc_usd.desc().nullslast(),
    )


def _clamp01(expr):
    return case(
        (expr < 0, 0.0),
        (expr > 1, 1.0),
        else_=expr,
    )


def _lin_norm(expr, lo: float, hi: float):
    denom = hi - lo
    if denom <= 0:
        return cast(0.0, SqlFloat)
    return _clamp01((expr - lo) / denom)


def _team_first_score_expr(cfg):
    """Higher = better opportunity for a team-led redeploy (dashboard ordering)."""
    tfs = cfg.team_first_sort
    w = tfs.weights.model_dump()
    total = float(sum(max(0.0, float(v)) for v in w.values())) or 1.0
    nw = {k: max(0.0, float(v)) / total for k, v in w.items()}

    idea = cast(sqlfunc.coalesce(Classification.idea_score, 0.0), SqlFloat)

    ath = cast(sqlfunc.coalesce(Token.ath_mc_usd, 0.0), SqlFloat)
    peak_mc_n = _lin_norm(
        sqlfunc.log10(sqlfunc.max(ath, 1.0)),
        float(tfs.peak_mc_log_min),
        float(tfs.peak_mc_log_max),
    )

    holders_raw = cast(
        sqlfunc.coalesce(
            Enrichment.holders_count_at_peak,
            Enrichment.holders_count,
            0,
        ),
        SqlFloat,
    )
    peak_holders_n = _lin_norm(
        sqlfunc.log10(sqlfunc.max(holders_raw, 1.0)),
        float(tfs.holders_log_min),
        float(tfs.holders_log_max),
    )

    top10 = cast(sqlfunc.coalesce(Enrichment.top10_concentration_at_peak, 0.5), SqlFloat)
    distribution_n = _clamp01(1.0 - top10)

    cur_mc = cast(sqlfunc.coalesce(Token.current_mc_usd, 0.0), SqlFloat)
    ath2 = cast(sqlfunc.coalesce(Token.ath_mc_usd, 0.0), SqlFloat)
    dd = case(
        (ath2 > 0, 1.0 - (cur_mc / ath2)),
        else_=cast(0.0, SqlFloat),
    )
    drawdown_n = _clamp01(dd)

    social_n = cast(
        sqlfunc.coalesce(
            case((Token.website_url.is_not(None), 0.34), else_=0.0)
            + case((Token.twitter_url.is_not(None), 0.33), else_=0.0)
            + case((Token.telegram_url.is_not(None), 0.33), else_=0.0),
            0.0,
        ),
        SqlFloat,
    )
    social_n = _clamp01(social_n)

    score = (
        nw["idea_score"] * idea
        + nw["peak_mc"] * peak_mc_n
        + nw["peak_holders"] * peak_holders_n
        + nw["holder_distribution"] * distribution_n
        + nw["drawdown"] * drawdown_n
        + nw["social_links"] * social_n
    )
    return cast(score, SqlFloat)


def _team_first_order(cfg, direction: str):
    score = _team_first_score_expr(cfg)
    if direction == "asc":
        return (score.asc().nullslast(),)
    return (score.desc().nullslast(),)


def _fmt_money(v: float | None) -> str:
    if v is None:
        return "—"
    if v >= 1_000_000_000:
        return f"${v / 1_000_000_000:.2f}B"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v / 1_000:.1f}K"
    if v >= 1:
        return f"${v:,.2f}"
    return f"${v:.4f}"


def _fmt_price(v: float | None) -> str:
    if v is None:
        return "—"
    if v >= 1:
        return f"${v:,.4f}"
    if v >= 0.01:
        return f"${v:.4f}"
    if v >= 0.0001:
        return f"${v:.6f}"
    if v <= 0:
        return "—"
    # Sub-fractional notation: $0.0₅1234 = $0.000001234
    s = f"{v:.12f}".rstrip("0").rstrip(".")
    if "." in s:
        whole, frac = s.split(".")
        leading = len(frac) - len(frac.lstrip("0"))
        if leading >= 4:
            sig = frac.lstrip("0")[:4] or "0"
            return f"$0.0(<sub>{leading}</sub>){sig}"
    return f"${v:.10f}"


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.0f}%"


def _fmt_change(v: float | None) -> str:
    """Format a DexScreener-style price change percent (already in percent units)."""
    if v is None:
        return "—"
    sign = "+" if v >= 0 else ""
    if abs(v) >= 1000:
        return f"{sign}{v:,.0f}%"
    if abs(v) >= 100:
        return f"{sign}{v:.0f}%"
    return f"{sign}{v:.1f}%"


def _change_class(v: float | None) -> str:
    if v is None:
        return "neutral"
    if v > 0.01:
        return "up"
    if v < -0.01:
        return "down"
    return "neutral"


def _fmt_age(hours: float | None) -> str:
    if hours is None:
        return "—"
    if hours < 1:
        return f"{int(hours * 60)}m"
    if hours < 24:
        return f"{hours:.0f}h"
    days = hours / 24.0
    if days < 30:
        return f"{days:.1f}d"
    if days < 365:
        return f"{days / 30.0:.1f}mo"
    return f"{days / 365.0:.1f}y"


def _fmt_count(v: int | None) -> str:
    if v is None:
        return "—"
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}K"
    return str(v)


TEMPLATES.env.filters["money"] = _fmt_money
TEMPLATES.env.filters["price"] = _fmt_price
TEMPLATES.env.filters["pct"] = _fmt_pct
TEMPLATES.env.filters["change"] = _fmt_change
TEMPLATES.env.filters["change_class"] = _change_class
TEMPLATES.env.filters["age"] = _fmt_age
TEMPLATES.env.filters["count"] = _fmt_count


# ------------------ routes -----------------------------------------------------


def _recent_token_clause(*, cutoff: datetime, fallback_to_discovered: bool):
    pair_ok = and_(
        Token.pair_created_at.is_not(None),
        Token.pair_created_at >= cutoff,
    )

    if not fallback_to_discovered:
        return pair_ok

    discovered_ok = and_(
        Token.pair_created_at.is_(None),
        Token.discovered_at.is_not(None),
        Token.discovered_at >= cutoff,
    )
    return or_(pair_ok, discovered_ok)


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    sort: str | None = Query(None),
    direction: str = Query("desc"),
    only_candidates: bool = Query(False),
    limit: int = Query(200, ge=1, le=2000),
    q: str | None = Query(None, description="Filter by symbol/name substring"),
    within_hours: int | None = Query(
        None, ge=-1, le=24 * 366, description="Listing age <= N hours (-1 disables age filter)"
    ),
    no_age_cap: bool = Query(False, description="Bypass the listing-age cutoff"),
):
    cfg = get_config()
    df = cfg.dashboard_filters
    now = datetime.utcnow()

    # Age window ------------------------------------------------------------
    fallback = bool(df.use_discovered_at_if_no_pair_created)

    explicit_hours_supplied = within_hours is not None
    desired_hours = (
        df.max_pair_age_hours if within_hours is None else int(within_hours)
    )

    # If caller omits params, obey config.enabled by default.
    age_filter_enabled = (not explicit_hours_supplied and df.enabled) or (
        explicit_hours_supplied and not no_age_cap
    )

    cutoff: datetime | None = None
    if age_filter_enabled and desired_hours >= 0:
        cutoff = now - timedelta(hours=float(desired_hours))

    age_clause: Any = sql_true()
    if cutoff is not None:
        age_clause = _recent_token_clause(cutoff=cutoff, fallback_to_discovered=fallback)

    # Default sort: volume for the broad table; "best pick" composite when hunting candidates.
    qp_sort = request.query_params.get("sort")
    effective_sort = sort
    if effective_sort is None:
        if only_candidates and qp_sort is None:
            effective_sort = "best_candidate"
        else:
            effective_sort = "team_first"

    # Main query ------------------------------------------------------------
    sm = get_sessionmaker()
    if effective_sort == "best_candidate":
        order_exprs = _best_candidate_order(direction)
    elif effective_sort == "team_first":
        order_exprs = _team_first_order(cfg, direction)
    else:
        sort_col = SORT_COLUMNS.get(effective_sort, Token.current_volume_24h_usd)
        order_exprs = (
            (sort_col.desc() if direction == "desc" else sort_col.asc()).nullslast(),
        )
    async with sm() as s:
        query = (
            select(Token, Classification, Enrichment)
            .outerjoin(Classification, Classification.token_address == Token.address)
            .outerjoin(Enrichment, Enrichment.token_address == Token.address)
            .where(age_clause)
        )
        if only_candidates:
            query = query.where(Classification.is_redeploy_candidate.is_(True))
        if q:
            like = f"%{q}%"
            query = query.where(
                or_(
                    Token.symbol.ilike(like),
                    Token.name.ilike(like),
                    Token.address.ilike(like),
                )
            )
        query = query.order_by(*order_exprs).limit(limit)
        rows = (await s.execute(query)).all()

    coins = []
    for token, cls, enr in rows:
        age_hours = None
        if token.pair_created_at:
            age_hours = (now - token.pair_created_at).total_seconds() / 3600.0
        elif fallback and token.discovered_at:
            age_hours = (now - token.discovered_at).total_seconds() / 3600.0
        idea_pct = int(round(((cls.idea_score if cls else 0) or 0) * 100))
        drawdown = None
        if cls and cls.drawdown_from_ath is not None:
            drawdown = cls.drawdown_from_ath
        elif token.ath_mc_usd and token.current_mc_usd:
            drawdown = max(0.0, 1.0 - (token.current_mc_usd / token.ath_mc_usd))
        coins.append(
            {
                "token": token,
                "cls": cls,
                "enr": enr,
                "idea_score_pct": idea_pct,
                "age_hours": age_hours,
                "drawdown": drawdown,
                "is_candidate": bool(cls and cls.is_redeploy_candidate),
            }
        )

    # Stats banner (scoped to listing-age cutoff) -------------------------
    async with sm() as s:
        tracked_window_stmt = select(sqlfunc.count()).select_from(Token).where(
            age_clause
        )
        total_tracked = (await s.execute(tracked_window_stmt)).scalar() or 0

        cand_stmt = (
            select(sqlfunc.count())
            .select_from(Token)
            .join(Classification, Classification.token_address == Token.address)
            .where(Classification.is_redeploy_candidate.is_(True))
            .where(age_clause)
        )
        total_candidates = (await s.execute(cand_stmt)).scalar() or 0

        grand_total_stmt = select(sqlfunc.count()).select_from(Token)
        total_tracked_all = (await s.execute(grand_total_stmt)).scalar() or 0

    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "coins": coins,
            "sort": effective_sort,
            "direction": direction,
            "only_candidates": only_candidates,
            "search_q": q or "",
            "limit": limit,
            "config": cfg,
            # Window-scoped stats (respects listing-age cutoff)
            "total_tracked": total_tracked,
            "total_candidates": total_candidates,
            "total_tracked_all": total_tracked_all,
            # UI state helpers
            "within_hours_ui": desired_hours,
            "age_filter_enabled_ui": cutoff is not None,
            "age_cutoff_text": cutoff.isoformat(timespec="seconds") if cutoff else None,
            "now": now.strftime("%Y-%m-%d %H:%M UTC"),
            # Vercel: demo rows hydrate on startup; fallback text if skipped and still empty.
            "serverless_empty_hint": bool(os.environ.get("VERCEL"))
            and total_tracked_all == 0,
            "vercel_hosting_banner": bool(os.environ.get("VERCEL")),
        },
    )


@app.get("/coin/{address}", response_class=HTMLResponse)
async def coin_detail(request: Request, address: str):
    sm = get_sessionmaker()
    async with sm() as s:
        token = await s.get(Token, address)
        if token is None:
            raise HTTPException(status_code=404, detail="token not found")
        cls = await s.get(Classification, address)
        enr = await s.get(Enrichment, address)
        rows = await s.execute(
            select(PriceSnapshot)
            .where(PriceSnapshot.token_address == address)
            .order_by(PriceSnapshot.taken_at)
        )
        snaps = list(rows.scalars().all())

    spark = [
        {"t": s.taken_at.isoformat(), "mc": s.market_cap_usd, "vol": s.volume_24h_usd}
        for s in snaps
    ]

    breakdown: dict[str, Any] = {}
    if cls and cls.idea_score_breakdown_json:
        try:
            breakdown = json.loads(cls.idea_score_breakdown_json)
        except json.JSONDecodeError:
            breakdown = {}

    return TEMPLATES.TemplateResponse(
        request,
        "coin.html",
        {
            "token": token,
            "cls": cls,
            "enr": enr,
            "snaps": snaps,
            "spark_json": json.dumps(spark),
            "breakdown": breakdown,
            "pass_reasons": json.loads(cls.pass_reasons_json) if cls and cls.pass_reasons_json else [],
            "fail_reasons": json.loads(cls.fail_reasons_json) if cls and cls.fail_reasons_json else [],
        },
    )


# --- Manual trigger endpoints (for "run now" buttons / debugging) -------------


@app.post("/api/run/discovery")
async def api_run_discovery():
    return JSONResponse(await run_discovery())


@app.post("/api/run/snapshot")
async def api_run_snapshot():
    return JSONResponse(await run_snapshot())


@app.post("/api/run/enrich")
async def api_run_enrich():
    return JSONResponse(await run_enrichment())


@app.post("/api/run/classify")
async def api_run_classify():
    return JSONResponse(await run_classifier())


@app.post("/api/config/reload")
async def api_reload_config():
    cfg = reload_config()
    return JSONResponse({"reloaded": True, "scheduler": cfg.scheduler.model_dump()})


@app.get("/api/healthz")
async def healthz():
    return {"ok": True, "ts": datetime.utcnow().isoformat()}

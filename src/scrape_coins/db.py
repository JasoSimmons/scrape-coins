"""SQLAlchemy models + async session factory.

Schema overview:
- tokens         : one row per discovered SOL mint
- price_snapshots: time series of (price, mc, fdv, liq, vol_24h, txns_24h) per token
- enrichments    : latest on-chain enrichment per token (holders, top10, dev wallet, LP status)
- classifications: latest classifier verdict + idea score per token
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .config import get_env


class Base(DeclarativeBase):
    pass


class Token(Base):
    __tablename__ = "tokens"

    address: Mapped[str] = mapped_column(String, primary_key=True)
    chain_id: Mapped[str] = mapped_column(String, default="solana", index=True)
    symbol: Mapped[Optional[str]] = mapped_column(String)
    name: Mapped[Optional[str]] = mapped_column(String)
    image_url: Mapped[Optional[str]] = mapped_column(Text)

    pair_address: Mapped[Optional[str]] = mapped_column(String, index=True)
    pair_dex_id: Mapped[Optional[str]] = mapped_column(String)
    pair_url: Mapped[Optional[str]] = mapped_column(Text)
    pair_created_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    description: Mapped[Optional[str]] = mapped_column(Text)
    website_url: Mapped[Optional[str]] = mapped_column(Text)
    twitter_url: Mapped[Optional[str]] = mapped_column(Text)
    telegram_url: Mapped[Optional[str]] = mapped_column(Text)
    other_links_json: Mapped[Optional[str]] = mapped_column(Text)

    # Rolling derived fields (updated by snapshot worker so we can filter cheaply).
    ath_mc_usd: Mapped[Optional[float]] = mapped_column(Float, index=True)
    ath_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    ath_volume_24h_usd: Mapped[Optional[float]] = mapped_column(Float)
    ath_swaps_24h: Mapped[Optional[int]] = mapped_column(Integer)
    hours_above_50pct_peak: Mapped[Optional[float]] = mapped_column(Float)

    # Cached "latest snapshot" values so the dashboard doesn't need a join.
    current_price_usd: Mapped[Optional[float]] = mapped_column(Float)
    current_mc_usd: Mapped[Optional[float]] = mapped_column(Float, index=True)
    current_liq_usd: Mapped[Optional[float]] = mapped_column(Float)
    current_volume_24h_usd: Mapped[Optional[float]] = mapped_column(Float)
    current_buys_24h: Mapped[Optional[int]] = mapped_column(Integer)
    current_sells_24h: Mapped[Optional[int]] = mapped_column(Integer)
    current_price_change_5m_pct: Mapped[Optional[float]] = mapped_column(Float)
    current_price_change_1h_pct: Mapped[Optional[float]] = mapped_column(Float)
    current_price_change_6h_pct: Mapped[Optional[float]] = mapped_column(Float)
    current_price_change_24h_pct: Mapped[Optional[float]] = mapped_column(Float)

    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    discovery_source: Mapped[Optional[str]] = mapped_column(String)
    # True once seen on DexScreener paid boost feeds (/token-boosts/*), regardless of discover order.
    dexscreener_paid_boost: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    snapshots: Mapped[list["PriceSnapshot"]] = relationship(
        back_populates="token",
        cascade="all, delete-orphan",
    )
    enrichment: Mapped[Optional["Enrichment"]] = relationship(
        back_populates="token",
        uselist=False,
        cascade="all, delete-orphan",
    )
    classification: Mapped[Optional["Classification"]] = relationship(
        back_populates="token",
        uselist=False,
        cascade="all, delete-orphan",
    )


class PriceSnapshot(Base):
    __tablename__ = "price_snapshots"
    __table_args__ = (
        Index("ix_snapshots_token_taken_at", "token_address", "taken_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_address: Mapped[str] = mapped_column(
        ForeignKey("tokens.address", ondelete="CASCADE"), index=True
    )
    taken_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    price_usd: Mapped[Optional[float]] = mapped_column(Float)
    market_cap_usd: Mapped[Optional[float]] = mapped_column(Float)
    fdv_usd: Mapped[Optional[float]] = mapped_column(Float)
    liquidity_usd: Mapped[Optional[float]] = mapped_column(Float)
    volume_24h_usd: Mapped[Optional[float]] = mapped_column(Float)
    txns_24h_buys: Mapped[Optional[int]] = mapped_column(Integer)
    txns_24h_sells: Mapped[Optional[int]] = mapped_column(Integer)
    price_change_5m_pct: Mapped[Optional[float]] = mapped_column(Float)
    price_change_1h_pct: Mapped[Optional[float]] = mapped_column(Float)
    price_change_6h_pct: Mapped[Optional[float]] = mapped_column(Float)
    price_change_24h_pct: Mapped[Optional[float]] = mapped_column(Float)

    token: Mapped[Token] = relationship(back_populates="snapshots")


class Enrichment(Base):
    __tablename__ = "enrichments"

    token_address: Mapped[str] = mapped_column(
        ForeignKey("tokens.address", ondelete="CASCADE"), primary_key=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    holders_count: Mapped[Optional[int]] = mapped_column(Integer)
    holders_count_capped: Mapped[Optional[bool]] = mapped_column(Boolean)
    top10_concentration: Mapped[Optional[float]] = mapped_column(Float)  # 0-1, ex LP/burn
    holders_count_at_peak: Mapped[Optional[int]] = mapped_column(Integer)
    top10_concentration_at_peak: Mapped[Optional[float]] = mapped_column(Float)

    dev_wallet: Mapped[Optional[str]] = mapped_column(String)
    dev_last_active_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    dev_dumped_pct_within_1h_of_peak: Mapped[Optional[float]] = mapped_column(Float)

    lp_burned: Mapped[Optional[bool]] = mapped_column(Boolean)
    lp_locked: Mapped[Optional[bool]] = mapped_column(Boolean)

    raw_json: Mapped[Optional[str]] = mapped_column(Text)

    token: Mapped[Token] = relationship(back_populates="enrichment")


class Classification(Base):
    __tablename__ = "classifications"
    __table_args__ = (UniqueConstraint("token_address", name="uq_classification_token"),)

    token_address: Mapped[str] = mapped_column(
        ForeignKey("tokens.address", ondelete="CASCADE"), primary_key=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    is_redeploy_candidate: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    drawdown_from_ath: Mapped[Optional[float]] = mapped_column(Float)  # 0-1
    hours_since_ath: Mapped[Optional[float]] = mapped_column(Float)

    idea_score: Mapped[Optional[float]] = mapped_column(Float, index=True)
    idea_score_breakdown_json: Mapped[Optional[str]] = mapped_column(Text)

    fail_reasons_json: Mapped[Optional[str]] = mapped_column(Text)
    pass_reasons_json: Mapped[Optional[str]] = mapped_column(Text)

    token: Mapped[Token] = relationship(back_populates="classification")


# ---- Engine / session factory --------------------------------------------------

_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine, _sessionmaker
    if _engine is None:
        env = get_env()
        url = f"sqlite+aiosqlite:///{env.scrape_coins_db}"
        _engine = create_async_engine(url, future=True)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


_NEW_COLUMNS: list[tuple[str, str, str]] = [
    ("tokens", "current_price_usd", "REAL"),
    ("tokens", "current_mc_usd", "REAL"),
    ("tokens", "current_liq_usd", "REAL"),
    ("tokens", "current_volume_24h_usd", "REAL"),
    ("tokens", "current_buys_24h", "INTEGER"),
    ("tokens", "current_sells_24h", "INTEGER"),
    ("tokens", "current_price_change_5m_pct", "REAL"),
    ("tokens", "current_price_change_1h_pct", "REAL"),
    ("tokens", "current_price_change_6h_pct", "REAL"),
    ("tokens", "current_price_change_24h_pct", "REAL"),
    ("tokens", "dexscreener_paid_boost", "INTEGER NOT NULL DEFAULT 0"),
    ("price_snapshots", "price_change_5m_pct", "REAL"),
    ("price_snapshots", "price_change_1h_pct", "REAL"),
    ("price_snapshots", "price_change_6h_pct", "REAL"),
]


def _apply_migrations(sync_conn) -> None:
    """SQLite doesn't support 'ADD COLUMN IF NOT EXISTS' — check + add manually."""
    for table, col, ctype in _NEW_COLUMNS:
        try:
            existing = {
                row[1]
                for row in sync_conn.exec_driver_sql(f"PRAGMA table_info({table})")
            }
        except Exception:
            continue
        if col not in existing:
            try:
                sync_conn.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN {col} {ctype}"
                )
            except Exception:
                pass
    _backfill_dexscreener_paid_boost(sync_conn)


def _backfill_dexscreener_paid_boost(sync_conn) -> None:
    """Mark tokens whose first ingest was from DexScreener boost (paid promo) feeds."""
    try:
        rows = sync_conn.exec_driver_sql("PRAGMA table_info(tokens)").fetchall()
    except Exception:
        return
    if not rows or not any(row[1] == "dexscreener_paid_boost" for row in rows):
        return
    try:
        sync_conn.exec_driver_sql(
            "UPDATE tokens SET dexscreener_paid_boost = 1 "
            "WHERE discovery_source IN ("
            "'dexscreener_token_boosts_latest', 'dexscreener_token_boosts_top'"
            ")"
        )
    except Exception:
        pass


async def init_db() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_apply_migrations)

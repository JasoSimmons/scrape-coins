"""Config loader: merges config.yaml + environment variables."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DiscoveryCfg(BaseModel):
    sources: list[str]
    search_terms: list[str] = Field(default_factory=list)
    chain_id: str = "solana"


class SnapshotCfg(BaseModel):
    batch_size: int = 30
    min_minutes_between_snapshots: int = 50
    prune_after_days_inactive: int = 21


class EnrichmentCfg(BaseModel):
    holders_page_size: int = 1000
    holders_max_pages: int = 50
    refresh_minutes: int = 360
    dev_inactive_days: int = 7


class ClassifierCfg(BaseModel):
    peak_mc_min_usd: float
    peak_mc_max_usd: float
    peak_holders_min: int
    peak_top10_concentration_max: float
    peak_swaps_24h_min: int
    hours_above_50pct_peak_min: int
    require_lp_burned_or_locked: bool
    current_drawdown_from_ath_min: float
    current_volume_24h_max_usd: float
    hours_since_ath_min: int
    dev_wallet_inactive_days_min: int
    exclude_dev_dumped_pct_within_1h_of_peak: float
    exclude_peak_holders_below: int
    exclude_peak_top10_above: float


class IdeaScoreWeights(BaseModel):
    peak_holders: float
    holder_diversity: float
    volume_intensity: float
    sustain: float
    holder_retention: float
    time_to_peak: float
    social_presence: float
    ticker_quality: float


class IdeaScoreCfg(BaseModel):
    weights: IdeaScoreWeights
    peak_holders_log_min: float
    peak_holders_log_max: float
    volume_intensity_log_min: float
    volume_intensity_log_max: float


class SchedulerCfg(BaseModel):
    discovery_minutes: int = 60
    snapshot_minutes: int = 60
    enrichment_minutes: int = 60
    classify_minutes: int = 60


class HttpCfg(BaseModel):
    request_timeout_seconds: float = 20
    max_retries: int = 4
    retry_backoff_seconds: float = 1.5
    dexscreener_rps: float = 4
    helius_rps: float = 10


class DashboardFiltersCfg(BaseModel):
    """Default UI filters applied on `/` when no query-params override."""

    # Only show coins whose DexScreener pair is younger than this many hours,
    # based on Token.pair_created_at (best proxy for listing age).
    max_pair_age_hours: int = Field(default=48, ge=0)
    enabled: bool = True
    # If True and pair_created_at is NULL, fallback to discovery time.
    use_discovered_at_if_no_pair_created: bool = True


class AppConfig(BaseModel):
    discovery: DiscoveryCfg
    snapshot: SnapshotCfg
    enrichment: EnrichmentCfg
    classifier: ClassifierCfg
    idea_score: IdeaScoreCfg
    scheduler: SchedulerCfg
    http: HttpCfg
    dashboard_filters: DashboardFiltersCfg


class Env(BaseSettings):
    """Environment-only settings (secrets, paths, log level)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    helius_api_key: str = ""
    scrape_coins_config: str = "config.yaml"
    scrape_coins_db: str = "coins.db"
    web_host: str = "127.0.0.1"
    web_port: int = 8000
    log_level: str = "INFO"


def _load_yaml(path: str | os.PathLike[str]) -> dict[str, Any]:
    with open(path, "r") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=1)
def get_env() -> Env:
    return Env()


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    env = get_env()
    cfg_path = Path(env.scrape_coins_config)
    if not cfg_path.is_absolute():
        cfg_path = Path.cwd() / cfg_path
    raw = _load_yaml(cfg_path)
    return AppConfig.model_validate(raw)


def reload_config() -> AppConfig:
    """Force re-read of config.yaml (useful when editing thresholds at runtime)."""
    get_config.cache_clear()
    return get_config()

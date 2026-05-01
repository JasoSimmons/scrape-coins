"""APScheduler wiring — runs all four jobs on independent schedules from config.yaml."""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .classifier import run_classifier
from .config import get_config
from .logging_setup import get_logger
from .workers.discovery import run_discovery
from .workers.enrich import run_enrichment
from .workers.snapshot import prune_inactive, run_snapshot

log = get_logger(__name__)


async def _run_with_log(name: str, fn) -> None:
    log.info("scheduler.job_start", job=name)
    try:
        result = await fn()
        log.info("scheduler.job_done", job=name, result=result)
    except Exception as exc:  # noqa: BLE001
        log.error("scheduler.job_failed", job=name, error=str(exc))


def build_scheduler() -> AsyncIOScheduler:
    cfg = get_config().scheduler
    sched = AsyncIOScheduler()

    sched.add_job(
        _run_with_log,
        "interval",
        minutes=cfg.discovery_minutes,
        args=("discovery", run_discovery),
        id="discovery",
        max_instances=1,
        coalesce=True,
        next_run_time=_now(),
    )
    sched.add_job(
        _run_with_log,
        "interval",
        minutes=cfg.snapshot_minutes,
        args=("snapshot", run_snapshot),
        id="snapshot",
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        _run_with_log,
        "interval",
        minutes=cfg.enrichment_minutes,
        args=("enrichment", run_enrichment),
        id="enrichment",
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        _run_with_log,
        "interval",
        minutes=cfg.classify_minutes,
        args=("classify", run_classifier),
        id="classify",
        max_instances=1,
        coalesce=True,
    )
    # Daily prune at 03:17 UTC.
    sched.add_job(
        _run_with_log,
        "cron",
        hour=3,
        minute=17,
        args=("prune_inactive", prune_inactive),
        id="prune",
        max_instances=1,
        coalesce=True,
    )

    return sched


def _now():
    from datetime import datetime
    return datetime.utcnow()

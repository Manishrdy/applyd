from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_host: str = "0.0.0.0"
    app_port: int = 8000
    debug: bool = False
    log_level: str = "INFO"
    identity_service_url: str = "http://localhost:8100"

    db_path: Path = Path("./data/applyd.db")
    cache_dir: Path = Path("./cache/parquet")

    manifest_url: str = "https://storage.stapply.ai/jobhive/v1/manifest.json"
    http_timeout_seconds: int = 120
    download_concurrency: int = 4

    ingest_hour_utc: int = 11
    # If the daily 11:00 UTC run is skipped (manifest unchanged), run
    # lightweight catch-up checks every N minutes until this UTC hour.
    ingest_poll_interval_minutes: int = 30
    ingest_poll_end_hour_utc: int = 17
    # 30-day cap on COALESCE(posted_at, first_seen_at). Prune drops anything older.
    # Storage matches UI: what we keep on disk = what users can query. No buffer.
    rolling_window_days: int = 30
    ingest_batch_size: int = 10_000
    # Optional Redis cache for hot/read-heavy API responses.
    redis_cache_enabled: bool = False
    redis_url: str | None = None
    redis_cache_ttl_seconds: int = 90
    # SQLite space reclaim policy. DELETE frees logical space; VACUUM rewrites
    # the file to return unused pages to disk. Guard with threshold+cadence.
    db_vacuum_enabled: bool = True
    db_vacuum_min_rows_pruned: int = 10_000
    db_vacuum_min_interval_hours: int = 168  # weekly

    default_country: str = "US"
    default_posted_hours: int = 24
    default_page_size: int = 50
    max_page_size: int = 100

    # Local-scraper module (manual-trigger pipeline; see /scrape page).
    # Daily jobhive cron is unaffected by these.
    local_scraper_enabled: bool = True
    # Allow-list: empty means "no restriction" — every ATS with a vendored
    # companies CSV is selectable. Heavy ATS (Playwright-backed) will fail
    # at runtime without the scrapers extra installed in the vendor venv;
    # that's a clear error per-run, not a silent skip.
    local_scraper_allowed_ats: list[str] = []
    # Pre-selected on page load when allow-list is empty.
    local_scraper_recommended_ats: list[str] = ["lever", "ashby", "greenhouse"]
    # Hard cap on how many ATS one run can target. Sequential per ATS means
    # 5 fast ATS ≈ minutes, 5 slow ATS ≈ hours.
    local_scraper_max_ats_per_run: int = 5
    local_scraper_timeout_seconds: int = 1800   # per-ATS hard timeout
    local_scraper_default_max_companies: int | None = 500  # bound applied if user doesn't override
    local_scraper_default_incremental_days: int = 7
    # How many companies within ONE ATS can be scraped in parallel inside
    # the shim. Sequential per ATS is still enforced; this only parallelizes
    # the per-company HTTP loop. 8 is a reasonable default for most ATS
    # endpoints — drop to 1 if a target rate-limits or behaves oddly.
    local_scraper_per_company_concurrency: int = 8
    scraper_log_retention_days: int = 14
    scrape_run_history_keep: int = 100


settings = Settings()
settings.db_path.parent.mkdir(parents=True, exist_ok=True)
settings.cache_dir.mkdir(parents=True, exist_ok=True)

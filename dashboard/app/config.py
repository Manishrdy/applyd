from pathlib import Path

from pydantic import field_validator
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

    # In-process identity/auth settings.
    session_cookie_name: str = "applyd_session"
    session_ttl_days: int = 14
    session_cookie_secure: bool = False
    session_cookie_samesite: str = "lax"
    session_cookie_domain: str | None = None
    session_cookie_max_age_seconds: int | None = None
    csrf_cookie_name: str = "applyd_csrf"
    csrf_cookie_secure: bool = False
    csrf_cookie_samesite: str = "strict"
    redirect_allow_hosts: str = "localhost:8000,127.0.0.1:8000,0.0.0.0:8000"
    auth_rate_limit_window_seconds: int = 300
    auth_rate_limit_max_attempts: int = 5
    auth_rate_limit_lockout_seconds: int = 600
    auth_rate_limit_email_max_attempts: int = 15
    auth_rate_limit_ip_max_attempts: int = 30
    auth_signup_ip_max_attempts: int = 10
    trusted_proxy_hops: int = 0
    argon2_time_cost: int = 3
    argon2_memory_cost_kib: int = 65536
    argon2_parallelism: int = 4
    argon2_hash_len: int = 32
    argon2_salt_len: int = 16
    password_pepper: str = "dev-only-change-me-in-production"
    # One-time import source for legacy identity DB into applyd.db.
    identity_legacy_db_path: Path = Path("./data/legacy/identity.db")

    # --- Expired-job detection lifecycle ----------------------------------
    # Global kill switch. When False, no lifecycle transitions happen at all:
    # reports are still accepted (so user signal isn't lost), but the
    # state machine doesn't promote anything.
    expired_detection_enabled: bool = True
    # When False, the lifecycle records signals and the verifier writes to
    # job_verification_log, but no jobs.verification_status='expired' writes
    # happen. Stays False for the first ~2 weeks in production so we can
    # eyeball matcher false-positive rates before auto-hiding anything.
    verifier_auto_marking_enabled: bool = False
    # How often to re-check every active job. With ~500k active corpus over
    # 1 day that's ~6 req/sec globally; bump to 3 or 5 if per-host rate-limits
    # start firing. 1 day default = every job hit every day.
    verifier_sweep_days: int = 1
    # When True (default), the periodic sweep does NOT filter by
    # last_verified_at — it picks the oldest-checked jobs first and walks
    # the entire active corpus continuously. Set False to fall back to
    # "only check jobs older than verifier_sweep_days."
    verifier_sweep_all_active: bool = True
    # Per-host concurrency cap for the verifier. Mirrors download_concurrency.
    verifier_per_host_concurrency: int = 4
    # Global concurrency cap across all hosts.
    verifier_global_concurrency: int = 16
    # HTTP timeout for a single verifier check.
    verifier_request_timeout_seconds: int = 20
    # Suspected pool drain cadence (event-driven verifications).
    verifier_suspected_interval_minutes: int = 5
    # Periodic sweep tick cadence. Lower = more frequent, smaller batches
    # → more visible activity in the UI. Sweep batch size is auto-computed
    # so the full active corpus is still covered every verifier_sweep_days.
    verifier_sweep_interval_minutes: int = 10
    # Max suspected jobs verified per drain tick.
    verifier_suspected_batch: int = 50
    # Periodic-sweep batch size per hour (auto-computed by default: corpus /
    # (sweep_days * 24)). Override here to clamp manually.
    verifier_sweep_batch_size: int | None = None
    # Circuit breaker: if a single ATS produces >this many auto-expirations
    # in one hour bucket, halt 'expired' writes for that ATS.
    verifier_circuit_breaker_threshold: int = 25
    # Per-user report rate limits.
    report_rate_limit_per_day: int = 20
    report_rate_limit_per_company_per_week: int = 5

    @field_validator("verifier_sweep_batch_size", mode="before")
    @classmethod
    def _blank_sweep_batch_size_to_none(cls, value: object) -> object:
        if isinstance(value, str) and value.strip() == "":
            return None
        return value


settings = Settings()
settings.db_path.parent.mkdir(parents=True, exist_ok=True)
settings.cache_dir.mkdir(parents=True, exist_ok=True)

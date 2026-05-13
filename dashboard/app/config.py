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

    db_path: Path = Path("./data/applyd.db")
    cache_dir: Path = Path("./cache/parquet")

    manifest_url: str = "https://storage.stapply.ai/jobhive/v1/manifest.json"
    http_timeout_seconds: int = 120
    download_concurrency: int = 4

    ingest_hour_utc: int = 11
    # 30-day cap on COALESCE(posted_at, first_seen_at). Prune drops anything older.
    # Storage matches UI: what we keep on disk = what users can query. No buffer.
    rolling_window_days: int = 30
    ingest_batch_size: int = 10_000

    default_country: str = "US"
    default_posted_hours: int = 24
    default_page_size: int = 50
    max_page_size: int = 100


settings = Settings()
settings.db_path.parent.mkdir(parents=True, exist_ok=True)
settings.cache_dir.mkdir(parents=True, exist_ok=True)

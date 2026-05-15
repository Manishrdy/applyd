from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_host: str = "0.0.0.0"
    app_port: int = 8100
    db_path: Path = Path("./data/identity.db")
    # Python logging level — DEBUG/INFO/WARNING/ERROR. Override via env.
    log_level: str = "INFO"
    session_cookie_name: str = "applyd_session"
    session_ttl_days: int = 14
    session_cookie_secure: bool = False
    session_cookie_samesite: str = "lax"
    session_cookie_domain: str | None = None
    session_cookie_max_age_seconds: int | None = None

    csrf_cookie_name: str = "applyd_csrf"
    csrf_cookie_secure: bool = False
    # Strict by default — the CSRF cookie is only read by same-origin JS on
    # our auth pages, so it never needs to ride along on a cross-site nav.
    csrf_cookie_samesite: str = "strict"

    redirect_allow_hosts: str = "localhost:8000,127.0.0.1:8000"
    # Per (IP, email) — quickest trip. Catches a focused attacker.
    auth_rate_limit_window_seconds: int = 300
    auth_rate_limit_max_attempts: int = 5
    auth_rate_limit_lockout_seconds: int = 600
    # Per email across all IPs — catches distributed credential stuffing.
    # Higher threshold so a single misclick from a new IP doesn't lock the
    # real owner out everywhere.
    auth_rate_limit_email_max_attempts: int = 15
    # Per IP across all emails — catches an attacker rotating emails.
    auth_rate_limit_ip_max_attempts: int = 30
    # Per IP cap on /signup — stops mass-signup / enumeration probes.
    auth_signup_ip_max_attempts: int = 10

    # When > 0, resolve client IP from X-Forwarded-For (right-trusted hops).
    # Set to the number of reverse proxies in front of this app that append to XFF.
    trusted_proxy_hops: int = 0

    # ---- Password hashing ----------------------------------------------
    # Argon2id parameters (OWASP-recommended baseline; tune for the host).
    # 64 MiB memory, 3 iterations, parallelism 4. Cost ≈ 50–150 ms.
    argon2_time_cost: int = 3
    argon2_memory_cost_kib: int = 65536
    argon2_parallelism: int = 4
    argon2_hash_len: int = 32
    argon2_salt_len: int = 16
    # Server-side pepper. Hashes become useless if only the DB leaks. Keep
    # this OUT of the database and rotate by adding "pepper:v2:<bytes>" and
    # leaving the old "pepper:v1:<bytes>" entry below until all hashes are
    # rotated through verify_password's rehash path.
    password_pepper: str = "dev-only-change-me-in-production"


settings = Settings()
settings.db_path.parent.mkdir(parents=True, exist_ok=True)

"""Application settings (12-factor, loaded from environment / .env file)."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Locales verified against the public ReelShort build manifest rewrites
# (https://www.reelshort.com/_next/static/<buildId>/_buildManifest.js).
SUPPORTED_LOCALES: tuple[str, ...] = (
    "en", "vi", "th", "es", "fr", "de", "ja", "ko", "pt", "ru", "id",
    "zh-TW", "ar", "pl", "it", "tr", "ro", "cs", "bg", "tl", "zh",
)

# Locales the discovery engine walks by default (bounded, cheapest first).
DEFAULT_DISCOVERY_LOCALES: tuple[str, ...] = (
    "vi", "en", "es", "pt", "th", "id", "fr", "de", "ja", "ko", "ru", "ar",
)

# Fixed locale to homepage URL mapping for catalog discovery.
REELSHORT_LOCALES: dict[str, str] = {
    "vi": "https://www.reelshort.com/vi",
    "en": "https://www.reelshort.com",
    "pt": "https://www.reelshort.com/pt",
    "es": "https://www.reelshort.com/es",
    "ja": "https://www.reelshort.com/ja",
    "ko": "https://www.reelshort.com/ko",
    "zh-TW": "https://www.reelshort.com/zh-TW",
    "fr": "https://www.reelshort.com/fr",
}


# Verified per-locale full catalog pagination URL templates (Task 1 & Task 6).
REELSHORT_CATALOG_PAGE_URLS: dict[str, str] = {
    "en": "https://www.reelshort.com/movie-genres/all-movies/{page}",
    "vi": "https://www.reelshort.com/vi/movie-genres/all-movies/{page}",
    "pt": "https://www.reelshort.com/pt/movie-genres/all-movies/{page}",
    "es": "https://www.reelshort.com/es/movie-genres/all-movies/{page}",
    "ja": "https://www.reelshort.com/ja/movie-genres/all-movies/{page}",
    "ko": "https://www.reelshort.com/ko/movie-genres/all-movies/{page}",
    "zh-TW": "https://www.reelshort.com/zh-TW/movie-genres/all-movies/{page}",
    "zh-tw": "https://www.reelshort.com/zh-TW/movie-genres/all-movies/{page}",
    "fr": "https://www.reelshort.com/fr/movie-genres/all-movies/{page}",
}


def get_locale_source_url(lang: str, base_url: str = "https://www.reelshort.com") -> str:
    clean = lang.strip().lower()
    for k, url in REELSHORT_LOCALES.items():
        if k.lower() == clean:
            return url.replace("https://www.reelshort.com", base_url.rstrip("/"))
    if clean == "en":
        return base_url.rstrip("/")
    return f"{base_url.rstrip('/')}/{clean}"


def get_locale_catalog_page_url(
    lang: str, page: int = 1, base_url: str = "https://www.reelshort.com"
) -> str:
    clean = lang.strip().lower()
    page_num = max(1, int(page))
    for k, template in REELSHORT_CATALOG_PAGE_URLS.items():
        if k.lower() == clean:
            url = template.format(page=page_num)
            return url.replace("https://www.reelshort.com", base_url.rstrip("/"))
    if clean == "en":
        return f"{base_url.rstrip('/')}/movie-genres/all-movies/{page_num}"
    locale_segment = "zh-TW" if clean == "zh-tw" else clean
    return f"{base_url.rstrip('/')}/{locale_segment}/movie-genres/all-movies/{page_num}"



class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # Comma-separated list fields are parsed by our own validators below;
        # disable pydantic-settings' JSON decoding so plain CSV works too.
        enable_decoding=False,
    )

    # --- service -----------------------------------------------------------
    app_name: str = "toolnet-reelshort-api"
    app_version: str = "1.0.0"
    environment: str = "production"
    host: str = "0.0.0.0"
    port: int = 8077
    log_level: str = "INFO"
    log_file: str | None = "/srv/logs/reelshort-api.log"
    docs_enabled: bool = True

    # --- storage -----------------------------------------------------------
    db_path: str = "/srv/data/reelshort.sqlite3"
    data_dir: str = "/srv/data"
    backup_dir: str = "/srv/data/backups"
    backup_keep: int = 7
    sqlite_busy_timeout_ms: int = 15000
    sqlite_cache_kib: int = 64000
    turso_database_url: str | None = None
    turso_auth_token: str | None = None

    # --- auth --------------------------------------------------------------
    # Public API keys (comma separated). Empty list + public_auth_enabled=false
    # means "loopback only, anonymous allowed" (default, never public).
    public_api_keys: list[str] = Field(default_factory=list)
    public_auth_enabled: bool = False
    admin_username: str = "admin"
    admin_password: str = ""
    admin_token: str = ""
    admin_auth_enabled: bool = True
    trust_proxy_headers: bool = True

    # --- crawling ----------------------------------------------------------
    source_base_url: str = "https://www.reelshort.com"
    source_timeout_seconds: float = 25.0
    source_max_retries: int = 3
    source_backoff_base_seconds: float = 1.5
    source_backoff_max_seconds: float = 60.0
    source_requests_per_minute: float = 20.0   # global outbound budget
    source_burst: float = 4.0
    source_concurrency: int = 2                # bounded parallel requests
    source_cooldown_on_429_seconds: float = 300.0
    source_cooldown_on_403_seconds: float = 900.0
    source_breaker_error_threshold: int = 5
    respect_robots_txt: bool = True
    robots_cache_seconds: float = 3600.0
    user_agent: str = (
        "ToolNetReelShortMetadataBot/1.0 (+https://toolnet.tech; metadata-only; contact: ops@toolnet.tech)"
    )

    # --- cache / freshness -------------------------------------------------
    metadata_ttl_hours: float = 24.0
    catalog_ttl_hours: float = 12.0
    search_cache_ttl_minutes: float = 30.0
    edition_ttl_hours: float = 72.0
    stale_while_revalidate: bool = True

    # --- workers -----------------------------------------------------------
    worker_concurrency: int = 2
    worker_poll_seconds: float = 0.5
    job_max_attempts: int = 3
    job_timeout_seconds: float = 90.0
    # Discovery walks several locales (searches + bounded catalogue scan +
    # candidate metadata) under a polite outbound budget, so it needs a
    # larger per-job budget than a single movie crawl.
    discovery_job_timeout_seconds: float = 900.0
    catalog_job_timeout_seconds: float = 7200.0
    queue_max_size: int = 500
    shutdown_grace_seconds: float = 15.0
    scheduler_enabled: bool = True
    scheduler_interval_seconds: float = 300.0
    scheduler_batch_size: int = 10
    scheduler_min_age_hours: float = 24.0
    auto_sync_enabled: bool = True
    auto_sync_locales: list[str] = Field(default_factory=lambda: ["en", "vi"])
    enrichment_batch_size: int = 5

    # --- inbound limits ----------------------------------------------------
    api_rate_limit_per_minute: int = 120
    api_rate_limit_burst: int = 30
    discover_rate_limit_per_hour: int = 12
    # Authenticated admin crawls; the real protection is the global outbound
    # budget (source_requests_per_minute), so keep this roomy for bulk indexing.
    crawl_rate_limit_per_hour: int = 300
    max_page_size: int = 100
    default_page_size: int = 20

    # --- discovery ---------------------------------------------------------
    discovery_locales: list[str] = Field(default_factory=lambda: list(DEFAULT_DISCOVERY_LOCALES))
    discovery_max_locales_per_run: int = 6
    discovery_search_pages: int = 2
    discovery_tag_scan_pages: int = 3
    discovery_tag_scan_page_size: int = 50
    discovery_max_candidates: int = 12
    # Candidates below this cheap pre-score are never fetched upstream.
    discovery_candidate_min_score: float = 0.35
    # ...and they must carry at least one identity signal first: an exact
    # t_book_id, a matching t_book_id tail, an official dub relation with equal
    # episode count, or a title match at least this similar.
    discovery_candidate_min_title_similarity: float = 0.5
    discovery_min_confidence: float = 0.5
    # Auto-verification additionally requires deterministic evidence
    # (exact t_book_id, or official dub relation + identical tag-id set +
    # identical episode count + matching t_book_id tail) — see scoring.py.
    discovery_auto_verify_confidence: float = 0.8
    # The official catalogue listing endpoint requires a tag id argument
    # (upstream ignores it and returns the paginated per-locale catalogue).
    catalog_tag_id: str = "6348f5093c6ca761764d85f3"

    @field_validator("public_api_keys", "discovery_locales", "auto_sync_locales", mode="before")
    @classmethod
    def _split_list(cls, value: Any) -> Any:
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith("["):
                return json.loads(raw)
            return [item.strip() for item in raw.split(",") if item.strip()]
        return value

    @field_validator("discovery_locales")
    @classmethod
    def _validate_locales(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            lang = item.strip()
            if not lang:
                continue
            if lang not in SUPPORTED_LOCALES:
                raise ValueError(f"unsupported locale: {lang}")
            cleaned.append(lang)
        return cleaned

    @property
    def is_turso(self) -> bool:
        return bool(self.turso_database_url and self.turso_database_url.strip())

    @property
    def database_url(self) -> str:
        if self.is_turso:
            clean = (self.turso_database_url or "").strip()
            if not clean.startswith("sqlite+"):
                return f"sqlite+{clean}?secure=true"
            return clean
        return f"sqlite+pysqlite:///{self.db_path}"

    @property
    def admin_configured(self) -> bool:
        return bool(self.admin_password or self.admin_token)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

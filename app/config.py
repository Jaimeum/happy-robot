from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    tms_host: str = "localhost"
    tms_port: int = 9000
    tms_token: str = ""
    tms_timeout_seconds: float = 10.0
    # Measured LOAD_QUERY is ~197ms median, ~240ms p95. A read that inherits the
    # booking timeout can hold a live voice call silent for ten seconds, so reads
    # get their own budget. LOAD_BOOK keeps the long one: a slow commit beats an
    # uncertain one.
    tms_query_timeout_seconds: float = 3.0
    tms_max_retries: int = 3

    # ── Load board snapshot ───────────────────────────────────────
    board_refresh_interval_seconds: int = 60
    board_snapshot_ttl_seconds: int = 90
    board_stale_after_seconds: int = 600
    board_shard_max_results: int = 100
    # Which wire STATUS values may be pitched to a carrier. Config-driven so a
    # bookable-but-unseen status is one env var rather than a code change.
    board_offerable_statuses: str = "OPEN"
    # A hard stop on searching within one call. After this many the whole option
    # set is handed over instead, because on a ~50-load board there is nothing
    # left to discover.
    max_searches_per_call: int = 4

    fmcsa_api_key: str = ""
    fmcsa_base_url: str = "https://mobile.fmcsa.dot.gov/qc/services"
    fmcsa_timeout_seconds: float = 10.0

    api_auth_token: str = ""

    happyrobot_api_key: str = ""
    happyrobot_org_id: str = ""

    max_negotiation_rounds: int = 3
    # Our counters never come within this fraction of the ceiling, so the ceiling
    # itself is never named on the wire even after three rounds of probing.
    negotiation_ceiling_buffer_pct: float = 0.03
    # A carrier ask this close to our standing offer is taken rather than haggled over.
    negotiation_auto_accept_pct: float = 0.02

    otp_ttl_seconds: int = 300
    otp_max_attempts: int = 3
    otp_length: int = 6

    session_ttl_seconds: int = 3600
    audit_buffer_size: int = 5000

    app_port: int = 8000
    log_level: str = "INFO"
    env: str = "local"

    @property
    def is_production(self) -> bool:
        return self.env.lower() in {"prod", "production"}

    @property
    def offerable_statuses(self) -> frozenset[str]:
        return frozenset(
            part.strip().upper()
            for part in self.board_offerable_statuses.split(",")
            if part.strip()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()

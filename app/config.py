from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    tms_host: str = "localhost"
    tms_port: int = 9000
    tms_token: str = ""
    tms_timeout_seconds: float = 10.0
    tms_max_retries: int = 3

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


@lru_cache
def get_settings() -> Settings:
    return Settings()

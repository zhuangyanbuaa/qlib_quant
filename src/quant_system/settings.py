"""Environment-backed application settings."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Configuration shared by CLI commands and scheduled jobs."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_prefix="QUANT_",
        extra="ignore",
    )

    app_env: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    data_dir: Path = Path("data")
    alpha_vantage_api_key: str | None = None
    sec_user_agent: str = "qlib_quant/0.1 contact@example.com"

    @property
    def resolved_data_dir(self) -> Path:
        """Return an absolute runtime data directory."""
        if self.data_dir.is_absolute():
            return self.data_dir
        return PROJECT_ROOT / self.data_dir

    def public_config(self) -> dict[str, str]:
        """Return the safe subset suitable for terminal output and logs."""
        return {
            "app_env": self.app_env,
            "data_dir": str(self.resolved_data_dir),
            "log_level": self.log_level,
            "alpha_vantage_api_key": "***" if self.alpha_vantage_api_key else "",
            "sec_user_agent_configured": str(bool(self.sec_user_agent)),
        }


@lru_cache
def get_settings() -> Settings:
    """Load and cache settings for the current process."""
    return Settings()

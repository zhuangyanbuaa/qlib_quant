"""Environment-backed application settings."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
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
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    alpaca_api_key: SecretStr | None = None
    alpaca_secret_key: SecretStr | None = None

    @property
    def telegram_configured(self) -> bool:
        import re

        token = self.telegram_bot_token.get_secret_value() if self.telegram_bot_token else ""
        return bool(
            re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token)
            and self.telegram_chat_id
            and re.fullmatch(r"-?\d+", self.telegram_chat_id)
        )

    @property
    def alpha_vantage_key_configured(self) -> bool:
        return bool(
            self.alpha_vantage_api_key
            and self.alpha_vantage_api_key.strip().lower()
            not in {"demo", "your_api_key", "placeholder"}
        )

    @property
    def sec_contact_configured(self) -> bool:
        import re

        return bool(
            self.sec_user_agent
            and "example.com" not in self.sec_user_agent.lower()
            and re.search(r"[^\s@]+@[^\s@]+\.[^\s@]+", self.sec_user_agent)
        )

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
            "sec_user_agent_configured": str(self.sec_contact_configured),
            "telegram_configured": str(self.telegram_configured),
        }


@lru_cache
def get_settings() -> Settings:
    """Load and cache settings for the current process."""
    return Settings()

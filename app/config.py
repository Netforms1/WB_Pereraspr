from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    tg_bot_token: str
    tg_owner_id: int
    session_encryption_key: str

    db_path: Path = Path("./data/bot.db")

    daily_run_hour: int = 9
    daily_run_minute: int = 0
    attack_window_seconds: int = 1800
    attack_retry_seconds: int = 3
    warehouses_refresh_seconds: int = 300

    log_level: str = "INFO"


settings = Settings()

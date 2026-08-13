from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # `model_config`, not a nested `class Config`: the class-based form is
    # deprecated in pydantic v2 and removed in v3, so it would turn the next
    # pydantic bump into a hard failure at import time — which is app startup,
    # before any handler runs. Behaviour is unchanged (tests/test_config.py
    # pins the env_file, the case-insensitive `TZ` match and `extra=ignore`).
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    tz: str = "UTC"


settings = Settings()

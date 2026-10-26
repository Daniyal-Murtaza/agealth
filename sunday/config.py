"""Runtime settings, from the environment or ``.env``."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    provider: Literal["openai", "fake"] = Field(default="openai", alias="SUNDAY_PROVIDER")
    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4.1-mini", alias="OPENAI_MODEL")
    model_timeout_s: float = Field(default=20.0, alias="SUNDAY_MODEL_TIMEOUT_S")

    twilio_account_sid: str | None = Field(default=None, alias="TWILIO_ACCOUNT_SID")
    twilio_auth_token: SecretStr | None = Field(default=None, alias="TWILIO_AUTH_TOKEN")
    twilio_from_number: str | None = Field(default=None, alias="TWILIO_FROM_NUMBER")
    twilio_whatsapp_from: str | None = Field(default=None, alias="TWILIO_WHATSAPP_FROM")
    public_url: str | None = Field(default=None, alias="SUNDAY_PUBLIC_URL")

    db_path: Path = Field(default=Path("./data/sunday.sqlite"), alias="SUNDAY_DB")
    log_level: str = Field(default="INFO", alias="SUNDAY_LOG_LEVEL")
    max_call_minutes: int = Field(default=12, ge=2, le=30, alias="SUNDAY_MAX_CALL_MINUTES")
    max_turns: int = Field(default=40, ge=4, alias="SUNDAY_MAX_TURNS")

    def require_openai(self) -> str:
        if self.openai_api_key is None:
            raise MissingCredentialError("OPENAI_API_KEY")
        return self.openai_api_key.get_secret_value()

    def twilio_ready(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token and self.twilio_from_number)


class MissingCredentialError(RuntimeError):
    def __init__(self, name: str) -> None:
        super().__init__(f"{name} is not set. Copy .env.example to .env and fill it in.")
        self.name = name


def load_settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]

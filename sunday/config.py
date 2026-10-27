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
    # Twilio signs every webhook with the auth token. Verification is on by default and
    # fails closed (no token = every webhook rejected). Tests turn it off explicitly.
    twilio_verify_signatures: bool = Field(default=True, alias="SUNDAY_TWILIO_VERIFY_SIGNATURES")

    # Family web app. The secret signs login links and session cookies; without it the
    # server refuses to start. Generate one with:
    #   python -c "import secrets; print(secrets.token_urlsafe(32))"
    secret_key: SecretStr | None = Field(default=None, alias="SUNDAY_SECRET_KEY")
    # Hosted demo: seed the demo family on start, and offer a "See a family's page" button
    # that signs the visitor in as the demo parent's son. The session it creates is for the
    # demo phone only, so it can never reach a real family; real families still sign in by
    # one-time link.
    seed_demo: bool = Field(default=False, alias="SUNDAY_SEED_DEMO")
    demo_login: bool = Field(default=False, alias="SUNDAY_DEMO_LOGIN")
    # Set by Render / Vercel automatically; used when SUNDAY_PUBLIC_URL is not given.
    render_external_url: str | None = Field(default=None, alias="RENDER_EXTERNAL_URL")
    vercel_production_url: str | None = Field(default=None, alias="VERCEL_PROJECT_PRODUCTION_URL")
    vercel_url: str | None = Field(default=None, alias="VERCEL_URL")
    login_link_minutes: int = Field(default=15, ge=1, le=120, alias="SUNDAY_LOGIN_LINK_MINUTES")
    session_days: int = Field(default=30, ge=1, le=365, alias="SUNDAY_SESSION_DAYS")

    db_path: Path = Field(default=Path("./data/sunday.sqlite"), alias="SUNDAY_DB")
    log_level: str = Field(default="INFO", alias="SUNDAY_LOG_LEVEL")
    max_call_minutes: int = Field(default=12, ge=2, le=30, alias="SUNDAY_MAX_CALL_MINUTES")
    max_turns: int = Field(default=40, ge=4, alias="SUNDAY_MAX_TURNS")

    def require_openai(self) -> str:
        if self.openai_api_key is None:
            raise MissingCredentialError("OPENAI_API_KEY")
        return self.openai_api_key.get_secret_value()

    def model_post_init(self, __context) -> None:
        if not self.public_url and self.render_external_url:
            self.public_url = self.render_external_url.rstrip("/")
        host = self.vercel_production_url or self.vercel_url
        if not self.public_url and host:
            self.public_url = "https://" + host.replace("https://", "").rstrip("/")

    def require_secret_key(self) -> str:
        if self.secret_key is None or not self.secret_key.get_secret_value().strip():
            raise MissingCredentialError("SUNDAY_SECRET_KEY")
        return self.secret_key.get_secret_value()

    def twilio_ready(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token and self.twilio_from_number)


class MissingCredentialError(RuntimeError):
    def __init__(self, name: str) -> None:
        super().__init__(f"{name} is not set. Copy .env.example to .env and fill it in.")
        self.name = name


def load_settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]

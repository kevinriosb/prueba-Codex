from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", hide_input_in_errors=True,
    )

    app_env: str = "development"
    app_login_username: str = Field(default="admin", min_length=3, max_length=64)
    app_login_password: SecretStr
    app_secure_cookies: bool = False
    app_allowed_hosts: str = "localhost,127.0.0.1,[::1]"
    app_database_path: Path = Path("data/agent.sqlite3")
    app_session_hours: int = Field(default=8, ge=1, le=24)
    host: str = Field(default='127.0.0.1', min_length=1, max_length=255)
    port: int = Field(default=8000, ge=1, le=65535)
    forwarded_allow_ips: str = '127.0.0.1'
    ai_api_key: SecretStr = SecretStr("")
    ai_model: str = Field(default="gpt-4.1-mini", min_length=1, max_length=100)
    server_timezone: str = "America/Mexico_City"
    max_input_chars: int = Field(default=4000, ge=100, le=8000)
    max_context_bytes: int = Field(default=16000, ge=4000, le=48000)
    max_output_tokens: int = Field(default=150, ge=100, le=2000)
    max_agent_turns: int = Field(default=3, ge=1, le=6)
    max_tool_calls: int = Field(default=5, ge=1, le=10)
    run_timeout_seconds: int = Field(default=35, ge=5, le=90)
    tool_timeout_seconds: int = Field(default=3, ge=1, le=10)
    max_daily_runs: int = Field(default=30, ge=1, le=200)
    max_daily_tokens: int = Field(default=250000, ge=1000, le=2000000)
    max_conversation_turns: int = Field(default=20, ge=1, le=50)
    max_conversations: int = Field(default=30, ge=1, le=100)
    approval_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    billing_encryption_key: SecretStr = SecretStr('')
    billing_max_upload_bytes: int = Field(default=8_388_608, ge=1024, le=12_582_912)
    billing_document_retention_hours: int = Field(default=24, ge=1, le=168)
    billing_invoice_retention_days: int = Field(default=30, ge=1, le=90)
    billing_max_documents: int = Field(default=40, ge=1, le=100)
    billing_max_profiles: int = Field(default=10, ge=1, le=20)
    billing_max_daily_extractions: int = Field(default=10, ge=1, le=30)
    billing_max_daily_telegram_updates: int = Field(default=100, ge=10, le=500)
    billing_extraction_max_tokens: int = Field(default=1400, ge=600, le=2000)
    billing_live_email_enabled: bool = False
    billing_sat_enabled: bool = False
    billing_verified_contacts_json: str = Field(default='[]',max_length=20_000)
    billing_portal_inspection_urls: str = Field(default='https://f.zetus.mx/albat',max_length=4000)
    billing_portal_inspection_timeout_seconds: int = Field(default=7,ge=1,le=15)
    billing_background_tracking_enabled: bool = False
    billing_tracking_interval_seconds: int = Field(default=3600,ge=300,le=86400)
    billing_tracking_max_checks: int = Field(default=72,ge=1,le=100)
    gmail_client_id: str = ''
    gmail_client_secret: SecretStr = SecretStr('')
    gmail_redirect_uri: str = ''
    telegram_bot_token: SecretStr = SecretStr('')
    telegram_webhook_secret: SecretStr = SecretStr('')
    telegram_allowed_user_ids: str = ''
    telegram_reply_max_chars: int = Field(default=160, ge=50, le=500)

    @field_validator("app_login_password")
    @classmethod
    def password_length(cls, value: SecretStr) -> SecretStr:
        if not 12 <= len(value.get_secret_value()) <= 256:
            raise ValueError("APP_LOGIN_PASSWORD debe tener entre 12 y 256 caracteres")
        return value

    @field_validator("server_timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError("SERVER_TIMEZONE no es una zona horaria válida") from None
        return value

    @model_validator(mode="after")
    def production_security(self):
        if self.app_env not in {"development", "production"}:
            raise ValueError("APP_ENV debe ser development o production")
        if "*" in self.hosts:
            raise ValueError("APP_ALLOWED_HOSTS requiere dominios concretos, sin comodines")
        if not self.hosts or any('/' in host for host in self.hosts):
            raise ValueError('APP_ALLOWED_HOSTS requiere al menos un hostname sin esquema ni rutas')
        if self.app_env == "production" and not self.app_secure_cookies:
            raise ValueError("En producción APP_SECURE_COOKIES debe ser true")
        return self

    @property
    def hosts(self) -> list[str]:
        return [host.strip() for host in self.app_allowed_hosts.split(",") if host.strip()]

    @property
    def token_reservation(self) -> int:
        # Reserva conservadora de admisión; no equivale a un límite de dólares del proveedor.
        return (self.max_context_bytes + 8000 + self.max_output_tokens * 8 * self.max_agent_turns) * self.max_agent_turns + self.max_output_tokens * self.max_agent_turns

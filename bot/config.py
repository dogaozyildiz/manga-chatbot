from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, from the environment or a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # The RAG backend. Everything intelligent happens behind this URL.
    rag_url: str = "http://127.0.0.1:8000"
    # Generous, because a `both` route against a 70B model can take several
    # seconds. Still finite: a user staring at a silent chat needs an answer or
    # an apology, not an open connection.
    rag_timeout_seconds: float = 45.0
    # None means "let the backend choose" (its top_k, currently 5).
    rag_top_k: int | None = None

    telegram_bot_token: str = ""

    # Where escalations are pushed. Empty is valid and means "record only" —
    # the bot must not require an operator to be configured in order to run.
    admin_chat_id: str = ""

    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_api_version: str = "v21.0"

    database_url: str = "sqlite+aiosqlite:///./manga_chatbot.db"

    # The backend has no auth and no rate limiting of its own, and says so:
    # "Do not deploy publicly with your own API key attached." The bot is the
    # public surface, so the limiter belongs here.
    rate_limit_min_seconds: float = 3.0
    rate_limit_per_hour: int = 20

    # Telegram and WhatsApp both cap a text message at 4096 characters.
    max_message_chars: int = 4096

    @property
    def whatsapp_configured(self) -> bool:
        return bool(
            self.whatsapp_token
            and self.whatsapp_phone_number_id
            and self.whatsapp_verify_token
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()

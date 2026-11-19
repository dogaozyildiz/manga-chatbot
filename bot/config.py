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
    # Meta signs every webhook POST with this (X-Hub-Signature-256). Without it
    # anyone who learns the URL can post messages as any phone number.
    whatsapp_app_secret: str = ""
    whatsapp_api_version: str = "v21.0"

    discord_bot_token: str = ""
    # Commands registered to one guild appear instantly; global ones take up to
    # an hour to propagate. Set this while developing.
    discord_guild_id: str = ""

    # Reddit, for looking up real communities. Search needs only app-only
    # OAuth — no Reddit user account and no password.
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    # Reddit requires a descriptive User-Agent and forbids masking it.
    reddit_user_agent: str = "python:manga-chatbot:v0.1 (by /u/unknown)"

    database_url: str = "sqlite+aiosqlite:///./manga_chatbot.db"

    # The backend has no auth and no rate limiting of its own, and says so:
    # "Do not deploy publicly with your own API key attached." The bot is the
    # public surface, so the limiter belongs here.
    rate_limit_min_seconds: float = 3.0
    rate_limit_per_hour: int = 20

    @property
    def whatsapp_configured(self) -> bool:
        return bool(
            self.whatsapp_token
            and self.whatsapp_phone_number_id
            and self.whatsapp_verify_token
            # Fail closed: no app secret, no webhook.
            and self.whatsapp_app_secret
        )

    @property
    def discord_configured(self) -> bool:
        return bool(self.discord_bot_token)

    @property
    def communities_configured(self) -> bool:
        return bool(self.reddit_client_id and self.reddit_client_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()

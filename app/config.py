"""Application settings, loaded from environment variables or a local ``.env`` file.

Every tunable (model names, limits, storage, security knobs) lives here so the rest of
the code never reads ``os.environ`` directly.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed runtime configuration."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Gemini -----------------------------------------------------------------------
    gemini_api_key: SecretStr = SecretStr("")
    gemini_chat_model: str = "gemini-2.5-flash"
    gemini_analysis_model: str = "gemini-2.5-flash"
    # Tried in order when a model is overloaded or out of quota (each model has its own quota).
    gemini_fallback_models: str = "gemini-2.5-flash-lite,gemini-3-flash-preview,gemini-3.1-flash-lite"
    gemini_transcribe_model: str = "gemini-2.5-flash"
    gemini_embedding_model: str = "gemini-embedding-001"
    embedding_dimensions: int = Field(default=768, ge=128, le=3072)
    chat_thinking_budget: int = Field(default=1024, ge=0)
    voice_thinking_budget: int = Field(default=0, ge=0)
    analysis_thinking_budget: int = Field(default=2048, ge=0)
    llm_timeout_seconds: int = Field(default=90, ge=10)

    # --- Retrieval --------------------------------------------------------------------
    chunk_size: int = Field(default=1200, ge=200)
    chunk_overlap: int = Field(default=200, ge=0)
    retrieval_top_k: int = Field(default=8, ge=1, le=30)
    # Below this many characters we send every chunk instead of retrieving a subset.
    full_context_char_limit: int = Field(default=40_000, ge=0)
    analysis_max_chars: int = Field(default=350_000, ge=10_000)
    history_turns: int = Field(default=10, ge=0, le=40)

    # --- Storage & limits -------------------------------------------------------------
    database_path: str = "data/lexiguide.db"
    max_upload_mb: int = Field(default=50, ge=1, le=200)
    max_documents_per_session: int = Field(default=20, ge=1)
    max_session_chars: int = Field(default=10_000_000, ge=10_000)
    session_ttl_days: int = Field(default=30, ge=1)
    ai_requests_per_minute: int = Field(default=30, ge=1)

    # --- HTTP -------------------------------------------------------------------------
    cookie_secure: bool | None = None  # None = secure whenever the request is HTTPS
    enable_api_docs: bool = True
    log_level: str = "INFO"

    @property
    def fallback_models(self) -> list[str]:
        return [name.strip() for name in self.gemini_fallback_models.split(",") if name.strip()]

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def session_ttl_seconds(self) -> int:
        return self.session_ttl_days * 24 * 60 * 60


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance (cached)."""
    return Settings()

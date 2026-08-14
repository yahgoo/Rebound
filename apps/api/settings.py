"""Application settings. Secrets never have defaults."""

from decimal import Decimal
from enum import StrEnum
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


# Mirrors INTERFACES.md §0 / router ModelBackend until those packages exist.
class ReboundMode(StrEnum):
    LIVE = "live"
    REPLAY = "replay"


class ExecutorKind(StrEnum):
    DAYTONA = "daytona"
    LOCAL = "local"


class ChaosProfile(StrEnum):
    NONE = "none"
    DECLINE = "decline"
    TIMEOUT = "timeout"
    THREE_DS = "3ds"


class Surface(StrEnum):
    OPERATOR = "operator"
    TRAVELLER = "traveller"


class ModelBackend(StrEnum):
    GEMINI = "gemini"
    GEMMA = "gemma"
    KIMI = "kimi"
    QWEN = "qwen"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Atlas (required for boot per Task 1 Done-when)
    atlas_base_url: str
    atlas_client_id: str
    atlas_client_secret: str

    # Guardian
    guardian_max_spend_sgd: Decimal

    # Mode switches — documented defaults
    rebound_mode: ReboundMode = ReboundMode.LIVE
    executor: ExecutorKind = ExecutorKind.LOCAL
    chaos_profile: ChaosProfile = ChaosProfile.NONE
    surface: Surface = Surface.OPERATOR
    daytona_target_sandboxes: int = 8
    model_router_default: ModelBackend = ModelBackend.GEMINI

    # Optional / secrets — no secret defaults
    gemini_api_key: str | None = None
    gemma_endpoint: str | None = None
    kimi_api_key: str | None = None
    daytona_api_key: str | None = None
    nosana_api_key: str | None = None
    telegram_bot_token: str | None = None
    telegram_family_chat_id: str | None = None
    operator_token: str | None = None
    public_base_url: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()

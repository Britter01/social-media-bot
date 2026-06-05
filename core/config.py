"""Centralised configuration.

Loads every environment variable the system needs from the process
environment (and a local ``.env`` during development), validates the
required ones up front, and exposes a single immutable ``Config`` object
the rest of the codebase imports.

Importing ``config`` from here guarantees that a misconfigured deployment
fails fast with a clear message instead of erroring deep inside an agent.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

# Load a local .env if present. In production (Heroku/worker dynos, etc.)
# the variables are already in the environment and this is a no-op.
load_dotenv()

logger = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _get(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if value is not None:
        value = value.strip() or None
    return value


def _get_bool(name: str, default: bool = False) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    """Strongly-typed view over the environment."""

    # --- Brand identity -------------------------------------------------
    brand_name: str = "Brite Tech Lifestyle"
    brand_founder: str = "Dean Britter"
    brand_tagline: str = "Technology, beautifully lived."

    # --- Anthropic (text generation) ------------------------------------
    # Two cost tiers, no Opus (its quality isn't needed for social copy and
    # it costs ~5x Sonnet / ~25x Haiku per token):
    #   * model_creative — writing, ideation, and scoring that need judgment.
    #   * model_fast      — simple, mechanical, repetitive, high-volume work.
    # Each agent picks the cheapest tier that still does its job well; see the
    # model-choice note at the top of each agent module.
    anthropic_api_key: str | None = None
    model_creative: str = "claude-sonnet-4-6"
    model_fast: str = "claude-haiku-4-5"

    # --- Google Imagen 4 Fast (thumbnails) ------------------------------
    google_api_key: str | None = None
    imagen_model: str = "imagen-4.0-fast-generate-001"

    # --- HeyGen (video) -------------------------------------------------
    heygen_api_key: str | None = None
    heygen_voice_id: str | None = None
    heygen_avatar_id: str | None = None

    # --- Supabase (database + storage) ----------------------------------
    supabase_url: str | None = None
    supabase_key: str | None = None
    supabase_bucket: str = "media"

    # --- Platform credentials -------------------------------------------
    instagram_access_token: str | None = None
    instagram_business_account_id: str | None = None
    facebook_page_id: str | None = None

    twitter_api_key: str | None = None
    twitter_api_secret: str | None = None
    twitter_access_token: str | None = None
    twitter_access_secret: str | None = None

    linkedin_access_token: str | None = None
    linkedin_author_urn: str | None = None

    youtube_client_id: str | None = None
    youtube_client_secret: str | None = None
    youtube_refresh_token: str | None = None

    tiktok_access_token: str | None = None

    # --- Runtime behaviour ----------------------------------------------
    timezone: str = "Europe/London"
    dry_run: bool = True  # Safe default — must explicitly set DRY_RUN=false to post live
    log_level: str = "INFO"
    posts_per_run: int = 1
    # Only generate a carousel for 1 in every N Instagram/Facebook posts.
    # Carousels cost ~6–8× more than standard posts (Imagen × slides).
    carousel_every_n: int = 3

    # --- Research agent -------------------------------------------------
    # Minimum brand-relevance score (0-100) a topic must reach to be fed
    # into the content agent. Topics below this are stored but not used.
    min_topic_relevance: int = 70
    # How many of the highest-scoring topics to surface per run.
    topics_per_run: int = 3
    # When True (default), researched topics are stored for human review and
    # only turned into posts after approval (see scripts/review_topics.py).
    # Set False to let the research agent generate content automatically.
    require_topic_approval: bool = True

    # Content pillars and target platforms.
    content_pillars: list[str] = field(
        default_factory=lambda: [
            "AI Guide",
            "Tech Lifestyle",
            "Productivity",
            "Fitness Tech",
            "Review",
        ]
    )
    platforms: list[str] = field(
        default_factory=lambda: [
            "instagram",
            "facebook",
            "twitter",
            "linkedin",
            "youtube",
            "tiktok",
        ]
    )
    # Comma-separated list of platforms to skip entirely (e.g. "twitter,youtube").
    # Set DISABLED_PLATFORMS in Railway to pause a platform without removing its credentials.
    disabled_platforms: list[str] = field(default_factory=list)

    # Themes the research agent scans for trending topics. Free text — the
    # model maps each topic onto one of the content pillars above.
    research_categories: list[str] = field(
        default_factory=lambda: [
            "artificial intelligence",
            "productivity",
            "fitness technology",
            "tech lifestyle",
            "product reviews",
        ]
    )

    @classmethod
    def from_env(cls) -> Config:
        """Build a ``Config`` from the current environment."""
        return cls(
            brand_name=_get("BRAND_NAME", "Brite Tech Lifestyle"),
            brand_founder=_get("BRAND_FOUNDER", "Dean Britter"),
            brand_tagline=_get("BRAND_TAGLINE", "Technology, beautifully lived."),
            anthropic_api_key=_get("ANTHROPIC_API_KEY"),
            model_creative=_get("ANTHROPIC_MODEL_CREATIVE", "claude-sonnet-4-6"),
            model_fast=_get("ANTHROPIC_MODEL_FAST", "claude-haiku-4-5"),
            google_api_key=_get("GOOGLE_API_KEY"),
            imagen_model=_get("IMAGEN_MODEL", "imagen-4.0-fast-generate-001"),
            heygen_api_key=_get("HEYGEN_API_KEY"),
            heygen_voice_id=_get("HEYGEN_VOICE_ID"),
            heygen_avatar_id=_get("HEYGEN_AVATAR_ID"),
            supabase_url=_get("SUPABASE_URL"),
            supabase_key=_get("SUPABASE_KEY"),
            supabase_bucket=_get("SUPABASE_BUCKET", "media"),
            instagram_access_token=_get("INSTAGRAM_ACCESS_TOKEN"),
            instagram_business_account_id=_get("INSTAGRAM_BUSINESS_ACCOUNT_ID"),
            facebook_page_id=_get("FACEBOOK_PAGE_ID"),
            twitter_api_key=_get("TWITTER_API_KEY"),
            twitter_api_secret=_get("TWITTER_API_SECRET"),
            twitter_access_token=_get("TWITTER_ACCESS_TOKEN"),
            twitter_access_secret=_get("TWITTER_ACCESS_SECRET"),
            linkedin_access_token=_get("LINKEDIN_ACCESS_TOKEN"),
            linkedin_author_urn=_get("LINKEDIN_AUTHOR_URN"),
            youtube_client_id=_get("YOUTUBE_CLIENT_ID"),
            youtube_client_secret=_get("YOUTUBE_CLIENT_SECRET"),
            youtube_refresh_token=_get("YOUTUBE_REFRESH_TOKEN"),
            tiktok_access_token=_get("TIKTOK_ACCESS_TOKEN"),
            timezone=_get("TIMEZONE", "Europe/London"),
            # Safe default — must explicitly set DRY_RUN=false to go live
            dry_run=_get_bool("DRY_RUN", True),
            log_level=_get("LOG_LEVEL", "INFO"),
            posts_per_run=_get_int("POSTS_PER_RUN", 1),
            carousel_every_n=_get_int("CAROUSEL_EVERY_N", 3),
            min_topic_relevance=_get_int("MIN_TOPIC_RELEVANCE", 70),
            topics_per_run=_get_int("TOPICS_PER_RUN", 3),
            require_topic_approval=_get_bool("REQUIRE_TOPIC_APPROVAL", True),
            disabled_platforms=[
                p.strip().lower()
                for p in (_get("DISABLED_PLATFORMS") or "").split(",")
                if p.strip()
            ],
        )

    def require(self, *names: str) -> None:
        """Assert that the named attributes are set, else raise ``ConfigError``.

        Use this at the entry point of an agent so a missing key surfaces
        immediately with the exact variable name to fix.
        """
        missing = [name for name in names if not getattr(self, name, None)]
        if missing:
            env_names = ", ".join(sorted(n.upper() for n in missing))
            raise ConfigError(f"Missing required configuration: {env_names}")

    def configured_platforms(self) -> list[str]:
        """Return the subset of platforms that have credentials present and are not disabled.

        Lets the pipeline degrade gracefully: a missing TikTok token skips
        TikTok rather than failing the whole run.  Platforms listed in
        DISABLED_PLATFORMS are excluded even if credentials are present.
        """
        available = []
        for platform in self.platforms:
            if platform in self.disabled_platforms:
                continue
            if platform == "instagram" and self.instagram_access_token:
                available.append(platform)
            elif platform == "facebook" and self.facebook_page_id and self.instagram_access_token:
                available.append(platform)
            elif platform == "twitter" and self.twitter_api_key:
                available.append(platform)
            elif platform == "linkedin" and self.linkedin_access_token:
                available.append(platform)
            elif platform == "youtube" and self.youtube_refresh_token:
                available.append(platform)
            elif platform == "tiktok" and self.tiktok_access_token:
                available.append(platform)
        return available


def configure_logging(level: str = "INFO") -> None:
    """Set up structured-ish console logging once, at process start."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# A single shared instance the rest of the codebase imports.
config = Config.from_env()

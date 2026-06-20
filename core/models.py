"""Domain data models.

These are the in-memory representations the agents pass around. They map
cleanly to Supabase rows (see ``core.database``) and to the structured
output the content agent returns.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class Pillar(StrEnum):
    """The five content pillars for Brite Tech Lifestyle."""

    AI_GUIDE = "AI Guide"
    TECH_LIFESTYLE = "Tech Lifestyle"
    PRODUCTIVITY = "Productivity"
    FITNESS_TECH = "Fitness Tech"
    REVIEW = "Review"


class Platform(StrEnum):
    """Supported publishing destinations."""

    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    TWITTER = "twitter"
    LINKEDIN = "linkedin"
    YOUTUBE = "youtube"
    TIKTOK = "tiktok"


class PostStatus(StrEnum):
    """Lifecycle of a post as it moves through the pipeline."""

    DRAFT = "draft"  # created, awaiting content
    CONTENT_READY = "content_ready"
    MEDIA_READY = "media_ready"  # thumbnail and/or video generated
    MANUAL_READY = "manual_ready"  # manually generated, awaiting user decision
    SCHEDULED = "scheduled"  # has a scheduled time, awaiting publish
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    DISMISSED = "dismissed"  # soft-deleted; kept in DB but ignored by all pipeline jobs


class TopicStatus(StrEnum):
    """Lifecycle of a researched topic before it becomes a post."""

    NEW = "new"  # freshly discovered by the research agent
    PENDING_APPROVAL = "pending_approval"  # passed the relevance bar, awaiting human review
    SELECTED = "selected"  # legacy alias — superseded by PENDING_APPROVAL
    APPROVED = "approved"  # a human approved it; ready to become a post
    USED = "used"  # a post was generated from it
    REJECTED = "rejected"  # discarded (below the bar, or rejected by a human)


@dataclass
class Brand:
    """The brand the system creates content for."""

    name: str
    founder: str
    tagline: str
    voice: str = "Clear, confident, warm. Never patronising. Short sentences."
    pillars: list[str] = field(default_factory=lambda: [p.value for p in Pillar])
    platforms: list[str] = field(default_factory=lambda: [p.value for p in Platform])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Post:
    """A single piece of content moving through the pipeline.

    A ``Post`` starts as a draft (pillar + topic), gains a caption and
    hashtags from the content agent, then media URLs from the thumbnail
    and video agents, a scheduled time from the scheduler, and finally a
    platform post id once published.
    """

    pillar: str
    platform: str
    topic: str = ""
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    title: str = ""
    thumbnail_url: str | None = None
    video_url: str | None = None
    status: str = PostStatus.DRAFT.value
    scheduled_time: datetime | None = None
    published_time: datetime | None = None
    platform_post_id: str | None = None
    error: str | None = None
    post_type: str = "standard"  # "standard" or "carousel"
    # carousel slides: [{headline, body, image_url, role}]
    slides: list[dict] = field(default_factory=list)
    # arbitrary key-value metadata (e.g. bg_source: "cache"|"imagen_3"|"higgsfield")
    meta: dict = field(default_factory=dict)

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        self.caption = self.caption[:5000]
        self.title = self.title[:300]
        self.topic = self.topic[:500]
        self.hashtags = self.hashtags[:50]
        if self.error:
            self.error = self.error[:1000]

    # --- Convenience -----------------------------------------------------

    @property
    def caption_with_hashtags(self) -> str:
        """The caption with hashtags appended, ready to publish."""
        if not self.hashtags:
            return self.caption
        tags = " ".join(tag if tag.startswith("#") else f"#{tag}" for tag in self.hashtags)
        return f"{self.caption}\n\n{tags}".strip()

    def mark(self, status: PostStatus, error: str | None = None) -> None:
        """Transition status and bump ``updated_at``."""
        self.status = status.value
        self.error = error
        self.updated_at = datetime.now(UTC)

    # --- Serialisation ---------------------------------------------------

    def to_row(self) -> dict[str, Any]:
        """Serialise to a Supabase-friendly dict (ISO timestamps)."""
        return {
            "id": self.id,
            "pillar": self.pillar,
            "platform": self.platform,
            "topic": self.topic,
            "caption": self.caption,
            "hashtags": self.hashtags,
            "title": self.title,
            "thumbnail_url": self.thumbnail_url,
            "video_url": self.video_url,
            "status": self.status,
            "scheduled_time": _iso(self.scheduled_time),
            "published_time": _iso(self.published_time),
            "platform_post_id": self.platform_post_id,
            "error": self.error,
            "post_type": self.post_type,
            "slides": self.slides,
            "meta": self.meta,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Post:
        """Rehydrate a ``Post`` from a Supabase row."""
        return cls(
            id=row.get("id") or str(uuid.uuid4()),
            pillar=row["pillar"],
            platform=row["platform"],
            topic=row.get("topic", ""),
            caption=row.get("caption", ""),
            hashtags=list(row.get("hashtags") or []),
            title=row.get("title", ""),
            thumbnail_url=row.get("thumbnail_url"),
            video_url=row.get("video_url"),
            status=row.get("status", PostStatus.DRAFT.value),
            scheduled_time=_parse(row.get("scheduled_time")),
            published_time=_parse(row.get("published_time")),
            platform_post_id=row.get("platform_post_id"),
            error=row.get("error"),
            post_type=row.get("post_type", "standard"),
            slides=list(row.get("slides") or []),
            meta=dict(row.get("meta") or {}),
            created_at=_parse(row.get("created_at")) or datetime.now(UTC),
            updated_at=_parse(row.get("updated_at")) or datetime.now(UTC),
        )


@dataclass
class Topic:
    """A trending topic the research agent surfaced and scored.

    Topics are the upstream of posts: the research agent discovers them
    via web search, scores each for fit with the brand, and decides what
    to make and where. The best ones are handed to the content agent,
    which turns a ``Topic`` into a ``Post`` (pillar + platform + topic).
    """

    title: str
    pillar: str
    platform: str
    summary: str = ""
    content_angle: str = ""  # what to actually make from this topic
    relevance_score: int = 0  # 0-100, brand fit as judged by the model
    rationale: str = ""  # why it scored the way it did
    sources: list[str] = field(default_factory=list)  # supporting URLs
    status: str = TopicStatus.NEW.value

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        self.title = self.title[:500]
        self.summary = self.summary[:2000]
        self.content_angle = self.content_angle[:2000]
        self.rationale = self.rationale[:500]
        self.sources = self.sources[:20]

    # --- Convenience -----------------------------------------------------

    def mark(self, status: TopicStatus) -> None:
        """Transition status and bump ``updated_at``."""
        self.status = status.value
        self.updated_at = datetime.now(UTC)

    def to_post(self) -> Post:
        """Create a fresh draft ``Post`` seeded from this topic."""
        return Post(pillar=self.pillar, platform=self.platform, topic=self.title)

    # --- Serialisation ---------------------------------------------------

    def to_row(self) -> dict[str, Any]:
        """Serialise to a Supabase-friendly dict (ISO timestamps)."""
        return {
            "id": self.id,
            "title": self.title,
            "pillar": self.pillar,
            "platform": self.platform,
            "summary": self.summary,
            "content_angle": self.content_angle,
            "relevance_score": self.relevance_score,
            "rationale": self.rationale,
            "sources": self.sources,
            "status": self.status,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Topic:
        """Rehydrate a ``Topic`` from a Supabase row."""
        return cls(
            id=row.get("id") or str(uuid.uuid4()),
            title=row["title"],
            pillar=row.get("pillar", ""),
            platform=row.get("platform", ""),
            summary=row.get("summary", ""),
            content_angle=row.get("content_angle", ""),
            relevance_score=int(row.get("relevance_score") or 0),
            rationale=row.get("rationale", ""),
            sources=list(row.get("sources") or []),
            status=row.get("status", TopicStatus.NEW.value),
            created_at=_parse(row.get("created_at")) or datetime.now(UTC),
            updated_at=_parse(row.get("updated_at")) or datetime.now(UTC),
        )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    # Supabase returns ISO 8601; tolerate a trailing Z.
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None

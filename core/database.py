"""Supabase persistence layer.

Wraps the Supabase Python client with a small, typed CRUD surface for
``Post`` rows. The rest of the codebase never touches the raw client.

Expected table (create once in the Supabase SQL editor):

    create table if not exists posts (
        id uuid primary key,
        pillar text not null,
        platform text not null,
        topic text,
        caption text,
        hashtags jsonb default '[]'::jsonb,
        title text,
        thumbnail_url text,
        video_url text,
        status text not null default 'draft',
        scheduled_time timestamptz,
        published_time timestamptz,
        platform_post_id text,
        error text,
        created_at timestamptz not null default now(),
        updated_at timestamptz not null default now()
    );
    create index if not exists posts_status_idx on posts (status);
    create index if not exists posts_scheduled_idx on posts (scheduled_time);

And the topics table the research agent writes to:

    create table if not exists topics (
        id uuid primary key,
        title text not null,
        pillar text,
        platform text,
        summary text,
        content_angle text,
        relevance_score int default 0,
        rationale text,
        sources jsonb default '[]'::jsonb,
        status text not null default 'new',
        created_at timestamptz not null default now(),
        updated_at timestamptz not null default now()
    );
    create index if not exists topics_status_idx on topics (status);
    create index if not exists topics_score_idx on topics (relevance_score);
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from core.config import Config, config
from core.models import Post, PostStatus, Topic, TopicStatus

logger = logging.getLogger(__name__)

_TABLE = "posts"
_TOPICS_TABLE = "topics"


class Database:
    """Thin, well-logged wrapper over the Supabase ``posts`` table."""

    def __init__(self, cfg: Config = config) -> None:
        cfg.require("supabase_url", "supabase_key")
        # Imported lazily so the module imports cleanly without the package
        # installed (e.g. during unit tests that monkeypatch the client).
        from supabase import create_client

        self._cfg = cfg
        self._client = create_client(cfg.supabase_url, cfg.supabase_key)
        logger.info("Connected to Supabase at %s", cfg.supabase_url)

    # --- Writes ----------------------------------------------------------

    def insert(self, post: Post) -> Post:
        """Insert a new post row."""
        try:
            self._client.table(_TABLE).insert(post.to_row()).execute()
            logger.info("Inserted post %s (%s/%s)", post.id, post.pillar, post.platform)
            return post
        except Exception:
            logger.exception("Failed to insert post %s", post.id)
            raise

    def upsert(self, post: Post) -> Post:
        """Insert or update a post, bumping ``updated_at``."""
        post.updated_at = datetime.now(UTC)
        try:
            self._client.table(_TABLE).upsert(post.to_row()).execute()
            logger.debug("Upserted post %s (status=%s)", post.id, post.status)
            return post
        except Exception:
            logger.exception("Failed to upsert post %s", post.id)
            raise

    def update_status(self, post: Post, status: PostStatus, error: str | None = None) -> Post:
        """Persist a status transition."""
        post.mark(status, error)
        return self.upsert(post)

    def claim_for_publishing(self, post: Post) -> bool:
        """Atomically claim a post for publishing.

        Conditionally transitions the row ``scheduled -> publishing`` in a
        single UPDATE that only matches while the row is still ``scheduled``.
        PostgREST returns the updated rows, so an empty result means another
        worker (or a previous run) already claimed it — the caller must skip.

        This is the guard against double-publishing when more than one worker
        instance is running. Returns True iff *this* caller won the claim.
        """
        now = datetime.now(UTC)
        try:
            resp = (
                self._client.table(_TABLE)
                .update(
                    {
                        "status": PostStatus.PUBLISHING.value,
                        "updated_at": now.isoformat(),
                    }
                )
                .eq("id", post.id)
                .eq("status", PostStatus.SCHEDULED.value)
                .execute()
            )
        except Exception:
            logger.exception("Failed to claim post %s for publishing", post.id)
            raise

        claimed = bool(resp.data)
        if claimed:
            post.status = PostStatus.PUBLISHING.value
            post.updated_at = now
            logger.info("Claimed post %s for publishing", post.id)
        else:
            logger.info("Post %s was already claimed or published; skipping", post.id)
        return claimed

    # --- Reads -----------------------------------------------------------

    def get(self, post_id: str) -> Post | None:
        try:
            resp = self._client.table(_TABLE).select("*").eq("id", post_id).limit(1).execute()
            rows = resp.data or []
            return Post.from_row(rows[0]) if rows else None
        except Exception:
            logger.exception("Failed to fetch post %s", post_id)
            raise

    def by_status(self, status: PostStatus, limit: int = 50) -> list[Post]:
        """Return posts in a given status, oldest first."""
        try:
            resp = (
                self._client.table(_TABLE)
                .select("*")
                .eq("status", status.value)
                .order("created_at", desc=False)
                .limit(limit)
                .execute()
            )
            return [Post.from_row(row) for row in (resp.data or [])]
        except Exception:
            logger.exception("Failed to query posts by status %s", status.value)
            raise

    def due_for_publishing(self, now: datetime | None = None, limit: int = 50) -> list[Post]:
        """Return scheduled posts whose ``scheduled_time`` has passed."""
        now = now or datetime.now(UTC)
        try:
            resp = (
                self._client.table(_TABLE)
                .select("*")
                .eq("status", PostStatus.SCHEDULED.value)
                .lte("scheduled_time", now.isoformat())
                .order("scheduled_time", desc=False)
                .limit(limit)
                .execute()
            )
            return [Post.from_row(row) for row in (resp.data or [])]
        except Exception:
            logger.exception("Failed to query posts due for publishing")
            raise

    # --- Topics ----------------------------------------------------------

    def insert_topic(self, topic: Topic) -> Topic:
        """Insert a new researched topic row."""
        try:
            self._client.table(_TOPICS_TABLE).insert(topic.to_row()).execute()
            logger.info(
                "Inserted topic %s (%s, score=%s)",
                topic.id,
                topic.pillar,
                topic.relevance_score,
            )
            return topic
        except Exception:
            logger.exception("Failed to insert topic %s", topic.id)
            raise

    def upsert_topic(self, topic: Topic) -> Topic:
        """Insert or update a topic, bumping ``updated_at``."""
        topic.updated_at = datetime.now(UTC)
        try:
            self._client.table(_TOPICS_TABLE).upsert(topic.to_row()).execute()
            logger.debug("Upserted topic %s (status=%s)", topic.id, topic.status)
            return topic
        except Exception:
            logger.exception("Failed to upsert topic %s", topic.id)
            raise

    def topics_by_status(self, status: TopicStatus, limit: int = 50) -> list[Topic]:
        """Return topics in a given status, highest-scoring first."""
        try:
            resp = (
                self._client.table(_TOPICS_TABLE)
                .select("*")
                .eq("status", status.value)
                .order("relevance_score", desc=True)
                .limit(limit)
                .execute()
            )
            return [Topic.from_row(row) for row in (resp.data or [])]
        except Exception:
            logger.exception("Failed to query topics by status %s", status.value)
            raise


_db: Database | None = None


def get_database(cfg: Config = config) -> Database:
    """Return a process-wide singleton ``Database`` (lazy-initialised)."""
    global _db
    if _db is None:
        _db = Database(cfg)
    return _db

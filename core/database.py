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
        post_type text not null default 'standard',
        slides jsonb default '[]'::jsonb,
        created_at timestamptz not null default now(),
        updated_at timestamptz not null default now()
    );
    create index if not exists posts_status_idx on posts (status);
    create index if not exists posts_scheduled_idx on posts (scheduled_time);

-- If upgrading from an older schema that is missing these columns, run:
--   alter table posts add column if not exists post_type text not null default 'standard';
--   alter table posts add column if not exists slides jsonb default '[]'::jsonb;

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

And the analytics table the analytics agent writes to:

    CREATE TABLE IF NOT EXISTS post_analytics (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        post_id uuid NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
        platform text NOT NULL,
        platform_post_id text NOT NULL,
        snapshot_type text NOT NULL CHECK (snapshot_type IN ('24h','7d')),
        fetched_at timestamptz NOT NULL DEFAULT now(),
        reach integer,
        impressions integer,
        likes integer,
        comments integer,
        shares integer,
        saves integer,
        video_views integer,
        raw_data jsonb,
        UNIQUE(post_id, snapshot_type)
    );
    CREATE INDEX IF NOT EXISTS post_analytics_post_id_idx ON post_analytics(post_id);
    CREATE INDEX IF NOT EXISTS post_analytics_fetched_at_idx ON post_analytics(fetched_at);
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from core.config import Config, config
from core.models import Post, PostStatus, Topic, TopicStatus

logger = logging.getLogger(__name__)

_TABLE = "posts"
_TOPICS_TABLE = "topics"
_ANALYTICS_TABLE = "post_analytics"


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

    def latest_scheduled_time_by_platform(self) -> dict[str, datetime]:
        """Return the most recent ``scheduled_time`` per platform for 'scheduled' posts.

        Used by the pipeline to space new posts after any already in the queue,
        preventing two posts on the same platform from landing at the same slot.
        """
        try:
            resp = (
                self._client.table(_TABLE)
                .select("platform, scheduled_time")
                .eq("status", "scheduled")
                .execute()
            )
            rows = resp.data or []
        except Exception:
            logger.exception("Failed to query latest scheduled times by platform")
            return {}

        latest: dict[str, datetime] = {}
        for row in rows:
            plat = row.get("platform") or ""
            raw = row.get("scheduled_time") or ""
            if not plat or not raw:
                continue
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if plat not in latest or dt > latest[plat]:
                    latest[plat] = dt
            except Exception:
                pass
        return latest

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

    def get_topic(self, topic_id: str) -> Topic | None:
        """Fetch a single topic by id, or None if it doesn't exist."""
        try:
            resp = (
                self._client.table(_TOPICS_TABLE).select("*").eq("id", topic_id).limit(1).execute()
            )
            rows = resp.data or []
            return Topic.from_row(rows[0]) if rows else None
        except Exception:
            logger.exception("Failed to fetch topic %s", topic_id)
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

    # --- Analytics -------------------------------------------------------

    def get_all_published_without_analytics(self) -> list[dict]:
        """Return all published posts that have no analytics row at all.

        Used for the backfill pass so posts that slipped through the narrow
        time windows are still captured on a manual fetch. Posts with a
        'dry-run' platform_post_id are excluded — they were never actually
        posted live and will never have real metrics.
        """
        try:
            resp = (
                self._client.table(_TABLE)
                .select("id, platform, platform_post_id, published_time, title, pillar")
                .eq("status", "published")
                .not_.is_("platform_post_id", "null")
                .neq("platform_post_id", "dry-run")
                .execute()
            )
            candidates = resp.data or []
        except Exception:
            logger.exception("Failed to query all published posts")
            return []

        if not candidates:
            return []

        candidate_ids = [row["id"] for row in candidates]
        try:
            existing_resp = (
                self._client.table(_ANALYTICS_TABLE)
                .select("post_id")
                .in_("post_id", candidate_ids)
                .execute()
            )
            existing_ids = {row["post_id"] for row in (existing_resp.data or [])}
        except Exception:
            logger.exception("Failed to query existing analytics rows")
            existing_ids = set()

        return [row for row in candidates if row["id"] not in existing_ids]

    def get_analytics_for_posts(self, post_ids: list[str]) -> list[dict]:
        """Return all post_analytics rows for the given post IDs."""
        if not post_ids:
            return []
        try:
            resp = (
                self._client.table(_ANALYTICS_TABLE).select("*").in_("post_id", post_ids).execute()
            )
            return resp.data or []
        except Exception:
            logger.exception("Failed to fetch analytics for %d post(s)", len(post_ids))
            return []

    def get_posts_needing_analytics(
        self, snapshot_type: str, hours_after_publish: int
    ) -> list[dict]:
        """Return published posts that need a given analytics snapshot.

        Looks for posts published between ``(now - hours - 2h)`` and
        ``(now - hours)`` that do NOT yet have a ``post_analytics`` row for
        ``snapshot_type``.  Returns raw dicts (not Post objects) so the
        caller gets ``platform_post_id`` without needing a Post model field.
        """
        from datetime import timedelta

        now = datetime.now(UTC)
        window_end = now - timedelta(hours=hours_after_publish)
        window_start = window_end - timedelta(hours=2)
        try:
            # Fetch posts published in the target window with a platform_post_id.
            resp = (
                self._client.table(_TABLE)
                .select("id, platform, platform_post_id, published_time, title, pillar")
                .eq("status", "published")
                .not_.is_("platform_post_id", "null")
                .gte("published_time", window_start.isoformat())
                .lte("published_time", window_end.isoformat())
                .execute()
            )
            candidates = resp.data or []
        except Exception:
            logger.exception("Failed to query posts needing analytics")
            return []

        if not candidates:
            return []

        # Filter out posts that already have this snapshot type.
        candidate_ids = [row["id"] for row in candidates]
        try:
            existing_resp = (
                self._client.table(_ANALYTICS_TABLE)
                .select("post_id")
                .in_("post_id", candidate_ids)
                .eq("snapshot_type", snapshot_type)
                .execute()
            )
            existing_ids = {row["post_id"] for row in (existing_resp.data or [])}
        except Exception:
            logger.exception("Failed to query existing analytics snapshots")
            existing_ids = set()

        return [row for row in candidates if row["id"] not in existing_ids]

    def get_posts_missing_views(self, platforms: list[str]) -> list[dict]:
        """Return published posts that have an analytics row but no view metrics.

        Used to re-fetch views after a metric rename (e.g. the 2026-06-15
        deprecation of post_impressions -> post_views on Facebook).  Only
        rows where ALL three view columns are NULL are returned — if any view
        metric was ever stored we leave that row alone.
        """
        if not platforms:
            return []
        try:
            # Get published posts for these platforms that have a platform_post_id.
            posts_resp = (
                self._client.table(_TABLE)
                .select("id, platform, platform_post_id, published_time")
                .eq("status", "published")
                .in_("platform", platforms)
                .not_.is_("platform_post_id", "null")
                .neq("platform_post_id", "dry-run")
                .execute()
            )
            posts = posts_resp.data or []
        except Exception:
            logger.exception("Failed to query published posts for views-missing check")
            return []

        if not posts:
            return []

        post_ids = [p["id"] for p in posts]
        try:
            # Find analytics rows for these posts where all view columns are NULL.
            rows_resp = (
                self._client.table(_ANALYTICS_TABLE)
                .select("post_id")
                .in_("post_id", post_ids)
                .is_("impressions", "null")
                .is_("reach", "null")
                .is_("video_views", "null")
                .execute()
            )
            missing_view_ids = {r["post_id"] for r in (rows_resp.data or [])}
        except Exception:
            logger.exception("Failed to query analytics rows for views check")
            return []

        return [p for p in posts if p["id"] in missing_view_ids]


_db: Database | None = None


def get_database(cfg: Config = config) -> Database:
    """Return a process-wide singleton ``Database`` (lazy-initialised)."""
    global _db
    if _db is None:
        _db = Database(cfg)
    return _db

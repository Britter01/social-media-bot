"""Tests for core.database (Supabase client mocked)."""

from __future__ import annotations

from unittest.mock import MagicMock

from core.database import Database
from core.models import Post, PostStatus, Topic, TopicStatus


def _make_db(base_config):
    db = Database(base_config)
    db._client = MagicMock()
    return db


def test_insert_calls_supabase(base_config):
    db = _make_db(base_config)
    post = Post(pillar="Review", platform="instagram")
    db.insert(post)
    db._client.table.assert_called_with("posts")
    db._client.table.return_value.insert.assert_called_once()


def test_update_status_persists_new_status(base_config):
    db = _make_db(base_config)
    post = Post(pillar="AI Guide", platform="twitter")
    db.update_status(post, PostStatus.PUBLISHED)
    assert post.status == PostStatus.PUBLISHED.value
    db._client.table.return_value.upsert.assert_called_once()


def test_by_status_maps_rows_to_posts(base_config):
    db = _make_db(base_config)
    query = db._client.table.return_value.select.return_value.eq.return_value
    chain = query.order.return_value.limit.return_value.execute.return_value
    chain.data = [
        {"pillar": "Review", "platform": "instagram", "status": "draft"},
        {"pillar": "AI Guide", "platform": "twitter", "status": "draft"},
    ]
    posts = db.by_status(PostStatus.DRAFT)
    assert len(posts) == 2
    assert all(isinstance(p, Post) for p in posts)
    assert posts[0].pillar == "Review"


def test_claim_for_publishing_succeeds_when_row_updated(base_config):
    db = _make_db(base_config)
    upd = db._client.table.return_value.update.return_value
    chain = upd.eq.return_value.eq.return_value.execute.return_value
    chain.data = [{"id": "x", "status": "publishing"}]

    post = Post(pillar="Review", platform="instagram")
    post.mark(PostStatus.SCHEDULED)
    assert db.claim_for_publishing(post) is True
    assert post.status == PostStatus.PUBLISHING.value


def test_claim_for_publishing_fails_when_already_taken(base_config):
    db = _make_db(base_config)
    upd = db._client.table.return_value.update.return_value
    chain = upd.eq.return_value.eq.return_value.execute.return_value
    chain.data = []  # no row matched 'scheduled' -> someone else claimed it

    post = Post(pillar="Review", platform="instagram")
    post.mark(PostStatus.SCHEDULED)
    assert db.claim_for_publishing(post) is False
    # Local status is left untouched when the claim is lost.
    assert post.status == PostStatus.SCHEDULED.value


def test_insert_topic_calls_topics_table(base_config):
    db = _make_db(base_config)
    topic = Topic(title="x", pillar="AI Guide", platform="instagram")
    db.insert_topic(topic)
    db._client.table.assert_called_with("topics")
    db._client.table.return_value.insert.assert_called_once()


def test_topics_by_status_orders_by_score_desc(base_config):
    db = _make_db(base_config)
    query = db._client.table.return_value.select.return_value.eq.return_value
    chain = query.order.return_value.limit.return_value.execute.return_value
    chain.data = [
        {"title": "a", "pillar": "Review", "platform": "instagram", "relevance_score": 90},
    ]
    topics = db.topics_by_status(TopicStatus.NEW)
    assert len(topics) == 1
    assert isinstance(topics[0], Topic)
    db._client.table.return_value.select.return_value.eq.return_value.order.assert_called_with(
        "relevance_score", desc=True
    )


def test_due_for_publishing_queries_scheduled(base_config):
    db = _make_db(base_config)
    query = db._client.table.return_value.select.return_value.eq.return_value
    chain = query.lte.return_value.order.return_value.limit.return_value.execute.return_value
    chain.data = []
    assert db.due_for_publishing() == []
    db._client.table.return_value.select.return_value.eq.assert_called_with(
        "status", PostStatus.SCHEDULED.value
    )

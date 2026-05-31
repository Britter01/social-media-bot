"""Tests for core.models."""

from __future__ import annotations

from datetime import UTC, datetime

from core.models import Post, PostStatus, Topic, TopicStatus


def test_caption_with_hashtags_appends_and_prefixes_hash():
    post = Post(pillar="Review", platform="instagram", caption="Great earbuds.")
    post.hashtags = ["audio", "#tech"]
    out = post.caption_with_hashtags
    assert out.startswith("Great earbuds.")
    assert "#audio" in out
    assert "#tech" in out
    # No double-hashing.
    assert "##tech" not in out


def test_caption_with_hashtags_no_tags():
    post = Post(pillar="Review", platform="instagram", caption="Just text.")
    assert post.caption_with_hashtags == "Just text."


def test_mark_updates_status_and_timestamp():
    post = Post(pillar="AI Guide", platform="twitter")
    before = post.updated_at
    post.mark(PostStatus.PUBLISHED)
    assert post.status == PostStatus.PUBLISHED.value
    assert post.updated_at >= before
    assert post.error is None


def test_mark_records_error():
    post = Post(pillar="AI Guide", platform="twitter")
    post.mark(PostStatus.FAILED, error="boom")
    assert post.status == PostStatus.FAILED.value
    assert post.error == "boom"


def test_row_roundtrip_preserves_fields():
    post = Post(
        pillar="Productivity",
        platform="linkedin",
        topic="time blocking",
        caption="Block your calendar.",
        hashtags=["productivity", "focus"],
        title="Own your day",
    )
    post.scheduled_time = datetime(2026, 6, 2, 8, 0, tzinfo=UTC)

    row = post.to_row()
    restored = Post.from_row(row)

    assert restored.id == post.id
    assert restored.pillar == post.pillar
    assert restored.platform == post.platform
    assert restored.hashtags == post.hashtags
    assert restored.title == post.title
    assert restored.scheduled_time == post.scheduled_time


def test_from_row_tolerates_trailing_z():
    row = {
        "pillar": "Review",
        "platform": "youtube",
        "scheduled_time": "2026-06-02T08:00:00Z",
    }
    post = Post.from_row(row)
    assert post.scheduled_time is not None
    assert post.scheduled_time.year == 2026


def test_topic_row_roundtrip_preserves_fields():
    topic = Topic(
        title="On-device AI assistants",
        pillar="AI Guide",
        platform="youtube",
        summary="New phones ship local models.",
        content_angle="Explain what runs on-device vs in the cloud.",
        relevance_score=88,
        rationale="Squarely on-brand and timely.",
        sources=["https://example.com/a", "https://example.com/b"],
    )
    restored = Topic.from_row(topic.to_row())

    assert restored.id == topic.id
    assert restored.title == topic.title
    assert restored.pillar == topic.pillar
    assert restored.platform == topic.platform
    assert restored.relevance_score == 88
    assert restored.sources == topic.sources
    assert restored.status == TopicStatus.NEW.value


def test_topic_mark_transitions_status():
    topic = Topic(title="x", pillar="Review", platform="instagram")
    topic.mark(TopicStatus.USED)
    assert topic.status == TopicStatus.USED.value


def test_topic_to_post_seeds_a_draft():
    topic = Topic(title="best earbuds 2026", pillar="Review", platform="instagram")
    post = topic.to_post()
    assert post.pillar == "Review"
    assert post.platform == "instagram"
    assert post.topic == "best earbuds 2026"
    assert post.status == PostStatus.DRAFT.value

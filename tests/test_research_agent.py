"""Tests for agents.research_agent (Anthropic client mocked)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agents.research_agent import ResearchAgent, ScoredTopic, TopicSlate
from core.models import Pillar, Post, PostStatus, Topic, TopicStatus


def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def _search_response(text, stop_reason="end_turn"):
    return SimpleNamespace(content=[_text_block(text)], stop_reason=stop_reason)


def _scored(title="Topic", pillar="AI Guide", platform="instagram", score=80):
    return ScoredTopic(
        title=title,
        summary="A summary.",
        pillar=pillar,
        platform=platform,
        content_angle="An angle.",
        relevance_score=score,
        rationale="Because.",
        sources=["https://example.com"],
    )


def _slate_response(scored_topics, stop_reason="end_turn"):
    return SimpleNamespace(
        parsed_output=TopicSlate(topics=scored_topics),
        stop_reason=stop_reason,
        usage=SimpleNamespace(cache_read_input_tokens=10, input_tokens=100, output_tokens=50),
    )


def _agent(base_config, **kwargs):
    agent = ResearchAgent(base_config, **kwargs)
    agent._client = MagicMock()
    return agent


# --- discover --------------------------------------------------------------


def test_discover_searches_then_scores(base_config):
    agent = _agent(base_config)
    agent._client.messages.create.return_value = _search_response("Findings: X is trending.")
    agent._client.messages.parse.return_value = _slate_response(
        [_scored(title="Apple Vision", pillar="Review", platform="youtube", score=85)]
    )

    topics = agent.discover()

    assert len(topics) == 1
    topic = topics[0]
    assert isinstance(topic, Topic)
    assert topic.title == "Apple Vision"
    assert topic.pillar == Pillar.REVIEW.value
    assert topic.platform == "youtube"
    assert topic.relevance_score == 85
    assert topic.sources == ["https://example.com"]


def test_discover_uses_web_search_tool_and_caching(base_config):
    agent = _agent(base_config)
    agent._client.messages.create.return_value = _search_response("stuff")
    agent._client.messages.parse.return_value = _slate_response([_scored()])

    agent.discover()

    _, kwargs = agent._client.messages.create.call_args
    assert kwargs["tools"] == [{"type": "web_search_20260209", "name": "web_search"}]
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}

    _, parse_kwargs = agent._client.messages.parse.call_args
    assert parse_kwargs["output_format"] is TopicSlate
    assert parse_kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_discover_empty_search_returns_no_topics(base_config):
    agent = _agent(base_config)
    agent._client.messages.create.return_value = _search_response("   ")

    assert agent.discover() == []
    agent._client.messages.parse.assert_not_called()


def test_search_resumes_on_pause_turn(base_config):
    agent = _agent(base_config)
    agent._client.messages.create.side_effect = [
        _search_response("partial", stop_reason="pause_turn"),
        _search_response("complete findings", stop_reason="end_turn"),
    ]
    agent._client.messages.parse.return_value = _slate_response([_scored()])

    agent.discover()

    assert agent._client.messages.create.call_count == 2


def test_score_raises_without_structured_output(base_config):
    agent = _agent(base_config)
    agent._client.messages.create.return_value = _search_response("findings")
    agent._client.messages.parse.return_value = SimpleNamespace(
        parsed_output=None, stop_reason="refusal", usage=SimpleNamespace()
    )

    with pytest.raises(RuntimeError):
        agent.discover()


# --- normalisation ---------------------------------------------------------


def test_unknown_pillar_falls_back(base_config):
    agent = _agent(base_config)
    topic = agent._to_topic(_scored(pillar="Crypto Hype"))
    assert topic.pillar == Pillar.TECH_LIFESTYLE.value


def test_pillar_match_is_case_insensitive(base_config):
    agent = _agent(base_config)
    topic = agent._to_topic(_scored(pillar="productivity"))
    assert topic.pillar == Pillar.PRODUCTIVITY.value


def test_unknown_platform_falls_back_to_configured(base_config):
    agent = _agent(base_config)
    topic = agent._to_topic(_scored(platform="myspace"))
    assert topic.platform in base_config.configured_platforms()


def test_relevance_score_is_clamped(base_config):
    agent = _agent(base_config)
    assert agent._to_topic(_scored(score=140)).relevance_score == 100
    assert agent._to_topic(_scored(score=-5)).relevance_score == 0


# --- select_best -----------------------------------------------------------


def test_select_best_filters_sorts_and_caps(base_config):
    agent = _agent(base_config)
    topics = [
        Topic(title="low", pillar="Review", platform="instagram", relevance_score=40),
        Topic(title="high", pillar="Review", platform="instagram", relevance_score=95),
        Topic(title="mid", pillar="Review", platform="instagram", relevance_score=75),
    ]

    chosen = agent.select_best(topics, limit=2)

    assert [t.title for t in chosen] == ["high", "mid"]  # sorted, capped at 2
    assert all(t.status == TopicStatus.SELECTED.value for t in chosen)
    # The below-threshold topic is marked rejected, not selected.
    assert topics[0].status == TopicStatus.REJECTED.value


# --- run (end to end, agents mocked) ---------------------------------------


def test_run_persists_topics_and_feeds_content_agent(base_config):
    content_agent = MagicMock()

    def _generate(post):
        post.mark(PostStatus.CONTENT_READY)
        return post

    content_agent.generate.side_effect = _generate
    db = MagicMock()

    agent = _agent(base_config, content_agent=content_agent, db=db)
    agent._client.messages.create.return_value = _search_response("findings")
    agent._client.messages.parse.return_value = _slate_response(
        [
            _scored(title="great", platform="instagram", score=90),
            _scored(title="meh", platform="instagram", score=30),  # below default 70
        ]
    )

    posts = agent.run()

    # Only the high-scoring topic became a post.
    assert len(posts) == 1
    assert isinstance(posts[0], Post)
    assert posts[0].topic == "great"
    content_agent.generate.assert_called_once()
    # Both topics persisted; the used one upserted after content.
    assert db.insert_topic.call_count == 2
    db.upsert_topic.assert_called()


def test_run_isolates_content_failures(base_config):
    content_agent = MagicMock()
    content_agent.generate.side_effect = RuntimeError("api down")
    db = MagicMock()

    agent = _agent(base_config, content_agent=content_agent, db=db)
    agent._client.messages.create.return_value = _search_response("findings")
    agent._client.messages.parse.return_value = _slate_response([_scored(score=90)])

    posts = agent.run()

    assert posts == []  # failure didn't propagate
    db.insert_topic.assert_called()  # topic still persisted


def test_run_without_collaborators_just_returns_no_posts(base_config):
    agent = _agent(base_config)
    agent._client.messages.create.return_value = _search_response("findings")
    agent._client.messages.parse.return_value = _slate_response([_scored(score=90)])

    # No content agent / db injected — discovery runs, nothing is generated.
    assert agent.run() == []


# --- config / construction -------------------------------------------------


def test_missing_api_key_raises():
    import dataclasses

    from core.config import ConfigError, config

    cfg = dataclasses.replace(config, anthropic_api_key=None)
    with pytest.raises(ConfigError):
        ResearchAgent(cfg)

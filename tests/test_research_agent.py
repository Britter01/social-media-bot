"""Tests for agents.research_agent (Anthropic client mocked)."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agents.research_agent import ResearchAgent, ScoredTopic, TopicSlate
from core.models import Pillar, PostStatus, Topic, TopicStatus


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


def test_discover_uses_web_search_tool_and_models(base_config):
    agent = _agent(base_config)
    agent._client.messages.create.return_value = _search_response("stuff")
    agent._client.messages.parse.return_value = _slate_response([_scored()])

    agent.discover()

    _, kwargs = agent._client.messages.create.call_args
    assert kwargs["tools"] == [
        {"type": "web_search_20260209", "name": "web_search", "allowed_callers": ["direct"]}
    ]
    # Discovery runs on the fast (Haiku) tier — and must not send effort,
    # which Haiku 4.5 rejects.
    assert kwargs["model"] == base_config.model_fast
    assert "output_config" not in kwargs
    # No prompt caching on the research calls (single daily calls, no reuse):
    # system is a plain string, not a cache_control-tagged block list.
    assert kwargs["system"] == agent._system
    assert isinstance(kwargs["system"], str)

    _, parse_kwargs = agent._client.messages.parse.call_args
    assert parse_kwargs["output_format"] is TopicSlate
    assert isinstance(parse_kwargs["system"], str)
    # Scoring runs on the creative (Sonnet) tier — never Opus.
    assert parse_kwargs["model"] == base_config.model_creative
    assert "opus" not in agent._cfg.model_fast and "opus" not in agent._cfg.model_creative


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
    assert all(t.status == TopicStatus.PENDING_APPROVAL.value for t in chosen)
    # The below-threshold topic is marked rejected, not pending.
    assert topics[0].status == TopicStatus.REJECTED.value


def test_select_best_rejects_disabled_platform_topics(base_config):
    cfg = dataclasses.replace(base_config, disabled_platforms=["youtube"])
    agent = _agent(cfg)
    topics = [
        Topic(title="yt", pillar="Review", platform="youtube", relevance_score=99),
        Topic(title="ig", pillar="Review", platform="instagram", relevance_score=80),
    ]

    chosen = agent.select_best(topics)

    # The high-scoring YouTube topic is never surfaced for approval.
    assert [t.title for t in chosen] == ["ig"]
    assert topics[0].status == TopicStatus.REJECTED.value


# --- research (discovery + persistence, no content) ------------------------


def test_research_persists_all_and_returns_selected(base_config):
    db = MagicMock()
    agent = _agent(base_config, db=db)
    agent._client.messages.create.return_value = _search_response("findings")
    agent._client.messages.parse.return_value = _slate_response(
        [
            _scored(title="great", score=90),
            _scored(title="meh", score=30),  # below default 70
        ]
    )

    selected = agent.research()

    # Both topics persisted (rejected ideas stay auditable); only one selected.
    assert db.insert_topic.call_count == 2
    assert [t.title for t in selected] == ["great"]
    assert selected[0].status == TopicStatus.PENDING_APPROVAL.value


# --- run / approval gate ---------------------------------------------------


def test_run_awaits_approval_by_default(base_config):
    content_agent = MagicMock()
    db = MagicMock()
    agent = _agent(base_config, content_agent=content_agent, db=db)
    agent._client.messages.create.return_value = _search_response("findings")
    agent._client.messages.parse.return_value = _slate_response([_scored(score=90)])

    posts = agent.run()

    # Gate is on by default: topics are stored for review, nothing generated.
    assert posts == []
    content_agent.generate.assert_not_called()
    db.insert_topic.assert_called()


def test_run_auto_generates_when_approval_disabled(base_config):
    cfg = dataclasses.replace(base_config, require_topic_approval=False)
    content_agent = MagicMock()
    content_agent.generate.side_effect = lambda post: post.mark(PostStatus.CONTENT_READY)
    db = MagicMock()

    agent = _agent(cfg, content_agent=content_agent, db=db)
    agent._client.messages.create.return_value = _search_response("findings")
    agent._client.messages.parse.return_value = _slate_response(
        [
            _scored(title="great", score=90),
            _scored(title="meh", score=30),
        ]
    )

    posts = agent.run()

    # "great" generates an Instagram post + a Facebook cross-post = 2
    assert all(p.topic == "great" for p in posts)
    assert {p.platform for p in posts} == {"instagram", "facebook"}
    content_agent.generate.assert_called_once()


# --- generate_for_approved -------------------------------------------------


def test_generate_for_approved_pulls_approved_topics(base_config):
    content_agent = MagicMock()
    content_agent.generate.side_effect = lambda post: post.mark(PostStatus.CONTENT_READY)
    db = MagicMock()
    db.topics_by_status.return_value = [
        Topic(title="approved one", pillar="Review", platform="instagram", relevance_score=90),
    ]

    agent = _agent(base_config, content_agent=content_agent, db=db)
    posts = agent.generate_for_approved()

    db.topics_by_status.assert_called_once_with(TopicStatus.APPROVED, limit=50)
    # Instagram topic generates Instagram post + Facebook cross-post = 2
    assert len(posts) == 2
    assert all(p.topic == "approved one" for p in posts)
    assert {p.platform for p in posts} == {"instagram", "facebook"}
    # The topic is marked used and persisted.
    db.upsert_topic.assert_called()


def test_generate_content_isolates_failures(base_config):
    content_agent = MagicMock()
    content_agent.generate.side_effect = RuntimeError("api down")
    db = MagicMock()
    agent = _agent(base_config, content_agent=content_agent, db=db)

    topics = [Topic(title="x", pillar="Review", platform="instagram")]
    assert agent.generate_content(topics) == []  # failure didn't propagate
    db.upsert_topic.assert_called()  # status still persisted


# --- config / construction -------------------------------------------------


def test_missing_api_key_raises():
    import dataclasses

    from core.config import ConfigError, config

    cfg = dataclasses.replace(config, anthropic_api_key=None)
    with pytest.raises(ConfigError):
        ResearchAgent(cfg)

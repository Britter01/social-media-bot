"""Tests for agents.quality_agent."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agents.quality_agent import QualityAgent, QualityError
from core.models import Post


def _msg(text):
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


def _agent(base_config):
    agent = QualityAgent(base_config)
    agent._client = MagicMock()
    return agent


def _post(caption="Great post. Great post.", thumbnail_url=None):
    return Post(
        pillar="AI Guide",
        platform="instagram",
        caption=caption,
        thumbnail_url=thumbnail_url,
    )


# --- Text checks ------------------------------------------------------------


def test_text_ok_leaves_caption_unchanged(base_config):
    agent = _agent(base_config)
    agent._client.messages.create.return_value = _msg("OK")
    post = _post("A great caption.")
    agent.review(post)
    assert post.caption == "A great caption."


def test_text_fix_rewrites_caption(base_config):
    agent = _agent(base_config)
    agent._client.messages.create.return_value = _msg("A great caption.")
    post = _post("A great caption. A great caption.")
    agent.review(post)
    assert post.caption == "A great caption."


def test_text_ok_case_insensitive(base_config):
    agent = _agent(base_config)
    agent._client.messages.create.return_value = _msg("ok")
    post = _post("Fine caption.")
    agent.review(post)
    assert post.caption == "Fine caption."


def test_empty_caption_skips_text_check(base_config):
    agent = _agent(base_config)
    post = _post(caption="")
    agent.review(post)
    agent._client.messages.create.assert_not_called()


# --- Image checks -----------------------------------------------------------


def test_image_ok_does_not_raise(base_config):
    agent = _agent(base_config)
    agent._client.messages.create.return_value = _msg('{"ok": true}')
    post = _post(thumbnail_url="https://test.supabase.co/thumb.jpg")
    with patch.object(
        QualityAgent,
        "_fetch_image",
        return_value=("base64data", "image/jpeg"),
    ):
        agent.review(post)  # should not raise


def test_image_fail_raises_quality_error(base_config):
    agent = _agent(base_config)
    agent._client.messages.create.return_value = _msg(
        json.dumps({"ok": False, "issue": "logo is cut off"})
    )
    post = _post(thumbnail_url="https://test.supabase.co/thumb.jpg")
    with patch.object(
        QualityAgent,
        "_fetch_image",
        return_value=("base64data", "image/jpeg"),
    ):
        with pytest.raises(QualityError, match="logo is cut off"):
            agent.review(post)


def test_image_fetch_failure_skips_check(base_config):
    agent = _agent(base_config)
    # Empty caption so only the image path runs; fetch fails → no API call.
    post = _post(caption="", thumbnail_url="https://test.supabase.co/thumb.jpg")
    with patch.object(QualityAgent, "_fetch_image", return_value=(None, "")):
        agent.review(post)  # should not raise despite bad fetch
    agent._client.messages.create.assert_not_called()


def test_image_non_json_response_skips_check(base_config):
    agent = _agent(base_config)
    agent._client.messages.create.return_value = _msg("looks good to me")
    post = _post(thumbnail_url="https://test.supabase.co/thumb.jpg")
    with patch.object(
        QualityAgent,
        "_fetch_image",
        return_value=("base64data", "image/jpeg"),
    ):
        agent.review(post)  # non-JSON response must not raise


def test_no_thumbnail_skips_image_check(base_config):
    agent = _agent(base_config)
    agent._client.messages.create.return_value = _msg("OK")
    post = _post(thumbnail_url=None)
    agent.review(post)
    # create called once for text, never for image
    assert agent._client.messages.create.call_count == 1


# --- _qc_with_retry ---------------------------------------------------------


def test_qc_with_retry_passes_immediately(base_config):
    from scheduler.cron import _qc_with_retry

    qa = _agent(base_config)
    qa._client.messages.create.return_value = _msg("OK")
    post = _post(caption="Good caption.", thumbnail_url=None)
    assert _qc_with_retry(post, qa, thumbnail_agent=None) is True


def test_qc_with_retry_regenerates_on_image_failure(base_config):
    from scheduler.cron import _qc_with_retry

    qa = _agent(base_config)
    # First review raises; second review (after regen) passes.
    qa._client.messages.create.side_effect = [
        _msg("OK"),  # text check pass (first review)
        _msg('{"ok": false, "issue": "logo cut off"}'),  # image check fail
        _msg("OK"),  # text check pass (second review)
        _msg('{"ok": true}'),  # image check pass
    ]

    thumbnail_agent = MagicMock()
    post = _post(caption="Caption.", thumbnail_url="https://test.supabase.co/img.jpg")
    with patch.object(QualityAgent, "_fetch_image", return_value=("data", "image/jpeg")):
        result = _qc_with_retry(post, qa, thumbnail_agent)

    assert result is True
    thumbnail_agent.generate.assert_called_once_with(post)


def test_qc_with_retry_marks_failed_when_regen_also_fails(base_config):
    from scheduler.cron import _qc_with_retry

    qa = _agent(base_config)
    qa._client.messages.create.side_effect = [
        _msg("OK"),  # text pass
        _msg('{"ok": false, "issue": "logo missing"}'),  # image fail (first)
        _msg("OK"),  # text pass
        _msg('{"ok": false, "issue": "still wrong"}'),  # image fail (second)
    ]

    thumbnail_agent = MagicMock()
    post = _post(caption="Caption.", thumbnail_url="https://test.supabase.co/img.jpg")
    with patch.object(QualityAgent, "_fetch_image", return_value=("data", "image/jpeg")):
        result = _qc_with_retry(post, qa, thumbnail_agent)

    assert result is False
    assert post.error is not None
    assert "QC" in post.error


def test_qc_with_retry_none_agent_always_passes(base_config):
    from scheduler.cron import _qc_with_retry

    post = _post()
    assert _qc_with_retry(post, quality_agent=None, thumbnail_agent=None) is True


# --- Config -----------------------------------------------------------------


def test_missing_api_key_raises(base_config):
    import dataclasses

    from core.config import ConfigError

    cfg = dataclasses.replace(base_config, anthropic_api_key=None)
    with pytest.raises(ConfigError):
        QualityAgent(cfg)

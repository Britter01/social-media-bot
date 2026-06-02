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


# --- _generate_media (image QC integrated) ----------------------------------


def test_generate_media_imagen_called_once_on_overlay_retry(base_config):
    """Imagen is called exactly once even when the first overlay fails QC."""
    from scheduler.cron import _generate_media

    qa = _agent(base_config)
    # First overlay check fails, second passes.
    qa._client.messages.create.side_effect = [
        _msg('{"ok": false, "issue": "logo cut off"}'),
        _msg('{"ok": true}'),
    ]

    thumbnail_agent = MagicMock()
    thumbnail_agent.generate_raw.return_value = b"raw"
    thumbnail_agent.apply_overlay.return_value = b"overlaid"

    post = _post(caption="Caption.")
    _generate_media(post, thumbnail_agent, video_agent=None, quality_agent=qa)

    thumbnail_agent.generate_raw.assert_called_once()  # Imagen: once only
    assert thumbnail_agent.apply_overlay.call_count == 2  # overlay retried (free)
    thumbnail_agent.upload.assert_called_once()  # uploaded once at the end


def test_generate_media_raises_quality_error_after_two_overlay_failures(base_config):
    """QualityError propagates when both overlay attempts fail."""
    from scheduler.cron import _generate_media

    qa = _agent(base_config)
    qa._client.messages.create.side_effect = [
        _msg('{"ok": false, "issue": "logo missing"}'),
        _msg('{"ok": false, "issue": "still missing"}'),
    ]

    thumbnail_agent = MagicMock()
    thumbnail_agent.generate_raw.return_value = b"raw"
    thumbnail_agent.apply_overlay.return_value = b"overlaid"

    post = _post()
    with pytest.raises(QualityError):
        _generate_media(post, thumbnail_agent, video_agent=None, quality_agent=qa)

    thumbnail_agent.generate_raw.assert_called_once()  # still only one Imagen call
    thumbnail_agent.upload.assert_not_called()  # never uploaded a bad image


def test_generate_media_no_quality_agent_uploads_directly(base_config):
    """Without a quality agent the thumbnail is uploaded without QC."""
    from scheduler.cron import _generate_media

    thumbnail_agent = MagicMock()
    thumbnail_agent.generate_raw.return_value = b"raw"
    thumbnail_agent.apply_overlay.return_value = b"overlaid"

    post = _post()
    _generate_media(post, thumbnail_agent, video_agent=None, quality_agent=None)

    thumbnail_agent.generate_raw.assert_called_once()
    thumbnail_agent.upload.assert_called_once()


def test_generate_media_no_thumbnail_agent_skips_imagen(base_config):
    from scheduler.cron import _generate_media

    post = _post()
    _generate_media(post, thumbnail_agent=None, video_agent=None, quality_agent=None)
    # Should not raise; nothing to assert beyond no crash


# --- Config -----------------------------------------------------------------


def test_missing_api_key_raises(base_config):
    import dataclasses

    from core.config import ConfigError

    cfg = dataclasses.replace(base_config, anthropic_api_key=None)
    with pytest.raises(ConfigError):
        QualityAgent(cfg)

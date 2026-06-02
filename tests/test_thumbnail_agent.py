"""Tests for agents.thumbnail_agent (Imagen client mocked)."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agents.thumbnail_agent import ThumbnailAgent
from core.models import Post, PostStatus


def _imagen_response(image_bytes=b"\x89PNG-fake"):
    image = SimpleNamespace(image=SimpleNamespace(image_bytes=image_bytes))
    return SimpleNamespace(generated_images=[image])


def _no_storage_cfg(base_config):
    # Strip supabase creds so the agent persists locally, not to storage.
    return dataclasses.replace(base_config, supabase_url=None, supabase_key=None)


def test_generate_writes_local_file(base_config, tmp_path):
    cfg = _no_storage_cfg(base_config)
    agent = ThumbnailAgent(cfg, output_dir=str(tmp_path))
    agent._client = MagicMock()
    agent._client.models.generate_images.return_value = _imagen_response()

    post = Post(pillar="Tech Lifestyle", platform="instagram", topic="desk setup")
    # Patch the overlay so PIL never tries to decode the fake bytes.
    with patch("core.image_utils.add_brand_overlay", side_effect=lambda b, *a, **kw: b):
        agent.generate(post)

    assert post.thumbnail_url == str(tmp_path / f"{post.id}.png")
    assert (tmp_path / f"{post.id}.png").read_bytes() == b"\x89PNG-fake"
    assert post.status == PostStatus.MEDIA_READY.value


def test_generate_uses_storage_when_available(base_config, tmp_path):
    agent = ThumbnailAgent(_no_storage_cfg(base_config), output_dir=str(tmp_path))
    agent._client = MagicMock()
    agent._client.models.generate_images.return_value = _imagen_response()
    # Simulate a configured storage backend.
    agent._storage = MagicMock()
    agent._storage.upload.return_value = "https://cdn.example/thumb.png"

    post = Post(pillar="Review", platform="instagram")
    with patch("core.image_utils.add_brand_overlay", side_effect=lambda b, *a, **kw: b):
        agent.generate(post)

    assert post.thumbnail_url == "https://cdn.example/thumb.png"
    agent._storage.upload.assert_called_once()
    args, kwargs = agent._storage.upload.call_args
    assert args[0] == f"thumbnails/{post.id}.png"
    assert kwargs.get("content_type") == "image/png"


def test_generate_raises_when_no_images(base_config, tmp_path):
    agent = ThumbnailAgent(_no_storage_cfg(base_config), output_dir=str(tmp_path))
    agent._client = MagicMock()
    agent._client.models.generate_images.return_value = SimpleNamespace(generated_images=[])
    post = Post(pillar="Review", platform="instagram")
    with pytest.raises(RuntimeError):
        agent.generate(post)


def test_missing_google_key_raises(base_config):
    cfg = dataclasses.replace(base_config, google_api_key=None)
    from core.config import ConfigError

    with pytest.raises(ConfigError):
        ThumbnailAgent(cfg)

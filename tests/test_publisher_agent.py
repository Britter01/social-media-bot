"""Tests for agents.publisher_agent."""

from __future__ import annotations

import dataclasses

import httpx
import pytest

from agents.publisher_agent import PublisherAgent, PublishError
from core.models import Post, PostStatus


def test_dry_run_does_not_call_apis(base_config):
    cfg = dataclasses.replace(base_config, dry_run=True)
    post = Post(pillar="Review", platform="instagram", caption="Hello")
    PublisherAgent(cfg).publish(post)
    assert post.status == PostStatus.PUBLISHED.value
    assert post.platform_post_id == "dry-run"
    assert post.published_time is not None


def test_publish_is_idempotent_when_already_published(base_config, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("must not call any API for an already-published post")

    monkeypatch.setattr(httpx, "Client", _boom)
    post = Post(pillar="Review", platform="instagram", caption="Hi")
    post.platform_post_id = "existing-123"
    post.mark(PostStatus.PUBLISHED)

    PublisherAgent(base_config).publish(post)

    assert post.platform_post_id == "existing-123"
    assert post.status == PostStatus.PUBLISHED.value


def test_unsupported_platform_raises(base_config):
    post = Post(pillar="Review", platform="myspace", caption="Hi")
    with pytest.raises(PublishError):
        PublisherAgent(base_config).publish(post)


def test_instagram_requires_thumbnail(base_config):
    post = Post(pillar="Review", platform="instagram", caption="Hi")
    with pytest.raises(PublishError):
        PublisherAgent(base_config).publish(post)
    assert post.status == PostStatus.FAILED.value


def test_facebook_requires_thumbnail(base_config):
    post = Post(pillar="Review", platform="facebook", caption="Hi")
    with pytest.raises(PublishError):
        PublisherAgent(base_config).publish(post)
    assert post.status == PostStatus.FAILED.value


# --- Fake httpx client for the Instagram happy path -------------------


class _FakeResponse:
    def __init__(self, payload, headers=None, status_code=200):
        self._payload = payload
        self.headers = headers or {}
        self.status_code = status_code
        self.text = str(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)

    def json(self):
        return self._payload


class _FakeClient:
    """Returns a media container id, then a published id."""

    def __init__(self, *args, **kwargs):
        self._calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, **kwargs):
        self._calls += 1
        if url.endswith("/media"):
            return _FakeResponse({"id": "container-1"})
        if url.endswith("/media_publish"):
            return _FakeResponse({"id": "published-1"})
        raise AssertionError(f"unexpected url {url}")

    def get(self, url, **kwargs):
        # Container status check — always return FINISHED
        return _FakeResponse({"status_code": "FINISHED"})


def test_instagram_publish_happy_path(base_config, monkeypatch):
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    post = Post(
        pillar="Tech Lifestyle",
        platform="instagram",
        caption="Beautiful setup.",
        thumbnail_url="https://qfyqoxpcoinlbxgjsihn.supabase.co/storage/v1/object/public/media/thumbnails/test.png",
    )
    PublisherAgent(base_config).publish(post)
    assert post.status == PostStatus.PUBLISHED.value
    assert post.platform_post_id == "published-1"


def test_linkedin_publish_uses_restli_header(base_config, monkeypatch):
    class _LinkedInClient(_FakeClient):
        def post(self, url, **kwargs):
            return _FakeResponse({}, headers={"x-restli-id": "urn:li:share:99"})

    monkeypatch.setattr(httpx, "Client", _LinkedInClient)
    post = Post(pillar="Productivity", platform="linkedin", caption="Focus.")
    PublisherAgent(base_config).publish(post)
    assert post.platform_post_id == "urn:li:share:99"
    assert post.status == PostStatus.PUBLISHED.value

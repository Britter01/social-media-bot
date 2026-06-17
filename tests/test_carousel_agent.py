"""Tests for agents.carousel_agent — text-based 4-slide carousels (Claude mocked)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agents.carousel_agent import CarouselAgent
from core.models import Post

_PLAN = {
    "format_type": "tips",
    "cover_headline": "Work Smarter, Not Harder",
    "cover_subtext": "Three habits that change everything.",
    "slides": [
        {"headline": "Batch Your Deep Work", "body": "Group hard tasks into one focused block."},
        {"headline": "Kill Notifications", "body": "Silence pings so your brain can settle in."},
    ],
    "cta_headline": "Try One Today",
    "cta_body": "Pick a single habit and start this week.",
}


def _agent(base_config):
    agent = CarouselAgent(base_config)
    # Mock Claude planning.
    agent._client = MagicMock()
    agent._client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(text=json.dumps(_PLAN))]
    )
    # Mock storage so uploads return a URL without hitting Supabase.
    agent._storage = MagicMock()
    agent._storage.upload.side_effect = lambda path, *a, **kw: f"https://cdn.example/{path}"
    return agent


def test_only_needs_anthropic_key(base_config):
    import dataclasses

    # No image keys at all — text carousels must still init.
    cfg = dataclasses.replace(base_config, google_api_key=None, higgsfield_api_key=None)
    CarouselAgent(cfg)  # should not raise


def test_creates_exactly_four_text_slides(base_config):
    """When scene cover is unavailable (returns None), all 4 slides are dark text cards."""
    agent = _agent(base_config)
    source = Post(pillar="Productivity", platform="instagram", topic="focus habits")

    with (
        patch("agents.carousel_agent._make_scene_cover", return_value=None),
        patch("core.image_utils.add_brand_overlay", side_effect=lambda b, *a, **kw: b),
        patch("core.image_utils.make_dark_text_card", return_value=b"\x89PNG-card") as mk,
    ):
        carousel = agent.create_from_post(source)

    assert carousel.post_type == "carousel"
    assert len(carousel.slides) == 4
    roles = [s["role"] for s in carousel.slides]
    assert roles == ["cover", "content", "content", "cta"]
    assert all(s["image_url"].startswith("https://cdn.example/") for s in carousel.slides)
    assert carousel.thumbnail_url == carousel.slides[0]["image_url"]
    assert mk.call_count == 4  # all 4 slides fall back to dark text card


def test_scene_cover_used_for_slide_zero(base_config):
    """When scene cover succeeds, slide 0 uses the scene photo; slides 1-3 use text cards."""
    agent = _agent(base_config)
    source = Post(pillar="Productivity", platform="instagram", topic="focus habits")

    scene_bytes = b"\x89PNG-scene"
    with (
        patch("agents.carousel_agent._make_scene_cover", return_value=scene_bytes),
        patch("core.image_utils.add_brand_overlay", side_effect=lambda b, *a, **kw: b),
        patch("core.image_utils.make_dark_text_card", return_value=b"\x89PNG-card") as mk,
    ):
        carousel = agent.create_from_post(source)

    assert len(carousel.slides) == 4
    assert mk.call_count == 3  # slides 1, 2, 3 only — slide 0 uses scene cover


def test_trims_extra_value_slides_to_two(base_config):
    agent = _agent(base_config)
    plan = dict(_PLAN)
    plan["slides"] = _PLAN["slides"] + [
        {"headline": "Bonus", "body": "Extra slide that must be trimmed."}
    ]
    agent._client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(text=json.dumps(plan))]
    )
    source = Post(pillar="Productivity", platform="facebook", topic="focus habits")

    with (
        patch("core.image_utils.add_brand_overlay", side_effect=lambda b, *a, **kw: b),
        patch("core.image_utils.make_dark_text_card", return_value=b"\x89PNG-card"),
    ):
        carousel = agent.create_from_post(source)

    assert len(carousel.slides) == 4

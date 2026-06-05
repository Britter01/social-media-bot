"""Carousel agent — multi-slide post generator for Instagram and Facebook.

Creates a structured carousel post from a topic in two steps:
  1. Claude Sonnet plans the carousel: format, slide headlines and body copy.
  2. Imagen 4 Fast generates one image per slide; each is uploaded to Supabase.

Supported formats (the model picks the best fit for the topic):
  tips      — "5 tips for doing X"
  howto     — "Step-by-step: how to X"
  breakdown — "Breaking down X: what it is, why it matters, how to use it"
  compare   — "X vs Y: which is right for you?"
  myths     — "Common myths about X, debunked"

The returned Post has post_type='carousel' and slides=[{headline, body, image_url}].
thumbnail_url is set to the cover slide image so the dashboard preview works.

Model choice: Sonnet for copy (judgment + brand voice), Imagen Fast for images
(cheapest tier, no quality difference for editorial photography at this scale).
"""

from __future__ import annotations

import logging

import anthropic
from pydantic import BaseModel, Field

from core.config import Config, config
from core.models import Post, PostStatus

logger = logging.getLogger(__name__)

_FORMATS = "tips | howto | breakdown | compare | myths"

_ASPECT_RATIO = {
    "instagram": "1:1",
    "facebook": "1:1",
    "twitter": "16:9",
    "linkedin": "1:1",
}


class CarouselSlide(BaseModel):
    headline: str = Field(description="Bold, punchy slide headline — max 8 words.")
    body: str = Field(description="1-2 supporting sentences. Conversational, not corporate.")
    image_prompt: str = Field(
        description=(
            "A visual direction for this slide's image — describe the scene, mood, "
            "and subject without mentioning text, logos, or overlay graphics."
        )
    )


class CarouselPlan(BaseModel):
    format_type: str = Field(description=f"One of: {_FORMATS}")
    cover_headline: str = Field(description="The carousel title/hook — max 10 words.")
    cover_subtext: str = Field(description="One-sentence teaser shown below the cover headline.")
    cover_image_prompt: str = Field(description="Visual direction for the cover slide image.")
    slides: list[CarouselSlide] = Field(
        description="4–6 content slides. Each builds on the cover hook."
    )
    cta_headline: str = Field(description="Final slide headline — prompt an action or reflection.")
    cta_body: str = Field(description="One sentence CTA. Warm, not pushy.")
    cta_image_prompt: str = Field(description="Visual direction for the CTA slide image.")


class CarouselAgent:
    """Generates a multi-slide carousel post from a regular Post's topic."""

    def __init__(self, cfg: Config = config) -> None:
        cfg.require("anthropic_api_key", "google_api_key")
        self._cfg = cfg
        self._client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)

        from google import genai

        self._genai_client = genai.Client(api_key=cfg.google_api_key)
        self._genai = genai

        self._storage = None
        if cfg.supabase_url and cfg.supabase_key:
            try:
                from core.storage import get_storage

                self._storage = get_storage(cfg)
            except Exception:
                logger.warning("Supabase Storage unavailable for carousel images")

    def create_from_post(self, source: Post, quality_agent=None) -> Post:
        """Generate a carousel Post from an existing post's topic and pillar.

        Returns a new Post (different id) with post_type='carousel' and
        slides populated. The source post is not modified.
        Pass ``quality_agent`` to run per-slide QC before uploading.
        """
        import uuid as _uuid

        carousel_id = str(_uuid.uuid4())  # fixed before image generation so paths are stable
        plan = self._plan_carousel(source)
        slides_data = self._generate_images(carousel_id, source, plan, quality_agent=quality_agent)

        carousel = Post(
            id=carousel_id,
            pillar=source.pillar,
            platform=source.platform,
            topic=source.topic,
            caption=self._build_caption(plan),
            hashtags=list(source.hashtags),
            title=plan.cover_headline,
            post_type="carousel",
            slides=slides_data,
            thumbnail_url=slides_data[0]["image_url"] if slides_data else source.thumbnail_url,
        )
        carousel.mark(PostStatus.MEDIA_READY)
        logger.info(
            "Created carousel post %s (%s/%s) with %d slides",
            carousel.id,
            carousel.pillar,
            carousel.platform,
            len(slides_data),
        )
        return carousel

    # --- Internals -----------------------------------------------------------

    def _plan_carousel(self, post: Post) -> CarouselPlan:
        """Ask Claude to plan the carousel format and slide copy."""
        system = (
            f"You are the content strategist for {self._cfg.brand_name}, "
            f"founded by {self._cfg.brand_founder}. "
            f'Tagline: "{self._cfg.brand_tagline}". '
            "Voice: clear, confident, warm. Never patronising. Short sentences. "
            "You are planning a carousel post — a swipeable series of slides. "
            "Each slide should give genuine value. Avoid filler. "
            "Make the cover slide impossible to scroll past."
        )
        prompt = (
            f"Plan a carousel post for the '{post.pillar}' pillar on {post.platform.upper()}.\n"
            f"Topic: {post.topic}\n\n"
            f"Choose the best format for this topic ({_FORMATS}). "
            "Return 4-6 content slides (not counting cover and CTA). "
            "Each slide needs a headline, body copy, and an image direction — "
            "describe a real-world scene or visual metaphor that suits the slide, "
            "no text overlays or infographic styles."
        )
        response = self._client.messages.parse(
            model=self._cfg.model_creative,
            max_tokens=2000,
            thinking={"type": "adaptive"},
            output_config={"effort": "low"},
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_format=CarouselPlan,
        )
        plan = response.parsed_output
        if plan is None:
            raise RuntimeError("Claude returned no carousel plan")
        logger.info(
            "Planned carousel '%s' (%s, %d slides)",
            plan.cover_headline,
            plan.format_type,
            len(plan.slides),
        )
        return plan

    def _generate_images(
        self, carousel_id: str, post: Post, plan: CarouselPlan, quality_agent=None
    ) -> list[dict]:
        """Generate and upload one image per slide; return slide dicts."""
        from google.genai import types

        aspect_ratio = _ASPECT_RATIO.get(post.platform, "1:1")
        brand_prefix = (
            f"Editorial lifestyle photograph. Theme: {post.pillar}. "
            "Clean minimal composition, soft natural light, modern technology in a beautifully "
            "lived-in space, warm and confident mood, high detail. "
            "The image must contain absolutely no text, no letters, no words, no numbers, "
            "no brand names, no logos, no watermarks, no signs, no labels, and no readable "
            "characters of any kind anywhere in the image. "
            "Any device screens must display only abstract blurred patterns — never text. "
        )

        # Build the full slide list: cover + content + CTA
        all_slides = (
            [
                {
                    "headline": plan.cover_headline,
                    "body": plan.cover_subtext,
                    "prompt": brand_prefix + plan.cover_image_prompt,
                    "role": "cover",
                }
            ]
            + [
                {
                    "headline": s.headline,
                    "body": s.body,
                    "prompt": brand_prefix + s.image_prompt,
                    "role": "content",
                }
                for s in plan.slides
            ]
            + [
                {
                    "headline": plan.cta_headline,
                    "body": plan.cta_body,
                    "prompt": brand_prefix + plan.cta_image_prompt,
                    "role": "cta",
                }
            ]
        )

        result = []
        for i, slide in enumerate(all_slides):
            try:
                response = self._genai_client.models.generate_images(
                    model=self._cfg.imagen_model,
                    prompt=slide["prompt"],
                    config=types.GenerateImagesConfig(
                        number_of_images=1,
                        aspect_ratio=aspect_ratio,
                    ),
                )
                images = getattr(response, "generated_images", None) or []
                if not images:
                    logger.warning("Imagen returned no image for slide %d; skipping", i)
                    continue
                raw_bytes = images[0].image.image_bytes

                from core.image_utils import add_brand_overlay

                final_bytes = add_brand_overlay(
                    raw_bytes,
                    self._cfg.brand_name,
                    self._cfg.brand_tagline,
                )

                # QC check — retry overlay once (free) if it fails; skip slide if still bad
                if quality_agent is not None:
                    from agents.quality_agent import QualityError

                    try:
                        quality_agent.check_image_bytes(post, final_bytes, label=f"slide {i}")
                    except QualityError as exc:
                        logger.warning("QC: slide %d failed (%s) — retrying overlay", i, exc)
                        final_bytes = add_brand_overlay(
                            raw_bytes,
                            self._cfg.brand_name,
                            self._cfg.brand_tagline,
                        )
                        try:
                            quality_agent.check_image_bytes(post, final_bytes, label=f"slide {i}")
                        except QualityError as exc2:
                            logger.warning(
                                "QC: slide %d still failing after retry (%s) — skipping", i, exc2
                            )
                            continue

                image_url = self._upload(carousel_id, i, final_bytes)
                result.append(
                    {
                        "headline": slide["headline"][:120],
                        "body": slide["body"][:400],
                        "image_url": image_url,
                        "role": slide["role"],
                    }
                )
                logger.debug("Generated carousel slide %d for post %s", i, post.id)
            except Exception:
                logger.exception("Failed generating image for slide %d of post %s", i, post.id)

        return result

    def _upload(self, post_id: str, slide_index: int, image_bytes: bytes) -> str:
        """Upload slide image to Supabase; return public URL or empty string."""
        path = f"carousels/{post_id}/slide_{slide_index:02d}.png"
        if self._storage is not None:
            return self._storage.upload(path, image_bytes, content_type="image/png")
        # Local fallback
        import os
        import tempfile

        local_path = os.path.join(tempfile.gettempdir(), path.replace("/", "_"))
        with open(local_path, "wb") as fh:
            fh.write(image_bytes)
        return local_path

    @staticmethod
    def _build_caption(plan: CarouselPlan) -> str:
        """Assemble a caption that works as a standalone Instagram/Facebook post."""
        lines = [plan.cover_headline, "", plan.cover_subtext, ""]
        for i, slide in enumerate(plan.slides, 1):
            lines.append(f"{i}. {slide.headline}")
        lines += ["", plan.cta_body]
        return "\n".join(lines)
